"""End-to-end SMS flow test (issue #50) — the M1 gate rehearsal.

Marker: ``integration`` (real Postgres via docker-compose/dedicated test DB
+ ``alembic upgrade head``). Never ``eval`` — no real Anthropic or Twilio
credentials are ever used in this module; every external call is
faked/mocked (see "Fakes" below). Never touches the real ``.env``.

Scope — fake-based rehearsal only (Phase 5 of the core-loop campaign,
``.claude/skills/stoop-core-loop-campaign/SKILL.md``)
--------------------------------------------------------------------------
Issue #50's acceptance criteria describe TWO variants:

1. **THIS module** — the fake-based, CI-runnable rehearsal: a signed
   webhook POST drives the real webhook -> the real graph (mocked
   Anthropic) -> the real draft-approval endpoints -> the real scheduler
   tick -> a FAKE Twilio sender, asserting the complete audit trail for
   both the routine/urgent draft loop and the emergency (no-approval-wait)
   chain.
2. The issue's **staging variant** (real Twilio A2P number, real Anthropic,
   a live LangSmith trace) is credential-gated — Twilio A2P registration,
   a LangSmith API key, and a Fly deploy are founder/human-provisioned
   externals not available to this agent — and is explicitly OUT OF SCOPE
   here. Every fake below is structured to swap for the real client via
   env/settings alone: ``app.integrations.twilio_send.get_twilio_sender``
   (real binding: ``TwilioRestSender``, gated only by
   ``set_twilio_sender_for_tests`` being left uncalled) and
   ``app.integrations.anthropic.get_client`` (real binding: a genuine
   ``anthropic.AsyncAnthropic``, gated only by these tests' own
   ``monkeypatch.setattr``). No code path exercised here would need to
   change SHAPE to run for real elsewhere — only the test harness's own
   fakes would need to be omitted.

Fakes
-----
- **Twilio SMS/voice** — a small in-module fake (``_FakeTwilioSender``)
  injected via ``app.integrations.twilio_send.set_twilio_sender_for_tests``,
  the SAME seam ``app/agent/emergency_chain.py``'s own T+0 execution and
  ``app/agent/draft_sender.py`` (via ``app.integrations.sms_sender.
  TwilioBackedSmsSender``) both funnel through. Nothing in this module ever
  constructs a real ``twilio.rest.Client`` (``tests/
  test_twilio_send_allowlist.py`` enforces this for ``app/``; this fake
  simply never reaches that code path at all).
- **Anthropic** — ``app.integrations.anthropic.get_client`` monkeypatched
  to a fake client returning canned ``ToolUseBlock`` responses for
  classify_intent / classify_severity / draft_response, in that call
  order — mirrors ``tests/test_drafts_router.py``'s own pattern. Mocked in
  BOTH variants below: variant 2's Tier-0 HARD hit still schedules the
  background graph run (``app.agent.graph_entry.enqueue_classification``),
  which would otherwise attempt a real (if doomed-to-fail-auth) network
  call using the placeholder ``ANTHROPIC_API_KEY`` — never acceptable
  here regardless of whether the assertions below depend on its output.
- **Weather (Open-Meteo)** — never invoked: every property seeded below
  has no ``lat``/``lon``, and ``app/agent/nodes/load_context.py`` skips
  the lookup entirely when either is unset.

LangSmith trace (issue #50 AC #3)
----------------------------------
"Full run visible as one LangSmith trace with readable reasoning_log"
needs a real LangSmith API key against a founder-provisioned project —
not available in this environment. Represented below as a SKIPPED test
with an explicit reason, never faked (a trace that never touched a real
LangSmith project would be a false-positive signal that this AC is
proven when it isn't).
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio
import respx
from anthropic.types import ToolUseBlock
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.ec import (
    EllipticCurvePrivateKey,
    EllipticCurvePublicKey,
)
from httpx import ASGITransport
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine

import app.agent.nodes.finalize_draft_decision as finalize_draft_decision_mod
import app.db.session as db_mod
import app.scheduler as scheduler_mod
from app.agent import emergency_chain
from app.agent.checkpointer import close_checkpointer, setup_checkpointer
from app.config import settings
from app.integrations import anthropic as anthropic_mod
from app.integrations.twilio import compute_signature
from app.integrations.twilio_send import set_twilio_sender_for_tests
from app.main import app
from tests import factories

# ---------------------------------------------------------------------------
# DB / migration harness — mirrors tests/test_webhooks_twilio_sms.py and
# tests/test_drafts_router.py exactly, EXCEPT the fallback DB name: this
# module's own dedicated database is ``stoop_e2e`` (never ``stoop``/
# ``stoop_drafts``/``stoop_prov`` — see the campaign's own DB discipline),
# so the fallback used when ``DATABASE_URL`` is unset points there too,
# rather than the shared dev DB every other test module falls back to.
# ---------------------------------------------------------------------------

_DB_URL_DEFAULT = "postgresql+asyncpg://stoop:stoop@localhost:5432/stoop_e2e"


def _get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", _DB_URL_DEFAULT)
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql+asyncpg://", url)


def _alembic(*args: str) -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-m", "alembic", *args],
        capture_output=True,
        text=True,
        cwd=os.path.join(os.path.dirname(__file__), ".."),
        env={**os.environ, "DATABASE_URL": _get_db_url()},
    )
    if result.returncode != 0:
        cmd = " ".join(args)
        raise RuntimeError(
            f"alembic {cmd!r} failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )


@pytest.fixture(scope="session", autouse=False)
def _migrate_once() -> None:  # type: ignore[misc]
    _alembic("upgrade", "head")
    yield


@pytest_asyncio.fixture
async def db_engine(_migrate_once: None) -> AsyncGenerator[AsyncEngine, None]:
    engine = create_async_engine(_get_db_url(), echo=False)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine: AsyncEngine) -> AsyncGenerator[AsyncSession, None]:
    async with AsyncSession(db_engine) as session:
        yield session


@pytest_asyncio.fixture(autouse=True)
async def dispose_app_engine() -> AsyncGenerator[None, None]:
    """Same rationale as every other integration test module: the app's
    own module-level admin engine (backing ``get_admin_session``, which the
    webhook / graph nodes / scheduler sweeps all use) must not carry pooled
    connections across event loops between tests."""
    await db_mod.engine.dispose()
    yield
    await db_mod.engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _checkpointer_lifecycle(_migrate_once: None) -> AsyncGenerator[None, None]:
    """``httpx.ASGITransport`` does not run FastAPI's own lifespan (no
    ``setup_checkpointer()`` call), but the graph the webhook's background
    task invokes always compiles the case-scoped graph WITH the Postgres
    checkpointer (``app/agent/graph.py``), whether or not a run actually
    reaches a live ``interrupt()`` — same ordering contract as every other
    integration test module that exercises the graph directly."""
    await setup_checkpointer()
    yield
    await close_checkpointer()


@pytest.fixture(autouse=True)
def _reset_anthropic_client() -> None:
    anthropic_mod.reset_client_for_tests()
    yield
    anthropic_mod.reset_client_for_tests()


# ---------------------------------------------------------------------------
# JWT / JWKS harness — mirrors tests/test_drafts_router.py exactly (its own
# module docstring: "mirrored from test_auth.py to keep tests
# self-contained").
# ---------------------------------------------------------------------------

_ISSUER = "https://test.supabase.co/auth/v1"
_JWKS_URL = "https://test.supabase.co/auth/v1/.well-known/jwks.json"
_KID = "test-kid-e2e-001"
_P256_COORD_BYTES = 32


def _b64url(n: int, length: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes(length, "big")).rstrip(b"=").decode()


def _make_keypair() -> tuple[EllipticCurvePrivateKey, EllipticCurvePublicKey]:
    private = ec.generate_private_key(ec.SECP256R1())
    return private, private.public_key()


def _public_key_to_jwks(public: EllipticCurvePublicKey, kid: str) -> dict[str, Any]:
    nums = public.public_numbers()
    return {
        "keys": [
            {
                "kty": "EC",
                "crv": "P-256",
                "x": _b64url(nums.x, _P256_COORD_BYTES),
                "y": _b64url(nums.y, _P256_COORD_BYTES),
                "kid": kid,
                "use": "sig",
                "alg": "ES256",
            }
        ]
    }


def _mint_token(private: EllipticCurvePrivateKey, *, sub: str, kid: str = _KID) -> str:
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": sub,
        "iss": _ISSUER,
        "aud": "authenticated",
        "role": "authenticated",
        "iat": now,
        "exp": now + 3600,
        "email": "landlord@example.com",
    }
    return jwt.encode(payload, private, algorithm="ES256", headers={"kid": kid})


@pytest.fixture()
def keypair() -> tuple[EllipticCurvePrivateKey, EllipticCurvePublicKey]:
    return _make_keypair()


@pytest.fixture()
def private_key(
    keypair: tuple[EllipticCurvePrivateKey, EllipticCurvePublicKey],
) -> EllipticCurvePrivateKey:
    return keypair[0]


@pytest.fixture()
def jwks_payload(keypair: tuple[EllipticCurvePrivateKey, EllipticCurvePublicKey]) -> dict[str, Any]:
    return _public_key_to_jwks(keypair[1], _KID)


def _mocked_jwks(jwks_payload: dict[str, Any]) -> respx.MockRouter:
    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    router.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))
    return router


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ---------------------------------------------------------------------------
# Twilio SMS webhook signing — mirrors tests/test_webhooks_twilio_sms.py
# exactly (same signing URL convention: no PUBLIC_BASE_URL set in tests, so
# the webhook's own fallback reconstructs "http://test" + path, matching
# httpx's ASGITransport ``Host`` header).
# ---------------------------------------------------------------------------

_SIGNING_URL = "http://test/webhooks/twilio/sms"


def _sms_params(*, message_sid: str, from_number: str, to_number: str, body: str) -> dict[str, str]:
    return {
        "MessageSid": message_sid,
        "SmsSid": message_sid,
        "AccountSid": "AC" + "0" * 32,
        "From": from_number,
        "To": to_number,
        "Body": body,
        "NumMedia": "0",
    }


def _sign(params: dict[str, str]) -> str:
    assert settings.twilio_auth_token is not None
    return compute_signature(_SIGNING_URL, params, settings.twilio_auth_token)


async def _post_sms(params: dict[str, str]) -> httpx.Response:
    headers = {"X-Twilio-Signature": _sign(params)}
    async with _client() as client:
        return await client.post("/webhooks/twilio/sms", data=params, headers=headers)


# ---------------------------------------------------------------------------
# Fake Anthropic client — mirrors tests/test_drafts_router.py's own
# ``_happy_path_fake_messages`` pattern exactly (three canned tool-use
# responses, popped in call order: classify_intent -> classify_severity ->
# draft_response).
# ---------------------------------------------------------------------------


def _fake_message(*, tool_input: dict[str, Any], tool_name: str = "tool") -> SimpleNamespace:
    block = ToolUseBlock(id="toolu_test", input=tool_input, name=tool_name, type="tool_use")
    usage = SimpleNamespace(input_tokens=150, output_tokens=40)
    return SimpleNamespace(content=[block], usage=usage, model="claude-sonnet-5")


class _FakeMessages:
    def __init__(self, *, responses: list[Any]) -> None:
        self._responses = list(responses)

    async def create(self, **kwargs: Any) -> Any:
        return self._responses.pop(0)


class _FakeClient:
    def __init__(self, messages: _FakeMessages) -> None:
        self.messages = messages


def _patch_client(monkeypatch: pytest.MonkeyPatch, fake_messages: _FakeMessages) -> None:
    monkeypatch.setattr(anthropic_mod, "get_client", lambda: _FakeClient(fake_messages))


def _happy_path_fake_messages(*, body: str = "I'll take a look today.") -> _FakeMessages:
    return _FakeMessages(
        responses=[
            _fake_message(
                tool_input={
                    "intent": "maintenance",
                    "is_new_issue": True,
                    "summary": "Heat is out",
                },
                tool_name="classify_intent",
            ),
            _fake_message(
                tool_input={
                    "severity": "URGENT",
                    "rules_fired": ["No heat, mild weather"],
                    "modifier": None,
                    "refusal_flags": [],
                    "reasoning": ["Tenant reports no heat."],
                },
                tool_name="classify_severity",
            ),
            _fake_message(
                tool_input={"body": body, "refusal_templates_used": []}, tool_name="draft_message"
            ),
        ]
    )


# ---------------------------------------------------------------------------
# Fake Twilio sender — records every call/text, never touches a network or
# real Twilio credentials. Injected via ``app.integrations.twilio_send.
# set_twilio_sender_for_tests`` — the SAME sanctioned seam
# ``tests/test_agent_emergency_chain.py``'s / ``tests/test_drafts_router.py``'s
# own fakes use, proving the FULL live chains: the draft flow's
# ``get_default_sms_sender()`` -> ``TwilioBackedSmsSender`` ->
# ``get_twilio_sender()`` -> this fake, and the emergency chain's own direct
# ``get_twilio_sender()`` -> this fake. Never a real ``twilio.rest.Client``.
# ---------------------------------------------------------------------------


@dataclass
class _RecordedSend:
    kind: str  # "sms" | "call"
    to: str
    from_: str
    body: str | None = None
    twiml_url: str | None = None


@dataclass
class _FakeTwilioSender:
    calls: list[_RecordedSend] = field(default_factory=list)

    async def send_sms(self, *, to: str, from_: str, body: str) -> str:
        self.calls.append(_RecordedSend(kind="sms", to=to, from_=from_, body=body))
        return f"SM{uuid.uuid4().hex}"

    async def create_call(self, *, to: str, from_: str, twiml_url: str) -> str:
        self.calls.append(_RecordedSend(kind="call", to=to, from_=from_, twiml_url=twiml_url))
        return f"CA{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# Seeding / cleanup helpers — self-contained per this codebase's own
# convention (duplicated from tests/test_drafts_router.py's
# ``_seed_landlord_with_auth`` rather than imported, since ``tests/
# factories.py::insert_landlord`` doesn't accept an explicit
# ``auth_user_id`` — this module needs one that matches a minted JWT's
# ``sub``). Property/tenant/message seeding reuses ``tests/factories.py``
# directly wherever it already supports what's needed.
# ---------------------------------------------------------------------------


async def _seed_landlord_with_auth(
    session: AsyncSession,
    *,
    auth_user_id: str,
    phone: str | None = None,
    full_name: str | None = None,
) -> str:
    landlord_id = str(uuid.uuid4())
    await session.execute(
        text(
            "INSERT INTO landlords (id, auth_user_id, email, phone, full_name) "
            "VALUES (:id, :auth_id, :email, :phone, :full_name)"
        ),
        {
            "id": landlord_id,
            "auth_id": auth_user_id,
            "email": f"{landlord_id}@example.com",
            "phone": phone,
            "full_name": full_name,
        },
    )
    await session.commit()
    return landlord_id


async def _tenant_phone(session: AsyncSession, tenant_id: str) -> str:
    row = (
        (await session.execute(text("SELECT phone FROM tenants WHERE id = :id"), {"id": tenant_id}))
        .mappings()
        .one()
    )
    return str(row["phone"])


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    """FK-safe delete order. Unlike every OTHER inbound-only test module's
    own ``_cleanup`` (``messages.case_id`` is always NULL for an inbound
    tenant message, so those can delete ``cases`` before ``messages``),
    this module's Variant 1 drives a REAL outbound send
    (``app/agent/draft_sender.py``'s own ``_INSERT_OUTBOUND_MESSAGE_SQL``
    sets ``case_id`` to the real case id on the sent message row) — so
    ``messages`` must be deleted BEFORE ``cases`` here, or the FK blocks
    the ``cases`` delete."""
    params = {"lid": landlord_id}
    await session.execute(
        text(
            "DELETE FROM message_cases WHERE case_id IN "
            "(SELECT id FROM cases WHERE landlord_id = :lid)"
        ),
        params,
    )
    await session.execute(text("DELETE FROM drafts WHERE landlord_id = :lid"), params)
    await session.execute(
        text(
            "DELETE FROM message_status_events WHERE message_id IN "
            "(SELECT id FROM messages WHERE landlord_id = :lid)"
        ),
        params,
    )
    await session.execute(text("DELETE FROM notifications WHERE landlord_id = :lid"), params)
    await session.execute(text("DELETE FROM audit_log WHERE landlord_id = :lid"), params)
    await session.execute(text("DELETE FROM trust_metrics WHERE landlord_id = :lid"), params)
    await session.execute(text("DELETE FROM messages WHERE landlord_id = :lid"), params)
    await session.execute(text("DELETE FROM cases WHERE landlord_id = :lid"), params)
    await session.execute(text("DELETE FROM tenants WHERE landlord_id = :lid"), params)
    await session.execute(text("DELETE FROM properties WHERE landlord_id = :lid"), params)
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), params)
    await session.commit()


# ---------------------------------------------------------------------------
# VARIANT 1 — routine/urgent loop: signed webhook -> graph (mocked
# Anthropic) -> draft parks -> GET /v1/queue -> POST .../approve ->
# app.scheduler._run_one_tick() -> exactly one real send -> full audit
# trail.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_variant1_routine_urgent_loop_webhook_to_send(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #50 AC #1 (fake-based rehearsal): "inbound SMS -> persisted ->
    classified -> draft -> approve via API -> outbound SMS received."
    Also proves AC #4 ("audit trail contains the complete action sequence")
    for this variant."""
    monkeypatch.setattr(finalize_draft_decision_mod, "UNDO_WINDOW", timedelta(seconds=0))
    fake_sender = _FakeTwilioSender()
    set_twilio_sender_for_tests(fake_sender)
    _patch_client(monkeypatch, _happy_path_fake_messages())

    sub = str(uuid.uuid4())
    landlord_id = await _seed_landlord_with_auth(db_session, auth_user_id=sub)
    to_number = factories.fresh_phone()
    property_id = await factories.insert_property(db_session, landlord_id, twilio_number=to_number)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    tenant_phone = await _tenant_phone(db_session, tenant_id)
    token = _mint_token(private_key, sub=sub)

    tenant_body = "the heat has been out since this morning"
    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=tenant_phone, to_number=to_number, body=tenant_body
    )

    try:
        # --- signed POST to the Twilio SMS webhook ---
        webhook_response = await _post_sms(params)
        assert webhook_response.status_code == 200
        assert webhook_response.text == "<Response/>"

        # DETERMINISM INVARIANT this module (alone) depends on: the webhook
        # schedules classification via Starlette BackgroundTasks (never
        # asyncio.create_task), and httpx's ASGITransport awaits the ASGI app
        # — background tasks included — before _post_sms returns. That is why
        # the graph's rows can be read synchronously below. If the webhook
        # ever switches to create_task, or these tests move to a live-server
        # transport, every post-webhook read here becomes racy.
        message_row = (
            (
                await db_session.execute(
                    text("SELECT party, tenant_id FROM messages WHERE twilio_sid = :sid"),
                    {"sid": message_sid},
                )
            )
            .mappings()
            .one()
        )
        assert message_row["party"] == "tenant"
        assert str(message_row["tenant_id"]) == tenant_id

        # --- the graph ran (mocked Anthropic): classified + drafted, draft
        # parks pending, case awaiting_approval ---
        case_draft_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT c.id AS case_id, c.status AS case_status, "
                        "d.id AS draft_id, d.status AS draft_status, d.body AS draft_body "
                        "FROM cases c JOIN drafts d ON d.case_id = c.id "
                        "WHERE c.landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert case_draft_row["case_status"] == "awaiting_approval"
        assert case_draft_row["draft_status"] == "pending"
        case_id = str(case_draft_row["case_id"])
        draft_id = str(case_draft_row["draft_id"])
        draft_body = case_draft_row["draft_body"]

        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                # --- GET /v1/queue shows the card (real endpoint,
                # authenticated landlord) ---
                queue_response = await client.get(
                    "/v1/queue", headers={"Authorization": f"Bearer {token}"}
                )
                assert queue_response.status_code == 200, queue_response.text
                cards = [
                    item for item in queue_response.json()["items"] if item["case_id"] == case_id
                ]
                assert len(cards) == 1
                card = cards[0]
                assert card["draft_id"] == draft_id
                assert card["draft_body"] == draft_body
                assert card["draft_recipient"] == "tenant"
                assert card["severity"] == "urgent"
                assert card["tenant_message"] == tenant_body

                # Rule 3, asserted directly: NOTHING has been sent to the
                # tenant before the landlord approves — not merely inferred
                # from the draft still being 'pending'.
                assert [c for c in fake_sender.calls if c.to == tenant_phone] == []

                # --- POST /v1/drafts/{id}/approve (real endpoint) ---
                approve_response = await client.post(
                    f"/v1/drafts/{draft_id}/approve",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert approve_response.status_code == 200, approve_response.text
                assert approve_response.json()["status"] == "approved"

        # --- drive app.scheduler._run_one_tick() (not a bespoke loop) past
        # scheduled_send_at (UNDO_WINDOW patched to 0s above) ---
        await scheduler_mod._run_one_tick()  # noqa: SLF001

        own_calls = [c for c in fake_sender.calls if c.to == tenant_phone]
        assert len(own_calls) == 1, f"expected exactly one send, got {fake_sender.calls}"
        assert own_calls[0].kind == "sms"
        assert own_calls[0].from_ == to_number
        assert own_calls[0].body == draft_body

        draft_row = (
            (
                await db_session.execute(
                    text("SELECT status, sent_message_id FROM drafts WHERE id = :id"),
                    {"id": draft_id},
                )
            )
            .mappings()
            .one()
        )
        assert draft_row["status"] == "sent"
        assert draft_row["sent_message_id"] is not None

        sent_message_row = (
            (
                await db_session.execute(
                    text("SELECT direction, party, twilio_sid, body FROM messages WHERE id = :id"),
                    {"id": str(draft_row["sent_message_id"])},
                )
            )
            .mappings()
            .one()
        )
        assert sent_message_row["direction"] == "outbound"
        assert sent_message_row["party"] == "tenant"
        assert sent_message_row["twilio_sid"] is not None
        assert sent_message_row["twilio_sid"].startswith("SM")
        assert sent_message_row["body"] == draft_body

        case_status = (
            await db_session.execute(
                text("SELECT status FROM cases WHERE id = :id"), {"id": case_id}
            )
        ).scalar_one()
        assert case_status == "awaiting_tenant"

        # --- the COMPLETE ordered audit_log action sequence for the case
        # (scoped by landlord_id since 'message_received' rows carry
        # case_id=NULL -- see app/agent/graph_entry.py). Read off the real
        # vocabulary (schema-v1.md's audit_log.action CHECK): a fresh case
        # opens via 'case_opened' (not 'new_case' -- that's the internal
        # ROUTING action name in app/agent/case_lifecycle.py, translated to
        # the schema's own vocabulary before the INSERT), and BOTH
        # classify_intent and classify_severity write 'classified' rows
        # (the schema has no separate "intent classified" action -- see
        # classify_intent.py's own module docstring) -- so 'classified'
        # appears TWICE, not once. ---
        action_sequence = (
            (
                await db_session.execute(
                    text("SELECT action FROM audit_log WHERE landlord_id = :lid ORDER BY id"),
                    {"lid": landlord_id},
                )
            )
            .scalars()
            .all()
        )
        assert action_sequence == [
            "message_received",
            "case_opened",
            "classified",
            "classified",
            "drafted",
            "approved",
            "sent",
        ]
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# VARIANT 2 — emergency, no approval wait: a Tier-0 HARD-keyword tenant SMS
# fires the landlord voice call + tenant safety SMS INLINE inside the
# webhook request itself -- before any draft exists, before any approval
# step, and independent of the background graph run.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_variant2_emergency_hard_keyword_no_approval_wait(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #50 AC #2: "HARD-keyword SMS -> landlord call fired + tenant
    safety SMS, no approval wait." Also proves AC #4 for this variant (the
    audit trail contains 'emergency_triggered' + the chain's own attempt
    action) and that no pending draft was required for the sends to fire."""
    fake_sender = _FakeTwilioSender()
    set_twilio_sender_for_tests(fake_sender)
    # Mocked even though this variant's assertions don't depend on its
    # output -- the background graph run (enqueue_classification) is
    # ALWAYS scheduled regardless of the Tier-0 hit (app/routers/webhooks/
    # twilio.py), and must never attempt a real Anthropic network call.
    _patch_client(monkeypatch, _happy_path_fake_messages())

    landlord_phone = factories.fresh_phone()
    landlord_id = await _seed_landlord_with_auth(
        db_session, auth_user_id=str(uuid.uuid4()), phone=landlord_phone, full_name="Pat Landlord"
    )
    to_number = factories.fresh_phone()
    property_id = await factories.insert_property(db_session, landlord_id, twilio_number=to_number)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id, name="Riley")
    tenant_phone = await _tenant_phone(db_session, tenant_id)

    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid,
        from_number=tenant_phone,
        to_number=to_number,
        body="there is a fire in the kitchen",
    )

    try:
        # --- signed POST with a Tier-0 HARD keyword ---
        webhook_response = await _post_sms(params)
        assert webhook_response.status_code == 200
        assert webhook_response.text == "<Response/>"

        # --- durable artifacts the webhook wrote synchronously, before
        # returning its 200 ---
        message_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT party, tenant_id, prefilter FROM messages WHERE twilio_sid = :sid"
                    ),
                    {"sid": message_sid},
                )
            )
            .mappings()
            .one()
        )
        assert message_row["party"] == "tenant"
        assert str(message_row["tenant_id"]) == tenant_id
        assert message_row["prefilter"]["hard_hit"] is True
        assert "fire" in message_row["prefilter"]["categories"]

        emergency_call_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT channel, status, next_attempt_at, payload FROM notifications "
                        "WHERE landlord_id = :lid AND type = 'emergency_call'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert emergency_call_row["channel"] == "voice"
        assert emergency_call_row["status"] == "pending"  # ack pending -- chain not acked
        assert emergency_call_row["next_attempt_at"] is not None  # born enriched, sweep-visible
        assert emergency_call_row["payload"]["ack_token"]

        # emergency_call_attempt (T+0) already drained this row inline, so
        # its send-intent (emergency_sms) is already 'sent' by the time the
        # webhook responds -- see app/agent/emergency_chain.py's "instant +
        # durable sweep hybrid".
        emergency_sms_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT status FROM notifications "
                        "WHERE landlord_id = :lid AND type = 'emergency_sms'"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        assert emergency_sms_row["status"] == "sent"

        # --- T+0 landlord voice call attempted AND tenant safety SMS
        # recorded -- WITHOUT any approval step (nothing in this test ever
        # touches routers/drafts.py) ---
        landlord_calls = [
            c for c in fake_sender.calls if c.kind == "call" and c.to == landlord_phone
        ]
        assert len(landlord_calls) == 1, f"expected exactly one call, got {fake_sender.calls}"
        assert landlord_calls[0].from_ == to_number

        expected_category, expected_body = emergency_chain.render_tenant_safety_sms(["fire"])
        assert expected_category == "fire"
        tenant_sms_calls = [
            c for c in fake_sender.calls if c.kind == "sms" and c.to == tenant_phone
        ]
        assert len(tenant_sms_calls) == 1, f"expected exactly one text, got {fake_sender.calls}"
        assert tenant_sms_calls[0].body == expected_body
        assert tenant_sms_calls[0].from_ == to_number

        # --- audit trail contains emergency_triggered and the chain
        # attempt action (read the real action vocabulary, schema-v1.md) ---
        audit_rows = (
            (
                await db_session.execute(
                    text(
                        "SELECT action, actor, payload FROM audit_log "
                        "WHERE landlord_id = :lid ORDER BY id"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .all()
        )
        actions = [row["action"] for row in audit_rows]
        assert "emergency_triggered" in actions
        assert "emergency_call_attempt" in actions

        triggered_row = next(row for row in audit_rows if row["action"] == "emergency_triggered")
        assert triggered_row["actor"] == "prefilter"
        assert "fire" in triggered_row["payload"]["rules_fired"]

        attempt_row = next(row for row in audit_rows if row["action"] == "emergency_call_attempt")
        assert attempt_row["payload"]["step"] == 0
        attempt_actions = {a["action"]: a["status"] for a in attempt_row["payload"]["actions"]}
        assert attempt_actions["landlord_call"] == "sent"
        assert attempt_actions["tenant_safety_sms"] == "sent"

        # --- NO pending draft was required for the sends to fire: the
        # emergency artifacts/attempt are recorded strictly BEFORE this
        # SAME message's background graph run (enqueue_classification,
        # scheduled after the emergency chain already ran inline) could
        # ever produce a 'drafted' row -- proving the sends never waited
        # on, or depended on, a draft's existence. ---
        triggered_index = actions.index("emergency_triggered")
        attempt_index = actions.index("emergency_call_attempt")
        if "drafted" in actions:
            drafted_index = actions.index("drafted")
            assert drafted_index > triggered_index
            assert drafted_index > attempt_index

        # --- drive the scheduler tick too: with nothing else due yet
        # (T+2m is ~2 real minutes away), this must be a true no-op -- no
        # double call, no double text, proving the chain's claim discipline
        # holds even when the regular 60s ticker happens to run right on
        # top of the T+0 inline attempt. ---
        await scheduler_mod._run_one_tick()  # noqa: SLF001

        landlord_calls_after = [
            c for c in fake_sender.calls if c.kind == "call" and c.to == landlord_phone
        ]
        tenant_sms_calls_after = [
            c for c in fake_sender.calls if c.kind == "sms" and c.to == tenant_phone
        ]
        assert len(landlord_calls_after) == 1
        assert len(tenant_sms_calls_after) == 1
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Issue #61 AC #4 — the case-history ("LTB artifact") query: GET
# /v1/cases/{id}'s timeline must be a complete, ordered, human-readable
# account of one case's ENTIRE lifecycle, answerable by that ONE endpoint.
# Drives the real webhook -> graph (mocked Anthropic) -> approve -> send ->
# resolve path — never hand-seeded rows (unlike tests/test_cases_router.py's
# own timeline-shape regression test, which seeds directly).
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_case_timeline_complete_ordered_full_lifecycle(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #61 AC #4: webhook -> classify -> draft -> approve -> send ->
    resolve, then ``GET /v1/cases/{id}`` returns every expected
    ``audit``/``message``/``draft`` timeline entry, oldest-first,
    human-readable.

    The "resolve" leg drives ``app.agent.case_lifecycle.sweep_cases`` (the
    auto-stale leg) directly, the same way ``tests/test_agent_nodes.py``'s
    own ``test_sweep_cases_auto_stales_inactive_case_in_the_real_database``
    already does — there is no ``POST /v1/cases/{id}/resolve`` endpoint in
    this codebase yet. ``api-contracts.md`` documents the shape, but issue
    #55 explicitly scoped it out ("write endpoint, not part of #55's
    read-only scope") and no other issue has built it since (verified at
    #61 implementation time via a repo-wide grep — reported separately,
    not built here: a new REST endpoint is out of #61's own scope, "audit
    log writes", not "add a new endpoint"). The tenant-confirmed and
    auto-stale legs are the only two ``case_resolved`` writers that
    actually exist to drive end-to-end today.
    """
    monkeypatch.setattr(finalize_draft_decision_mod, "UNDO_WINDOW", timedelta(seconds=0))
    fake_sender = _FakeTwilioSender()
    set_twilio_sender_for_tests(fake_sender)
    _patch_client(monkeypatch, _happy_path_fake_messages(body="I'll send someone out today."))

    sub = str(uuid.uuid4())
    landlord_id = await _seed_landlord_with_auth(db_session, auth_user_id=sub)
    to_number = factories.fresh_phone()
    property_id = await factories.insert_property(db_session, landlord_id, twilio_number=to_number)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id, name="Riley")
    tenant_phone = await _tenant_phone(db_session, tenant_id)
    token = _mint_token(private_key, sub=sub)

    tenant_body = "the heat has been out since this morning"
    message_sid = f"SM{uuid.uuid4().hex}"
    params = _sms_params(
        message_sid=message_sid, from_number=tenant_phone, to_number=to_number, body=tenant_body
    )

    try:
        # --- signed POST to the Twilio SMS webhook ---
        webhook_response = await _post_sms(params)
        assert webhook_response.status_code == 200
        assert webhook_response.text == "<Response/>"

        # --- the background graph ran (mocked Anthropic): classified +
        # drafted, draft parks pending ---
        case_draft_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT c.id AS case_id, d.id AS draft_id, d.body AS draft_body "
                        "FROM cases c JOIN drafts d ON d.case_id = c.id "
                        "WHERE c.landlord_id = :lid"
                    ),
                    {"lid": landlord_id},
                )
            )
            .mappings()
            .one()
        )
        case_id = str(case_draft_row["case_id"])
        draft_id = str(case_draft_row["draft_id"])
        draft_body = case_draft_row["draft_body"]

        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                # --- POST /v1/drafts/{id}/approve (real endpoint) ---
                approve_response = await client.post(
                    f"/v1/drafts/{draft_id}/approve",
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert approve_response.status_code == 200, approve_response.text

        # --- drive app.scheduler._run_one_tick() past scheduled_send_at
        # (UNDO_WINDOW patched to 0s above) — the real send ---
        await scheduler_mod._run_one_tick()  # noqa: SLF001

        sent_message_row = (
            (
                await db_session.execute(
                    text("SELECT sent_message_id FROM drafts WHERE id = :id"), {"id": draft_id}
                )
            )
            .mappings()
            .one()
        )
        assert sent_message_row["sent_message_id"] is not None

        # --- resolve: the auto-stale sweep leg, far enough in the future
        # that this case's real (unmodified) last_activity_at clears the
        # 14-day threshold — see this test's own docstring for why this,
        # not a landlord-direct-resolve endpoint call, drives the leg. ---
        from app.agent.case_lifecycle import sweep_cases

        far_future = datetime.now(UTC) + timedelta(days=15)
        swept = await sweep_cases(now=far_future)
        assert case_id in {str(action.case_id) for action in swept}

        # --- the ONE query: GET /v1/cases/{id} ---
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                detail_response = await client.get(
                    f"/v1/cases/{case_id}", headers={"Authorization": f"Bearer {token}"}
                )
        assert detail_response.status_code == 200, detail_response.text
        detail = detail_response.json()

        assert detail["status"] == "resolved"
        assert detail["resolved_at"] is not None

        timeline = detail["timeline"]
        ats = [entry["at"] for entry in timeline]
        assert ats == sorted(ats), "timeline must be strictly oldest-first"

        # --- complete: the full, ordered action_log action sequence,
        # extending the same golden sequence
        # test_variant1_routine_urgent_loop_webhook_to_send already proves
        # (webhook -> send) with the resolve leg appended. ---
        audit_actions = [entry["action"] for entry in timeline if entry["kind"] == "audit"]
        assert audit_actions == [
            "message_received",
            "case_opened",
            "classified",
            "classified",
            "drafted",
            "approved",
            "sent",
            "case_resolved",
        ], f"complete ordered action sequence expected, got {audit_actions}"

        # --- the two `classified` rows share an action name, so the list
        # above cannot detect THEIR relative order regressing — which is the
        # exact tied-timestamp case cases.py's `ORDER BY a.created_at, a.id`
        # tie-break exists to pin (intent classification runs, and is
        # INSERTed, before severity classification). Assert it by payload
        # shape, not action name. ---
        classified_entries = [
            entry
            for entry in timeline
            if entry["kind"] == "audit" and entry["action"] == "classified"
        ]
        assert classified_entries[0]["payload"].get("kind") == "intent", (
            "intent-classified row must sort before severity-classified "
            f"(tie-break regression): {classified_entries[0]['payload']}"
        )
        assert "severity" in classified_entries[1]["payload"], (
            f"second classified row must be the severity one: {classified_entries[1]['payload']}"
        )

        # --- human-readable: the tenant's own words and the landlord's
        # actual reply both appear as real message bodies, in order. ---
        message_entries = [entry for entry in timeline if entry["kind"] == "message"]
        assert [m["body"] for m in message_entries] == [tenant_body, draft_body]
        assert message_entries[0]["direction"] == "inbound"
        assert message_entries[1]["direction"] == "outbound"

        # --- the one draft row, now `sent`. ---
        draft_entries = [entry for entry in timeline if entry["kind"] == "draft"]
        assert len(draft_entries) == 1
        assert draft_entries[0]["status"] == "sent"
        assert draft_entries[0]["body"] == draft_body

        # --- human-readable: the severity classification's margin-note
        # summary (schema-v1 v1.7) is present, not just the bare enum. ---
        severity_entry = next(
            entry
            for entry in timeline
            if entry["kind"] == "audit"
            and entry["action"] == "classified"
            and "severity" in entry["payload"]
        )
        assert severity_entry["payload"]["severity"] == "urgent"
        assert severity_entry["payload"]["summary"]
        assert severity_entry["payload"]["rules_fired"]

        # --- human-readable: the resolution carries WHY it resolved. ---
        resolved_entry = next(
            entry
            for entry in timeline
            if entry["kind"] == "audit" and entry["action"] == "case_resolved"
        )
        assert resolved_entry["payload"]["reason"] == "auto_stale"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# Issue #50 AC #3 — "full run visible as one LangSmith trace with readable
# reasoning_log". Credential-gated: see module docstring "LangSmith trace".
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason=(
        "Credential-gated (issue #50's STAGING variant, not this fake-based "
        "rehearsal): proving a run is visible as one LangSmith trace needs a "
        "real LANGCHAIN_API_KEY/LANGSMITH_API_KEY against a founder-provisioned "
        "LangSmith project, which does not exist in this environment (see "
        ".claude/skills/stoop-core-loop-campaign/SKILL.md, Phase 5, and "
        "app/observability.py::init_langsmith_tracing, already wired process-wide "
        "with no code change needed once the key exists). The orchestrator should "
        "schedule the real staging run (real Twilio + real Anthropic + this trace "
        "check) once those credentials are provisioned -- never faked here, since "
        "a trace that never touched a real LangSmith project would be a "
        "false-positive signal that this AC is proven when it isn't."
    )
)
def test_langsmith_trace_visibility_credential_gated() -> None:
    """Placeholder for issue #50 AC #3 — see the skip reason above."""
