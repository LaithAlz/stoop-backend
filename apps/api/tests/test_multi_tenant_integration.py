"""Multi-tenant integration test (issue #64) — two landlords, full loops,
zero bleed, proven at the ENDPOINT layer.

Scope — the gap this closes
----------------------------------------------------------------------
``tests/test_rls_isolation_matrix.py`` (#23) and ``tests/
test_rls_isolation.py`` (#22) already prove, exhaustively, that RLS itself
denies cross-tenant SQL as ``app_role`` — a STATIC, per-table proof, run
directly against raw connections with ``SET LOCAL ROLE``. Every existing
per-router cross-tenant regression (``tests/test_drafts_router.py::
test_approve_wrong_landlord_returns_404``, ``tests/test_cases_router.py::
test_cross_tenant_case_access_returns_404``, ``tests/test_devices_router.py::
test_cross_tenant_unregister_returns_404_and_leaves_row_untouched``, ``tests/
test_queue_router.py::test_queue_scoped_by_landlord_cross_tenant_isolation``,
``tests/test_cases_resolve_router.py::test_resolve_case_cross_tenant_
returns_404``) either calls the router's own Python function directly with a
hand-built ``(Landlord, session)`` tuple (bypassing ``require_landlord``'s
real two-session GUC-setting bootstrap and the HTTP/JWT layer entirely), or
seeds its scenario by hand rather than driving the real webhook -> graph ->
approve -> send loop.

Issue #64's own AC asks for something neither of those proves: TWO
landlords, each running the REAL, FULL SMS loop (signed Twilio webhook ->
graph -> queue -> approve -> scheduler send), CONCURRENTLY, through the
REAL HTTP endpoints with each landlord's OWN minted JWT (the real
``require_landlord`` path, not a fabricated tuple) — proving that landlord A
can never see, approve, reject, edit-and-send, resolve, or read landlord
B's case/draft/queue/device/cost data through the ACTUAL endpoints, even
under genuine concurrency. This module is that end-to-end proof, not a
replacement for the matrix or the per-router regressions above.

Marker: ``integration``. Dedicated database: ``stoop_mtest`` (never
``stoop``/``stoop_e2e``/``stoop_drafts``/``stoop_prov`` — this module's own
database, matching the campaign's per-module DB-name discipline).

ZERO real network, mirroring ``tests/test_e2e_core_loop.py``'s own harness
exactly (duplicated, not imported, per this codebase's "each integration
test module is self-contained" convention):
- **Twilio SMS webhook signing** — real HMAC-SHA1 request signature via
  ``app.integrations.twilio.compute_signature``.
- **Twilio SMS/voice sends** — ``_FakeTwilioSender``, injected via
  ``app.integrations.twilio_send.set_twilio_sender_for_tests`` — the exact
  same sanctioned seam ``app/agent/draft_sender.py`` funnels every real send
  through. Nothing here ever constructs a real ``twilio.rest.Client``.
- **Anthropic** — ``app.integrations.anthropic.get_client`` monkeypatched to
  a ROUTING fake client (``_RoutedFakeMessages``, below) that inspects each
  call's own ``user_content`` (every one of classify_intent's/
  classify_severity's/draft_response's own ``_build_user_content`` embeds
  the tenant's raw message body verbatim) to decide which landlord's own
  canned-response queue to pop from. This is what makes it SAFE to run both
  landlords' webhook requests via genuine ``asyncio.gather`` concurrency
  against ONE shared fake client: a call belonging to landlord A's graph run
  can never accidentally consume a response queued for landlord B, no
  matter how the two runs interleave on the event loop.
- **Weather (Open-Meteo)** — never invoked: neither seeded property has
  ``lat``/``lon`` set (``app/agent/nodes/load_context.py`` skips the lookup
  entirely when either is unset).

Golden-test discipline: every assertion below reads real DB state or a real
endpoint response body — never a tautology. A dropped ``landlord_id``
predicate ANYWHERE in the routers/queries this test exercises should make
some assertion here fail.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import subprocess
import sys
import time
import uuid
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from uuid import UUID

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
from app.agent.checkpointer import close_checkpointer, setup_checkpointer
from app.config import settings
from app.cost_reporting import cost_per_case, cost_per_month, cost_per_property
from app.db.session import get_session
from app.deps import require_landlord
from app.integrations import anthropic as anthropic_mod
from app.integrations.supabase_auth import verify_jwt
from app.integrations.twilio import compute_signature
from app.integrations.twilio_send import set_twilio_sender_for_tests
from app.main import app
from tests import factories

# ---------------------------------------------------------------------------
# DB / migration harness — this module's own dedicated database
# (``stoop_mtest``); mirrors tests/test_e2e_core_loop.py exactly except the
# fallback DB name.
# ---------------------------------------------------------------------------

_DB_URL_DEFAULT = "postgresql+asyncpg://stoop:stoop@localhost:5432/stoop_mtest"


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
    """Same rationale as every other integration test module: the app's own
    module-level admin engine (backing ``get_admin_session``/``get_session``
    fallback, which the webhook / graph nodes / scheduler sweeps / the
    ``require_landlord`` calls below all use) must not carry pooled
    connections across event loops between tests."""
    await db_mod.engine.dispose()
    yield
    await db_mod.engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _checkpointer_lifecycle(_migrate_once: None) -> AsyncGenerator[None, None]:
    """The graph the webhook's background task invokes always compiles the
    case-scoped graph WITH the Postgres checkpointer (``app/agent/
    graph.py``) — same ordering contract as every other integration test
    module that exercises the graph directly."""
    await setup_checkpointer()
    yield
    await close_checkpointer()


@pytest.fixture(autouse=True)
def _reset_anthropic_client() -> Iterator[None]:
    anthropic_mod.reset_client_for_tests()
    yield
    anthropic_mod.reset_client_for_tests()


# ---------------------------------------------------------------------------
# JWT / JWKS harness — mirrors tests/test_e2e_core_loop.py exactly. ONE
# keypair/kid mints tokens for BOTH landlords (different ``sub`` claims) —
# same convention tests/test_drafts_router.py's own
# ``test_approve_wrong_landlord_returns_404`` already uses for its
# owner/attacker pair.
# ---------------------------------------------------------------------------

_ISSUER = "https://test.supabase.co/auth/v1"
_JWKS_URL = "https://test.supabase.co/auth/v1/.well-known/jwks.json"
_KID = "test-kid-mtest-001"
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


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ---------------------------------------------------------------------------
# Twilio SMS webhook signing — mirrors tests/test_e2e_core_loop.py exactly.
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
# Fake Anthropic client — ROUTES on the tenant message body embedded in
# every call's own user_content, so genuinely concurrent (asyncio.gather)
# landlord A / landlord B graph runs can never cross-pop each other's canned
# response regardless of interleaving order. See module docstring.
# ---------------------------------------------------------------------------


def _fake_message(*, tool_input: dict[str, Any], tool_name: str) -> SimpleNamespace:
    block = ToolUseBlock(id="toolu_test", input=tool_input, name=tool_name, type="tool_use")
    usage = SimpleNamespace(input_tokens=150, output_tokens=40)
    return SimpleNamespace(content=[block], usage=usage, model="claude-sonnet-5")


def _canned_responses(
    *, intent_summary: str, severity: str, rules_fired: list[str], draft_body: str
) -> list[Any]:
    """Three canned tool-use responses, popped in call order: classify_intent
    -> classify_severity -> draft_response — same shape ``tests/
    test_e2e_core_loop.py``'s own ``_happy_path_fake_messages`` uses."""
    return [
        _fake_message(
            tool_input={"intent": "maintenance", "is_new_issue": True, "summary": intent_summary},
            tool_name="classify_intent",
        ),
        _fake_message(
            tool_input={
                "severity": severity,
                "rules_fired": rules_fired,
                "modifier": None,
                "refusal_flags": [],
                "reasoning": [f"Reasoning for: {intent_summary}."],
            },
            tool_name="classify_severity",
        ),
        _fake_message(
            tool_input={"body": draft_body, "refusal_templates_used": []},
            tool_name="draft_message",
        ),
    ]


@dataclass
class _RoutedFakeMessages:
    """``.create()`` inspects the call's ``messages`` content for each
    registered marker substring and pops the next canned response from THAT
    marker's own queue — never a single shared list. A call matching no
    registered marker is a hard test bug (never silently falls through)."""

    routes: dict[str, list[Any]]

    async def create(self, **kwargs: Any) -> Any:
        messages_param = kwargs.get("messages") or []
        content = " ".join(str(m.get("content", "")) for m in messages_param)
        for marker, queue in self.routes.items():
            if marker in content:
                assert queue, f"no more canned Anthropic responses queued for marker {marker!r}"
                return queue.pop(0)
        raise AssertionError(f"no matching fake-Anthropic route for call content: {content!r}")


class _RoutedFakeClient:
    def __init__(self, messages: _RoutedFakeMessages) -> None:
        self.messages = messages


# ---------------------------------------------------------------------------
# Fake Twilio sender — same seam tests/test_e2e_core_loop.py's own
# _FakeTwilioSender uses; nothing here ever touches a real twilio.rest.Client.
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
# Seeding / cleanup helpers — self-contained per this codebase's convention.
# ---------------------------------------------------------------------------


async def _seed_landlord_with_auth(session: AsyncSession, *, auth_user_id: str) -> str:
    landlord_id = str(uuid.uuid4())
    await session.execute(
        text("INSERT INTO landlords (id, auth_user_id, email) VALUES (:id, :auth_id, :email)"),
        {"id": landlord_id, "auth_id": auth_user_id, "email": f"{landlord_id}@example.com"},
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


async def _fetch_case_and_pending_draft(session: AsyncSession, landlord_id: str) -> dict[str, Any]:
    row = (
        (
            await session.execute(
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
    return dict(row)


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    """FK-safe delete order (children before parents) — plain superuser
    DELETE, exactly ``tests/test_e2e_core_loop.py``'s own convention,
    extended with ``push_outbox``/``push_tokens`` since this module also
    registers devices."""
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
    await session.execute(text("DELETE FROM push_outbox WHERE landlord_id = :lid"), params)
    await session.execute(text("DELETE FROM push_tokens WHERE landlord_id = :lid"), params)
    await session.execute(text("DELETE FROM tenants WHERE landlord_id = :lid"), params)
    await session.execute(text("DELETE FROM properties WHERE landlord_id = :lid"), params)
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), params)
    await session.commit()


# ---------------------------------------------------------------------------
# The scenario.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_two_landlords_full_loop_concurrent_zero_cross_tenant_bleed(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #64's own three ACs, all proven in one continuous, realistic
    scenario (never hand-seeded past the initial landlord/property/tenant
    rows — every case/draft/send below is produced by the REAL webhook ->
    graph -> approve -> scheduler loop):

    1. Two landlords, each with their own property/tenant, run the full SMS
       loop CONCURRENTLY (``asyncio.gather`` — genuine event-loop
       concurrency, not merely sequential calls in one test body).
    2. No cross-tenant data visible at any endpoint: GET /v1/queue,
       GET /v1/cases/{id}, POST /v1/drafts/{id}/{approve,reject,
       edit-and-send}, POST /v1/cases/{id}/resolve, POST+DELETE /v1/devices
       — every one of them proven 404/disjoint for the WRONG landlord's own
       JWT, both directions (A->B and B->A), with the underlying row
       verified UNCHANGED after each rejected attempt. Cost rollups
       (``app/cost_reporting.py`` — no REST endpoint exists for these yet,
       verified by grep; exercised directly here via the REAL
       ``require_landlord`` bootstrap, never the admin engine) never
       include the other landlord's cost.
    3. Twilio number routing: each inbound message's persisted
       ``landlord_id``/``property_id``/``tenant_id`` matches the number it
       was actually addressed to — never swapped, even though both
       requests raced through the SAME webhook handler concurrently.
    """
    monkeypatch.setattr(finalize_draft_decision_mod, "UNDO_WINDOW", timedelta(seconds=0))
    fake_sender = _FakeTwilioSender()
    set_twilio_sender_for_tests(fake_sender)

    # Distinctive, non-overlapping substrings of each landlord's OWN tenant
    # message — used by the routed fake client to identify which landlord a
    # given Anthropic call belongs to, regardless of interleaving order.
    marker_a = "the heat has been out"
    marker_b = "leak under the kitchen sink"
    body_a = f"{marker_a} since last night"
    body_b = f"there's a small {marker_b}"
    draft_body_a = "I'll send someone out today for the heat -- landlord A here."
    draft_body_b = "Thanks for the heads up, I'll get a plumber out this week -- landlord B here."

    routed_messages = _RoutedFakeMessages(
        routes={
            marker_a: _canned_responses(
                intent_summary="Heat is out",
                severity="URGENT",
                rules_fired=["No heat, mild weather"],
                draft_body=draft_body_a,
            ),
            marker_b: _canned_responses(
                intent_summary="Leaky sink",
                severity="ROUTINE",
                rules_fired=["Minor plumbing issue"],
                draft_body=draft_body_b,
            ),
        }
    )
    monkeypatch.setattr(anthropic_mod, "get_client", lambda: _RoutedFakeClient(routed_messages))

    # --- seed: two fully independent landlords ---
    sub_a = str(uuid.uuid4())
    sub_b = str(uuid.uuid4())
    landlord_a = await _seed_landlord_with_auth(db_session, auth_user_id=sub_a)
    landlord_b = await _seed_landlord_with_auth(db_session, auth_user_id=sub_b)
    to_number_a = factories.fresh_phone()
    to_number_b = factories.fresh_phone()
    property_a = await factories.insert_property(db_session, landlord_a, twilio_number=to_number_a)
    property_b = await factories.insert_property(db_session, landlord_b, twilio_number=to_number_b)
    tenant_a = await factories.insert_tenant(db_session, landlord_a, property_a, name="Riley")
    tenant_b = await factories.insert_tenant(db_session, landlord_b, property_b, name="Sam")
    tenant_phone_a = await _tenant_phone(db_session, tenant_a)
    tenant_phone_b = await _tenant_phone(db_session, tenant_b)
    token_a = _mint_token(private_key, sub=sub_a)
    token_b = _mint_token(private_key, sub=sub_b)

    sid_a = f"SM{uuid.uuid4().hex}"
    sid_b = f"SM{uuid.uuid4().hex}"
    params_a = _sms_params(
        message_sid=sid_a, from_number=tenant_phone_a, to_number=to_number_a, body=body_a
    )
    params_b = _sms_params(
        message_sid=sid_b, from_number=tenant_phone_b, to_number=to_number_b, body=body_b
    )

    try:
        # =====================================================================
        # Phase 1 — both tenants text in AT THE SAME TIME (genuine asyncio
        # concurrency, not sequential calls) -- issue #64 AC #1.
        # =====================================================================
        webhook_response_a, webhook_response_b = await asyncio.gather(
            _post_sms(params_a), _post_sms(params_b)
        )
        assert webhook_response_a.status_code == 200
        assert webhook_response_a.text == "<Response/>"
        assert webhook_response_b.status_code == 200
        assert webhook_response_b.text == "<Response/>"

        # --- Twilio number routing never misattributes a message across
        # landlords, even though both requests raced through the SAME
        # webhook handler concurrently -- issue #64 AC #3. ---
        message_row_a = (
            (
                await db_session.execute(
                    text(
                        "SELECT landlord_id, property_id, tenant_id, party, prefilter "
                        "FROM messages WHERE twilio_sid = :sid"
                    ),
                    {"sid": sid_a},
                )
            )
            .mappings()
            .one()
        )
        message_row_b = (
            (
                await db_session.execute(
                    text(
                        "SELECT landlord_id, property_id, tenant_id, party, prefilter "
                        "FROM messages WHERE twilio_sid = :sid"
                    ),
                    {"sid": sid_b},
                )
            )
            .mappings()
            .one()
        )
        assert str(message_row_a["landlord_id"]) == landlord_a
        assert str(message_row_a["property_id"]) == property_a
        assert str(message_row_a["tenant_id"]) == tenant_a
        assert str(message_row_b["landlord_id"]) == landlord_b
        assert str(message_row_b["property_id"]) == property_b
        assert str(message_row_b["tenant_id"]) == tenant_b
        # Never crossed: A's message is never attributed to B's identifiers
        # and vice versa (the direct, non-tautological form of the check).
        assert str(message_row_a["landlord_id"]) != landlord_b
        assert str(message_row_b["landlord_id"]) != landlord_a
        # Belt-and-braces: neither message was a Tier-0 hard fire (would
        # otherwise derail the golden action sequence asserted later).
        assert message_row_a["prefilter"]["hard_hit"] is False
        assert message_row_b["prefilter"]["hard_hit"] is False

        # --- each landlord's own graph run produced exactly its own
        # case+pending-draft -- scoped by landlord_id. ---
        case_draft_a = await _fetch_case_and_pending_draft(db_session, landlord_a)
        case_draft_b = await _fetch_case_and_pending_draft(db_session, landlord_b)
        assert case_draft_a["case_status"] == "awaiting_approval"
        assert case_draft_b["case_status"] == "awaiting_approval"
        case_id_a = str(case_draft_a["case_id"])
        case_id_b = str(case_draft_b["case_id"])
        draft_id_a = str(case_draft_a["draft_id"])
        draft_id_b = str(case_draft_b["draft_id"])
        assert case_draft_a["draft_body"] == draft_body_a
        assert case_draft_b["draft_body"] == draft_body_b

        async with (
            _mocked_jwks(jwks_payload),
            _client() as client_a,
            _client() as client_b,
        ):
            # =================================================================
            # Phase 2 — GET /v1/queue, CONCURRENTLY, each with its OWN JWT
            # (the real require_landlord bootstrap): disjoint queues -- issue
            # #64 AC #2 ("queues ... fully separated").
            # =================================================================
            queue_resp_a, queue_resp_b = await asyncio.gather(
                client_a.get("/v1/queue", headers=_auth_headers(token_a)),
                client_b.get("/v1/queue", headers=_auth_headers(token_b)),
            )
            assert queue_resp_a.status_code == 200, queue_resp_a.text
            assert queue_resp_b.status_code == 200, queue_resp_b.text
            queue_a = queue_resp_a.json()
            queue_b = queue_resp_b.json()

            assert [item["case_id"] for item in queue_a["items"]] == [case_id_a]
            card_a = queue_a["items"][0]
            assert card_a["draft_id"] == draft_id_a
            assert card_a["draft_body"] == draft_body_a
            assert card_a["severity"] == "urgent"
            assert card_a["tenant_message"] == body_a
            assert queue_a["counts"] == {
                "total": 1,
                "emergency": 0,
                "urgent": 1,
                "routine": 0,
                "awaiting_tenant": 0,
            }

            assert [item["case_id"] for item in queue_b["items"]] == [case_id_b]
            card_b = queue_b["items"][0]
            assert card_b["draft_id"] == draft_id_b
            assert card_b["draft_body"] == draft_body_b
            assert card_b["severity"] == "routine"
            assert card_b["tenant_message"] == body_b
            assert queue_b["counts"] == {
                "total": 1,
                "emergency": 0,
                "urgent": 0,
                "routine": 1,
                "awaiting_tenant": 0,
            }

            queue_case_ids_a = {item["case_id"] for item in queue_a["items"]}
            queue_case_ids_b = {item["case_id"] for item in queue_b["items"]}
            assert queue_case_ids_a.isdisjoint(queue_case_ids_b)

            # =================================================================
            # Phase 3 — cross-tenant negative checks, BOTH directions, on the
            # still-pending drafts / still-open cases / not-yet-registered
            # devices -- issue #64 AC #2.
            # =================================================================

            # --- 3a. drafts: approve -- neither landlord can approve the
            # other's draft. ---
            resp = await client_a.post(
                f"/v1/drafts/{draft_id_b}/approve", headers=_auth_headers(token_a)
            )
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"]["code"] == "draft_not_found"
            resp = await client_b.post(
                f"/v1/drafts/{draft_id_a}/approve", headers=_auth_headers(token_b)
            )
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"]["code"] == "draft_not_found"

            # --- 3b. drafts: reject -- same, both directions. ---
            resp = await client_a.post(
                f"/v1/drafts/{draft_id_b}/reject", headers=_auth_headers(token_a)
            )
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"]["code"] == "draft_not_found"
            resp = await client_b.post(
                f"/v1/drafts/{draft_id_a}/reject", headers=_auth_headers(token_b)
            )
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"]["code"] == "draft_not_found"

            # --- 3c. drafts: edit-and-send -- same, both directions; the
            # attacker's "sneaky" body must never land anywhere. ---
            resp = await client_a.post(
                f"/v1/drafts/{draft_id_b}/edit-and-send",
                json={"body": "sneaky rewrite attempt"},
                headers=_auth_headers(token_a),
            )
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"]["code"] == "draft_not_found"
            resp = await client_b.post(
                f"/v1/drafts/{draft_id_a}/edit-and-send",
                json={"body": "sneaky rewrite attempt"},
                headers=_auth_headers(token_b),
            )
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"]["code"] == "draft_not_found"

            # --- both drafts are completely untouched by every attempt
            # above: still pending, still their OWN original body, never
            # edited, never the attacker's body. ---
            draft_row_a = (
                (
                    await db_session.execute(
                        text("SELECT status, body, edited, final_body FROM drafts WHERE id = :id"),
                        {"id": draft_id_a},
                    )
                )
                .mappings()
                .one()
            )
            draft_row_b = (
                (
                    await db_session.execute(
                        text("SELECT status, body, edited, final_body FROM drafts WHERE id = :id"),
                        {"id": draft_id_b},
                    )
                )
                .mappings()
                .one()
            )
            assert draft_row_a["status"] == "pending"
            assert draft_row_a["body"] == draft_body_a
            assert draft_row_a["edited"] is False
            assert draft_row_a["final_body"] is None
            assert draft_row_b["status"] == "pending"
            assert draft_row_b["body"] == draft_body_b
            assert draft_row_b["edited"] is False
            assert draft_row_b["final_body"] is None

            # --- 3d. cases: GET detail -- neither landlord can read the
            # other's case, both directions; positive control proves the
            # 404 is genuine ownership scoping, not a blanket bug. ---
            resp = await client_a.get(f"/v1/cases/{case_id_b}", headers=_auth_headers(token_a))
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"]["code"] == "case_not_found"
            resp = await client_b.get(f"/v1/cases/{case_id_a}", headers=_auth_headers(token_b))
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"]["code"] == "case_not_found"

            resp = await client_a.get(f"/v1/cases/{case_id_a}", headers=_auth_headers(token_a))
            assert resp.status_code == 200, resp.text
            assert resp.json()["id"] == case_id_a
            resp = await client_b.get(f"/v1/cases/{case_id_b}", headers=_auth_headers(token_b))
            assert resp.status_code == 200, resp.text
            assert resp.json()["id"] == case_id_b

            # --- 3e. cases: resolve -- neither landlord can resolve the
            # other's case, both directions; neither case is actually
            # resolved by any of these attempts. ---
            resp = await client_a.post(
                f"/v1/cases/{case_id_b}/resolve", headers=_auth_headers(token_a)
            )
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"]["code"] == "case_not_found"
            resp = await client_b.post(
                f"/v1/cases/{case_id_a}/resolve", headers=_auth_headers(token_b)
            )
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"]["code"] == "case_not_found"

            case_status_a = (
                await db_session.execute(
                    text("SELECT status FROM cases WHERE id = :id"), {"id": case_id_a}
                )
            ).scalar_one()
            case_status_b = (
                await db_session.execute(
                    text("SELECT status FROM cases WHERE id = :id"), {"id": case_id_b}
                )
            ).scalar_one()
            assert case_status_a == "awaiting_approval"
            assert case_status_b == "awaiting_approval"

            # --- 3f. devices: register + cross-tenant unregister 404, both
            # directions; positive control proves each landlord CAN manage
            # their own device. ---
            device_resp_a = await client_a.post(
                "/v1/devices",
                json={"token": f"ExponentPushToken[{uuid.uuid4()}]", "platform": "ios"},
                headers=_auth_headers(token_a),
            )
            assert device_resp_a.status_code == 201, device_resp_a.text
            device_id_a = device_resp_a.json()["id"]

            device_resp_b = await client_b.post(
                "/v1/devices",
                json={"token": f"ExponentPushToken[{uuid.uuid4()}]", "platform": "android"},
                headers=_auth_headers(token_b),
            )
            assert device_resp_b.status_code == 201, device_resp_b.text
            device_id_b = device_resp_b.json()["id"]

            resp = await client_a.delete(
                f"/v1/devices/{device_id_b}", headers=_auth_headers(token_a)
            )
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"]["code"] == "device_not_found"
            resp = await client_b.delete(
                f"/v1/devices/{device_id_a}", headers=_auth_headers(token_b)
            )
            assert resp.status_code == 404, resp.text
            assert resp.json()["error"]["code"] == "device_not_found"

            device_owner_a = (
                await db_session.execute(
                    text("SELECT landlord_id FROM push_tokens WHERE id = :id"), {"id": device_id_a}
                )
            ).scalar_one()
            device_owner_b = (
                await db_session.execute(
                    text("SELECT landlord_id FROM push_tokens WHERE id = :id"), {"id": device_id_b}
                )
            ).scalar_one()
            assert str(device_owner_a) == landlord_a
            assert str(device_owner_b) == landlord_b

            # Positive control: each landlord deletes their OWN device fine.
            resp = await client_a.delete(
                f"/v1/devices/{device_id_a}", headers=_auth_headers(token_a)
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "deleted"
            resp = await client_b.delete(
                f"/v1/devices/{device_id_b}", headers=_auth_headers(token_b)
            )
            assert resp.status_code == 200, resp.text
            assert resp.json()["status"] == "deleted"

            # =================================================================
            # Phase 4 — concurrent approve, each with its own JWT -- the
            # two-session require_landlord bootstrap must scope each request
            # to its own landlord with no bleed between concurrent-ish
            # requests.
            # =================================================================
            approve_resp_a, approve_resp_b = await asyncio.gather(
                client_a.post(f"/v1/drafts/{draft_id_a}/approve", headers=_auth_headers(token_a)),
                client_b.post(f"/v1/drafts/{draft_id_b}/approve", headers=_auth_headers(token_b)),
            )
            assert approve_resp_a.status_code == 200, approve_resp_a.text
            assert approve_resp_a.json()["status"] == "approved"
            assert approve_resp_b.status_code == 200, approve_resp_b.text
            assert approve_resp_b.json()["status"] == "approved"

        # =====================================================================
        # Phase 5 — one shared scheduler tick drains BOTH landlords' due
        # drafts (the production ticker is cluster-wide, never landlord
        # -scoped -- see tests/conftest.py's own note): exactly one send per
        # tenant, never crossed.
        # =====================================================================
        await scheduler_mod._run_one_tick()  # noqa: SLF001

        calls_to_a = [c for c in fake_sender.calls if c.to == tenant_phone_a]
        calls_to_b = [c for c in fake_sender.calls if c.to == tenant_phone_b]
        assert len(calls_to_a) == 1, f"expected exactly one send to A's tenant: {fake_sender.calls}"
        assert len(calls_to_b) == 1, f"expected exactly one send to B's tenant: {fake_sender.calls}"
        assert calls_to_a[0].kind == "sms"
        assert calls_to_a[0].from_ == to_number_a
        assert calls_to_a[0].body == draft_body_a
        assert calls_to_b[0].kind == "sms"
        assert calls_to_b[0].from_ == to_number_b
        assert calls_to_b[0].body == draft_body_b
        # A's send never references B's tenant/body and vice versa.
        cross_a = [
            c for c in fake_sender.calls if c.to == tenant_phone_a and c.body != draft_body_a
        ]
        cross_b = [
            c for c in fake_sender.calls if c.to == tenant_phone_b and c.body != draft_body_b
        ]
        assert not cross_a
        assert not cross_b
        # No stray third send anywhere.
        assert len(fake_sender.calls) == 2, fake_sender.calls

        draft_row_a_after = (
            (
                await db_session.execute(
                    text("SELECT status, sent_message_id FROM drafts WHERE id = :id"),
                    {"id": draft_id_a},
                )
            )
            .mappings()
            .one()
        )
        draft_row_b_after = (
            (
                await db_session.execute(
                    text("SELECT status, sent_message_id FROM drafts WHERE id = :id"),
                    {"id": draft_id_b},
                )
            )
            .mappings()
            .one()
        )
        assert draft_row_a_after["status"] == "sent"
        assert draft_row_a_after["sent_message_id"] is not None
        assert draft_row_b_after["status"] == "sent"
        assert draft_row_b_after["sent_message_id"] is not None

        case_status_a_after = (
            await db_session.execute(
                text("SELECT status FROM cases WHERE id = :id"), {"id": case_id_a}
            )
        ).scalar_one()
        case_status_b_after = (
            await db_session.execute(
                text("SELECT status FROM cases WHERE id = :id"), {"id": case_id_b}
            )
        ).scalar_one()
        assert case_status_a_after == "awaiting_tenant"
        assert case_status_b_after == "awaiting_tenant"

        # =====================================================================
        # Phase 6 — post-send GET /v1/queue: both cards gone (awaiting_tenant,
        # not awaiting_approval), each landlord's own awaiting_tenant count is
        # exactly 1, never contaminated by the other's.
        # =====================================================================
        async with (
            _mocked_jwks(jwks_payload),
            _client() as client_a,
            _client() as client_b,
        ):
            post_send_resp_a, post_send_resp_b = await asyncio.gather(
                client_a.get("/v1/queue", headers=_auth_headers(token_a)),
                client_b.get("/v1/queue", headers=_auth_headers(token_b)),
            )
        assert post_send_resp_a.json()["items"] == []
        assert post_send_resp_a.json()["counts"]["awaiting_tenant"] == 1
        assert post_send_resp_b.json()["items"] == []
        assert post_send_resp_b.json()["counts"]["awaiting_tenant"] == 1

        # =====================================================================
        # Phase 7 — audit trail: the complete, ordered, landlord-scoped
        # action sequence per landlord (same golden sequence tests/
        # test_e2e_core_loop.py's own variant-1 proves for a single
        # landlord), plus an explicit cross-tenant scoping check on every
        # row's own case_id.
        # =====================================================================
        expected_actions = [
            "message_received",
            "case_opened",
            "classified",
            "classified",
            "drafted",
            "approved",
            "sent",
        ]
        audit_rows_a = (
            (
                await db_session.execute(
                    text(
                        "SELECT action, case_id FROM audit_log WHERE landlord_id = :lid ORDER BY id"
                    ),
                    {"lid": landlord_a},
                )
            )
            .mappings()
            .all()
        )
        audit_rows_b = (
            (
                await db_session.execute(
                    text(
                        "SELECT action, case_id FROM audit_log WHERE landlord_id = :lid ORDER BY id"
                    ),
                    {"lid": landlord_b},
                )
            )
            .mappings()
            .all()
        )
        assert [r["action"] for r in audit_rows_a] == expected_actions
        assert [r["action"] for r in audit_rows_b] == expected_actions
        # Every row's own case_id (where set) belongs to THIS landlord's own
        # case, never the other's.
        assert all(r["case_id"] is None or str(r["case_id"]) == case_id_a for r in audit_rows_a)
        assert all(r["case_id"] is None or str(r["case_id"]) == case_id_b for r in audit_rows_b)

        # =====================================================================
        # Phase 8 — cost rollups (app/cost_reporting.py) never bleed across
        # landlords. No REST endpoint exists for these yet (verified: no
        # app/routers/*.py references app.cost_reporting) -- exercised
        # directly here via the REAL require_landlord bootstrap (own
        # authenticated session), never the admin engine, per this test's own
        # hard rule.
        # =====================================================================
        async with _mocked_jwks(jwks_payload):
            auth_user_a = await verify_jwt(token_a)
            auth_user_b = await verify_jwt(token_b)

        async with asynccontextmanager(get_session)() as session_a:
            resolved_landlord_a, scoped_session_a = await require_landlord(
                user=auth_user_a, session=session_a
            )
            assert str(resolved_landlord_a.id) == landlord_a

            case_cost_a = await cost_per_case(
                scoped_session_a, landlord_id=resolved_landlord_a.id, case_id=UUID(case_id_a)
            )
            cross_case_cost_a = await cost_per_case(
                scoped_session_a, landlord_id=resolved_landlord_a.id, case_id=UUID(case_id_b)
            )
            property_cost_a = await cost_per_property(
                scoped_session_a, landlord_id=resolved_landlord_a.id, property_id=UUID(property_a)
            )
            cross_property_cost_a = await cost_per_property(
                scoped_session_a, landlord_id=resolved_landlord_a.id, property_id=UUID(property_b)
            )
            month_cost_a = await cost_per_month(
                scoped_session_a, landlord_id=resolved_landlord_a.id
            )

        async with asynccontextmanager(get_session)() as session_b:
            resolved_landlord_b, scoped_session_b = await require_landlord(
                user=auth_user_b, session=session_b
            )
            assert str(resolved_landlord_b.id) == landlord_b

            case_cost_b = await cost_per_case(
                scoped_session_b, landlord_id=resolved_landlord_b.id, case_id=UUID(case_id_b)
            )
            cross_case_cost_b = await cost_per_case(
                scoped_session_b, landlord_id=resolved_landlord_b.id, case_id=UUID(case_id_a)
            )
            property_cost_b = await cost_per_property(
                scoped_session_b, landlord_id=resolved_landlord_b.id, property_id=UUID(property_b)
            )
            cross_property_cost_b = await cost_per_property(
                scoped_session_b, landlord_id=resolved_landlord_b.id, property_id=UUID(property_a)
            )
            month_cost_b = await cost_per_month(
                scoped_session_b, landlord_id=resolved_landlord_b.id
            )

        # A's own case/property/month rollups are real (both LLM and SMS
        # cost components present, never fabricated-zero).
        assert case_cost_a.llm_cost_cents > 0
        assert case_cost_a.sms_cost_cents > 0
        assert case_cost_b.llm_cost_cents > 0
        assert case_cost_b.sms_cost_cents > 0

        # A's landlord_id scoped against B's case/property id: zero, never
        # B's real (nonzero) cost leaking through a dropped predicate.
        assert cross_case_cost_a.total_cost_cents == 0
        assert cross_property_cost_a.total_cost_cents == 0
        assert cross_case_cost_b.total_cost_cents == 0
        assert cross_property_cost_b.total_cost_cents == 0

        # Only one case/property exists per landlord in this scenario, so a
        # CORRECTLY-scoped property/month rollup must equal that one case's
        # own cost exactly. If a landlord_id predicate were ever dropped
        # anywhere in the shared cost_reporting CTE, A's month rollup would
        # silently double (B's identically-shaped cost bleeding in) instead
        # -- this is a decisive, non-tautological check, not merely
        # "greater than zero".
        assert property_cost_a.total_cost_cents == case_cost_a.total_cost_cents
        assert sum(m.total_cost_cents for m in month_cost_a) == case_cost_a.total_cost_cents
        assert property_cost_b.total_cost_cents == case_cost_b.total_cost_cents
        assert sum(m.total_cost_cents for m in month_cost_b) == case_cost_b.total_cost_cents
    finally:
        await _cleanup(db_session, landlord_a)
        await _cleanup(db_session, landlord_b)
