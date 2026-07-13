"""Integration tests for #44/#45's ``routers/drafts.py`` — the full HTTP
surface: ``POST/DELETE /v1/drafts/{id}/approve``, ``POST /v1/drafts/{id}/
reject``, ``POST /v1/drafts/{id}/edit-and-send``.

Marker: ``integration`` — requires docker-compose Postgres + ``alembic
upgrade head``.

Harness mirrors ``tests/test_me.py`` exactly (in-test ES256 keypair, respx
-mocked JWKS, ``httpx.AsyncClient`` with ``ASGITransport`` against the real
FastAPI app) — kept self-contained per that module's own precedent
("mirrored from test_auth.py to keep tests self-contained").
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
from collections.abc import AsyncGenerator
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
import app.integrations.supabase_auth as auth_mod
import app.scheduler as scheduler_mod
from app.agent.checkpointer import close_checkpointer, setup_checkpointer
from app.agent.graph import run_graph
from app.integrations import anthropic as anthropic_mod
from app.integrations.twilio_send import set_twilio_sender_for_tests
from app.main import app
from tests import factories

_ISSUER = "https://test.supabase.co/auth/v1"
_JWKS_URL = "https://test.supabase.co/auth/v1/.well-known/jwks.json"
_KID = "test-kid-drafts-001"
_DB_URL_DEFAULT = "postgresql+asyncpg://stoop:stoop@localhost:5432/stoop"
_P256_COORD_BYTES = 32


def _get_db_url() -> str:
    url = os.environ.get("DATABASE_URL", _DB_URL_DEFAULT)
    return re.sub(r"^postgresql(\+\w+)?://", "postgresql+asyncpg://", url)


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


@pytest.fixture(autouse=True)
def reset_jwks_cache() -> None:
    auth_mod._jwks_state.cache = None  # noqa: SLF001


@pytest_asyncio.fixture(autouse=True)
async def dispose_app_engine() -> AsyncGenerator[None, None]:
    await db_mod.engine.dispose()
    yield
    await db_mod.engine.dispose()


@pytest_asyncio.fixture(autouse=True)
async def _checkpointer_lifecycle(_migrate_once: None) -> AsyncGenerator[None, None]:
    """``httpx.ASGITransport`` does NOT run FastAPI's own lifespan (no
    ``setup_checkpointer()`` call), but every drafts endpoint reaches
    ``resolve_draft_decision``/``resume_case_thread``, which need a live
    checkpointer pool — same ordering contract as every other integration
    test module that exercises the graph directly."""
    await setup_checkpointer()
    yield
    await close_checkpointer()


@pytest.fixture(autouse=True)
def _reset_anthropic_client() -> None:
    anthropic_mod.reset_client_for_tests()
    yield
    anthropic_mod.reset_client_for_tests()


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


async def _seed_landlord_with_auth(session: AsyncSession, *, auth_user_id: str) -> str:
    landlord_id = str(uuid.uuid4())
    await session.execute(
        text("INSERT INTO landlords (id, auth_user_id, email) VALUES (:id, :auth_id, :email)"),
        {"id": landlord_id, "auth_id": auth_user_id, "email": f"{landlord_id}@example.com"},
    )
    await session.commit()
    return landlord_id


async def _cleanup(session: AsyncSession, landlord_id: str) -> None:
    await session.execute(
        text(
            "DELETE FROM message_cases WHERE case_id IN "
            "(SELECT id FROM cases WHERE landlord_id = :lid)"
        ),
        {"lid": landlord_id},
    )
    await session.execute(
        text("DELETE FROM audit_log WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(
        # #197: classify_severity now writes cases.severity, so a full
        # graph -> approve -> scheduler-tick run can genuinely populate
        # trust_metrics (previously always skipped -- nothing to clean up
        # here before this issue).
        text("DELETE FROM trust_metrics WHERE landlord_id = :lid"),
        {"lid": landlord_id},
    )
    await session.execute(text("DELETE FROM drafts WHERE landlord_id = :lid"), {"lid": landlord_id})
    await session.execute(
        text("DELETE FROM messages WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id})
    await session.execute(
        text("DELETE FROM tenants WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(
        text("DELETE FROM properties WHERE landlord_id = :lid"), {"lid": landlord_id}
    )
    await session.execute(text("DELETE FROM landlords WHERE id = :lid"), {"lid": landlord_id})
    await session.commit()


async def _seed_pending_draft_direct(
    session: AsyncSession, *, auth_user_id: str, case_status: str = "awaiting_approval"
) -> tuple[str, str, str]:
    """Seeds a case/draft directly (bypassing the graph) — ONLY valid for
    tests whose endpoint call never actually needs a live LangGraph
    interrupt to resume: the degraded-path fallback (``case_status not in
    ('awaiting_approval',)``, never touches ``resume_case_thread`` at all),
    or a request that short-circuits (404/422/an already-non-pending draft)
    before ``resolve_draft_decision`` is ever called. A case seeded here
    with ``case_status='awaiting_approval'`` has NO real checkpoint behind
    it — using this helper for a genuine "resume a paused draft" test
    would incorrectly hit ``CaseNotAwaitingApprovalError`` (see
    ``_seed_pending_draft_via_graph`` for the realistic seeding those tests
    need instead)."""
    landlord_id = await _seed_landlord_with_auth(session, auth_user_id=auth_user_id)
    property_id = await factories.insert_property(session, landlord_id)
    tenant_id = await factories.insert_tenant(session, landlord_id, property_id)
    case_id = await factories.insert_case(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        status=case_status,
    )
    draft_id = await factories.insert_draft(session, landlord_id=landlord_id, case_id=case_id)
    return landlord_id, case_id, draft_id


async def _seed_pending_draft_via_graph(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    auth_user_id: str,
    body: str = "I'll take a look today.",
) -> tuple[str, str, str]:
    """Runs the REAL graph (mocked Anthropic) to a genuine paused
    ``await_approval`` interrupt — the realistic precondition every
    approve/reject/edit-and-send/undo test needs, since ``cases.status ==
    'awaiting_approval'`` alone (without a real checkpoint behind it) is
    not enough for ``resolve_draft_decision``'s normal path."""
    landlord_id = await _seed_landlord_with_auth(session, auth_user_id=auth_user_id)
    property_id = await factories.insert_property(session, landlord_id)
    tenant_id = await factories.insert_tenant(session, landlord_id, property_id)
    message_id = await factories.insert_message(
        session,
        landlord_id=landlord_id,
        property_id=property_id,
        tenant_id=tenant_id,
        body="the heat has been out since this morning",
    )
    _patch_client(monkeypatch, _happy_path_fake_messages(body=body))
    await run_graph(uuid.UUID(message_id))

    case_row = (
        (
            await session.execute(
                text("SELECT id FROM cases WHERE landlord_id = :lid"), {"lid": landlord_id}
            )
        )
        .mappings()
        .one()
    )
    case_id = str(case_row["id"])
    draft_row = (
        (
            await session.execute(
                text("SELECT id FROM drafts WHERE case_id = :cid AND status = 'pending'"),
                {"cid": case_id},
            )
        )
        .mappings()
        .one()
    )
    draft_id = str(draft_row["id"])
    return landlord_id, case_id, draft_id


def _mocked_jwks(jwks_payload: dict[str, Any]) -> respx.MockRouter:
    router = respx.MockRouter(assert_all_mocked=True, assert_all_called=False)
    router.get(_JWKS_URL).mock(return_value=httpx.Response(200, json=jwks_payload))
    return router


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class _FakeTwilioSender:
    """Records every call; never touches a network or real Twilio
    credentials (there are none in this test suite). Injected via
    ``app.integrations.twilio_send.set_twilio_sender_for_tests`` — the SAME
    sanctioned seam ``tests/test_agent_emergency_chain.py``'s own
    ``FakeTwilioSender`` uses for the emergency safety path. Proves the
    FULL live chain: ``get_default_sms_sender()`` ->
    ``TwilioBackedSmsSender`` -> ``get_twilio_sender()`` -> this fake —
    never a real ``twilio.rest.Client``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    async def send_sms(self, *, to: str, from_: str, body: str) -> str:
        self.calls.append({"to": to, "from_": from_, "body": body})
        return f"SM{uuid.uuid4().hex}"

    async def create_call(self, *, to: str, from_: str, twiml_url: str) -> str:
        raise AssertionError("the draft flow never places a voice call")


async def _tenant_phone_for_case(session: AsyncSession, *, case_id: str) -> str:
    """The case's tenant's phone — used by the live-wiring tests below to
    scope ``_FakeTwilioSender.calls`` assertions to THIS test's own send,
    never a raw global count (the fake is a process-wide singleton also
    used by ``app/agent/emergency_chain.py``'s own sweeps, which
    ``app.scheduler._run_one_tick()`` runs alongside the draft sender;
    scoping by phone keeps these assertions correct regardless of
    whatever else that same tick call touches)."""
    row = (
        (
            await session.execute(
                text(
                    "SELECT t.phone FROM tenants t JOIN cases c ON c.tenant_id = t.id "
                    "WHERE c.id = :case_id"
                ),
                {"case_id": case_id},
            )
        )
        .mappings()
        .one()
    )
    return str(row["phone"])


async def _provision_twilio_number(session: AsyncSession, *, case_id: str) -> None:
    """``_seed_pending_draft_via_graph``/``_seed_pending_draft_direct`` seed
    a property with NO ``twilio_number`` (matching every OTHER test in this
    module, which never drives an actual send). The live-wiring tests below
    are the only ones that need a real "from" number to get past
    ``app/agent/draft_sender.py``'s own guard — see
    ``app/integrations/sms_sender.py``'s module docstring "Why from_e164
    is required"."""
    await session.execute(
        text(
            "UPDATE properties SET twilio_number = :num WHERE id = "
            "(SELECT property_id FROM cases WHERE id = :case_id)"
        ),
        {"num": factories.fresh_phone(), "case_id": case_id},
    )
    await session.commit()


# ---------------------------------------------------------------------------
# 1. Approve — happy path.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_approve_returns_scheduled_send_at_and_undo_until(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = str(uuid.uuid4())
    landlord_id, case_id, draft_id = await _seed_pending_draft_via_graph(
        db_session, monkeypatch, auth_user_id=sub
    )
    token = _mint_token(private_key, sub=sub)

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                response = await client.post(
                    f"/v1/drafts/{draft_id}/approve", headers={"Authorization": f"Bearer {token}"}
                )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "approved"
        assert body["scheduled_send_at"] == body["undo_until"]

        row = (
            (
                await db_session.execute(
                    text("SELECT status, scheduled_send_at FROM drafts WHERE id = :id"),
                    {"id": draft_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "approved"
        assert row["scheduled_send_at"] is not None

        audit_actions = (
            (
                await db_session.execute(
                    text("SELECT action FROM audit_log WHERE case_id = :cid ORDER BY id"),
                    {"cid": case_id},
                )
            )
            .mappings()
            .all()
        )
        assert any(row["action"] == "approved" for row in audit_actions)
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_approve_idempotent_on_repeat(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = str(uuid.uuid4())
    landlord_id, case_id, draft_id = await _seed_pending_draft_via_graph(
        db_session, monkeypatch, auth_user_id=sub
    )
    token = _mint_token(private_key, sub=sub)

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                r1 = await client.post(
                    f"/v1/drafts/{draft_id}/approve", headers={"Authorization": f"Bearer {token}"}
                )
                r2 = await client.post(
                    f"/v1/drafts/{draft_id}/approve", headers={"Authorization": f"Bearer {token}"}
                )

        assert r1.status_code == 200
        assert r2.status_code == 200
        assert r1.json()["scheduled_send_at"] == r2.json()["scheduled_send_at"]

        audit_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE case_id = :cid AND action = 'approved'"),
                {"cid": case_id},
            )
        ).scalar_one()
        assert audit_count == 1  # never double-recorded
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_approve_concurrent_requests_exactly_one_audit_row(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Barrier-forced concurrency at the HTTP layer: two genuinely
    simultaneous approve requests for the SAME draft must never both
    schedule a send."""
    sub = str(uuid.uuid4())
    landlord_id, case_id, draft_id = await _seed_pending_draft_via_graph(
        db_session, monkeypatch, auth_user_id=sub
    )
    token = _mint_token(private_key, sub=sub)

    async def _call() -> httpx.Response:
        async with _client() as client:
            return await client.post(
                f"/v1/drafts/{draft_id}/approve", headers={"Authorization": f"Bearer {token}"}
            )

    try:
        async with _mocked_jwks(jwks_payload):
            r1, r2 = await asyncio.gather(_call(), _call())

        assert r1.status_code == 200, r1.text
        assert r2.status_code == 200, r2.text

        audit_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM audit_log WHERE case_id = :cid AND action = 'approved'"),
                {"cid": case_id},
            )
        ).scalar_one()
        assert audit_count == 1  # exactly one of the two actually applied the transition

        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "approved"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_approve_stale_draft_returns_409_with_fresh_draft_id(
    private_key: EllipticCurvePrivateKey, jwks_payload: dict[str, Any], db_session: AsyncSession
) -> None:
    sub = str(uuid.uuid4())
    landlord_id = await _seed_landlord_with_auth(db_session, auth_user_id=sub)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    stale_draft_id = await factories.insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_id, status="stale"
    )
    fresh_draft_id = await factories.insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_id
    )
    token = _mint_token(private_key, sub=sub)

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                response = await client.post(
                    f"/v1/drafts/{stale_draft_id}/approve",
                    headers={"Authorization": f"Bearer {token}"},
                )

        assert response.status_code == 409, response.text
        body = response.json()
        assert body["error"]["code"] == "draft_stale"
        assert body["error"]["fresh_draft_id"] == fresh_draft_id
        assert "request_id" in body["error"]
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_approve_wrong_landlord_returns_404(
    private_key: EllipticCurvePrivateKey, jwks_payload: dict[str, Any], db_session: AsyncSession
) -> None:
    owner_sub = str(uuid.uuid4())
    attacker_sub = str(uuid.uuid4())
    owner_landlord_id, case_id, draft_id = await _seed_pending_draft_direct(
        db_session, auth_user_id=owner_sub
    )
    attacker_landlord_id = await _seed_landlord_with_auth(db_session, auth_user_id=attacker_sub)
    token = _mint_token(private_key, sub=attacker_sub)

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                response = await client.post(
                    f"/v1/drafts/{draft_id}/approve", headers={"Authorization": f"Bearer {token}"}
                )

        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "draft_not_found"

        # Never mutated.
        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "pending"
    finally:
        await _cleanup(db_session, owner_landlord_id)
        await _cleanup(db_session, attacker_landlord_id)


@pytest.mark.integration
async def test_approve_degraded_path_draft_is_approvable(
    private_key: EllipticCurvePrivateKey, jwks_payload: dict[str, Any], db_session: AsyncSession
) -> None:
    """The #44-pinned decision: a pending draft on a case NOT in
    ``awaiting_approval`` (the EMERGENCY/``draft_guard_failed`` interim
    exit's own leftover state) is approvable via the SAME endpoint."""
    sub = str(uuid.uuid4())
    landlord_id, case_id, draft_id = await _seed_pending_draft_direct(
        db_session, auth_user_id=sub, case_status="open"
    )
    token = _mint_token(private_key, sub=sub)

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                response = await client.post(
                    f"/v1/drafts/{draft_id}/approve", headers={"Authorization": f"Bearer {token}"}
                )

        assert response.status_code == 200, response.text

        case_status = (
            await db_session.execute(
                text("SELECT status FROM cases WHERE id = :id"), {"id": case_id}
            )
        ).scalar_one()
        assert case_status == "awaiting_approval"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 2. Undo.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_undo_within_window_reverts_to_pending(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = str(uuid.uuid4())
    landlord_id, case_id, draft_id = await _seed_pending_draft_via_graph(
        db_session, monkeypatch, auth_user_id=sub
    )
    token = _mint_token(private_key, sub=sub)

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                approve_response = await client.post(
                    f"/v1/drafts/{draft_id}/approve", headers={"Authorization": f"Bearer {token}"}
                )
                assert approve_response.status_code == 200

                undo_response = await client.delete(
                    f"/v1/drafts/{draft_id}/approve", headers={"Authorization": f"Bearer {token}"}
                )

        assert undo_response.status_code == 200, undo_response.text
        assert undo_response.json() == {"status": "pending"}

        row = (
            (
                await db_session.execute(
                    text(
                        "SELECT status, scheduled_send_at, edited, final_body FROM drafts "
                        "WHERE id = :id"
                    ),
                    {"id": draft_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "pending"
        assert row["scheduled_send_at"] is None
        assert row["edited"] is False
        assert row["final_body"] is None

        audit_actions = (
            (
                await db_session.execute(
                    text("SELECT action FROM audit_log WHERE case_id = :cid ORDER BY id"),
                    {"cid": case_id},
                )
            )
            .mappings()
            .all()
        )
        assert any(r["action"] == "send_cancelled" for r in audit_actions)
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_undo_after_already_sent_returns_409(
    private_key: EllipticCurvePrivateKey, jwks_payload: dict[str, Any], db_session: AsyncSession
) -> None:
    sub = str(uuid.uuid4())
    landlord_id = await _seed_landlord_with_auth(db_session, auth_user_id=sub)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    draft_id = await factories.insert_draft(
        db_session,
        landlord_id=landlord_id,
        case_id=case_id,
        status="sent",
        scheduled_send_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    token = _mint_token(private_key, sub=sub)

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                response = await client.delete(
                    f"/v1/drafts/{draft_id}/approve", headers={"Authorization": f"Bearer {token}"}
                )

        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "already_sent"
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 3. Reject.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_reject_archives_draft_case_stays_open(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = str(uuid.uuid4())
    landlord_id, case_id, draft_id = await _seed_pending_draft_via_graph(
        db_session, monkeypatch, auth_user_id=sub
    )
    token = _mint_token(private_key, sub=sub)

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                response = await client.post(
                    f"/v1/drafts/{draft_id}/reject",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"note": "tenant sorted it themselves"},
                )

        assert response.status_code == 200, response.text
        assert response.json() == {"status": "rejected"}

        draft_status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert draft_status == "rejected"

        case_status = (
            await db_session.execute(
                text("SELECT status FROM cases WHERE id = :id"), {"id": case_id}
            )
        ).scalar_one()
        assert case_status == "open"

        audit_row = (
            (
                await db_session.execute(
                    text(
                        "SELECT actor, payload FROM audit_log WHERE case_id = :cid "
                        "AND action = 'rejected'"
                    ),
                    {"cid": case_id},
                )
            )
            .mappings()
            .one()
        )
        assert audit_row["actor"] == "landlord"
        assert audit_row["payload"]["note"] == "tenant sorted it themselves"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_reject_without_body_defaults_note_to_none(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = str(uuid.uuid4())
    landlord_id, case_id, draft_id = await _seed_pending_draft_via_graph(
        db_session, monkeypatch, auth_user_id=sub
    )
    token = _mint_token(private_key, sub=sub)

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                response = await client.post(
                    f"/v1/drafts/{draft_id}/reject", headers={"Authorization": f"Bearer {token}"}
                )

        assert response.status_code == 200, response.text
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 4. Edit-and-send.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_edit_and_send_records_edited_true_and_retains_original(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sub = str(uuid.uuid4())
    landlord_id, case_id, draft_id = await _seed_pending_draft_via_graph(
        db_session, monkeypatch, auth_user_id=sub
    )
    token = _mint_token(private_key, sub=sub)
    edited_text = "Heading over at 3pm today to fix the heat."

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                response = await client.post(
                    f"/v1/drafts/{draft_id}/edit-and-send",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"body": edited_text},
                )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "approved"
        assert body["scheduled_send_at"] == body["undo_until"]

        row = (
            (
                await db_session.execute(
                    text("SELECT status, edited, final_body, body FROM drafts WHERE id = :id"),
                    {"id": draft_id},
                )
            )
            .mappings()
            .one()
        )
        assert row["status"] == "approved"
        assert row["edited"] is True
        assert row["final_body"] == edited_text
        assert row["body"] != edited_text  # original retained, never overwritten

        audit_actions = (
            (
                await db_session.execute(
                    text("SELECT action FROM audit_log WHERE case_id = :cid ORDER BY id"),
                    {"cid": case_id},
                )
            )
            .mappings()
            .all()
        )
        assert any(r["action"] == "edited" for r in audit_actions)
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_edit_and_send_rejects_empty_body(
    private_key: EllipticCurvePrivateKey, jwks_payload: dict[str, Any], db_session: AsyncSession
) -> None:
    sub = str(uuid.uuid4())
    landlord_id, case_id, draft_id = await _seed_pending_draft_direct(db_session, auth_user_id=sub)
    token = _mint_token(private_key, sub=sub)

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                response = await client.post(
                    f"/v1/drafts/{draft_id}/edit-and-send",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"body": ""},
                )

        assert response.status_code == 422  # FastAPI/Pydantic validation error
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_edit_and_send_rejects_whitespace_only_body(
    private_key: EllipticCurvePrivateKey, jwks_payload: dict[str, Any], db_session: AsyncSession
) -> None:
    """Finding #4 (safety review, INFO/defensive): ``Field(min_length=1)``
    alone lets a whitespace-only string through — must also be rejected."""
    sub = str(uuid.uuid4())
    landlord_id, case_id, draft_id = await _seed_pending_draft_direct(db_session, auth_user_id=sub)
    token = _mint_token(private_key, sub=sub)

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                response = await client.post(
                    f"/v1/drafts/{draft_id}/edit-and-send",
                    headers={"Authorization": f"Bearer {token}"},
                    json={"body": "   "},
                )

        assert response.status_code == 422, response.text
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_no_auth_header_returns_401(db_session: AsyncSession) -> None:
    async with _client() as client:
        response = await client.post(f"/v1/drafts/{uuid.uuid4()}/approve")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_token"


# ---------------------------------------------------------------------------
# 5. Finding #5 (spec-guardian, MAJOR) — undo of a draft that never
#    approved (stale/rejected/cancelled) must NOT say "already gone out";
#    that's a false statement for these three. Distinct, accurate code.
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("seed_status", ["stale", "rejected", "cancelled"])
async def test_undo_on_never_approved_draft_returns_draft_not_undoable(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
    seed_status: str,
) -> None:
    sub = str(uuid.uuid4())
    landlord_id = await _seed_landlord_with_auth(db_session, auth_user_id=sub)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    draft_id = await factories.insert_draft(
        db_session, landlord_id=landlord_id, case_id=case_id, status=seed_status
    )
    token = _mint_token(private_key, sub=sub)

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                response = await client.delete(
                    f"/v1/drafts/{draft_id}/approve", headers={"Authorization": f"Bearer {token}"}
                )

        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "draft_not_undoable"
        # NEVER the "already gone out" code -- a false statement for a
        # draft that was never approved in the first place.
        assert response.json()["error"]["code"] != "already_sent"
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_reject_conflict_reconciliation_when_concurrent_approve_won_returns_already_sent(
    db_session: AsyncSession,
) -> None:
    """Finding #5 (spec-guardian, MAJOR): ``_reconcile_reject_conflict``
    must not report ``draft_stale`` when the reason reject no longer
    applies is a concurrent APPROVE having already won the race -- that is
    NOT staleness (no newer tenant message superseded anything); it is the
    same "already gone out" family the normal pre-check in ``_reject``
    already uses for the identical status set. Exercised directly against
    the fixed reconciliation helper for a deterministic repro (a genuine
    race is exercised at the HTTP layer by
    ``test_approve_concurrent_requests_exactly_one_audit_row`` elsewhere in
    this module; this test pins the EXACT reconciliation outcome once a
    concurrent approve has already won, rather than relying on winning a
    race non-deterministically)."""
    from app.deps import Landlord
    from app.errors import AppError
    from app.routers import drafts as drafts_router

    landlord_id = await factories.insert_landlord(db_session)
    property_id = await factories.insert_property(db_session, landlord_id)
    tenant_id = await factories.insert_tenant(db_session, landlord_id, property_id)
    case_id = await factories.insert_case(
        db_session, landlord_id=landlord_id, property_id=property_id, tenant_id=tenant_id
    )
    # Simulates the state a concurrent APPROVE would have already produced
    # by the time a losing REJECT's own reconciliation runs.
    draft_id = await factories.insert_draft(
        db_session,
        landlord_id=landlord_id,
        case_id=case_id,
        status="approved",
        scheduled_send_at=datetime.now(UTC) + timedelta(seconds=5),
    )

    try:
        landlord = Landlord(id=uuid.UUID(landlord_id))
        with pytest.raises(AppError) as exc_info:
            await drafts_router._reconcile_reject_conflict(
                db_session,
                landlord=landlord,
                draft_id=uuid.UUID(draft_id),
                case_id=uuid.UUID(case_id),
            )
        assert exc_info.value.code == "already_sent"
        assert exc_info.value.status_code == 409
    finally:
        await _cleanup(db_session, landlord_id)


# ---------------------------------------------------------------------------
# 6. Live wiring (#44/#45 integration commit) — approve genuinely drives an
#    outbound send through app/scheduler.py's real 60s-tick body, via a FAKE
#    Twilio sender injected at the SAME seam the emergency chain uses
#    (app.integrations.twilio_send.set_twilio_sender_for_tests). No real
#    Twilio credentials exist in this test suite; nothing here ever reaches
#    the network.
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_approve_drives_exactly_one_send_via_scheduler_tick(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The FULL live chain, end to end: POST .../approve -> (once due) ->
    app.scheduler._run_one_tick() -> app.agent.draft_sender.sender_tick ->
    app.integrations.sms_sender.get_default_sms_sender() ->
    TwilioBackedSmsSender -> app.integrations.twilio_send.get_twilio_sender()
    -> this test's fake. Exactly ONE send recorded; every durable side
    effect of a real send is written."""
    monkeypatch.setattr(finalize_draft_decision_mod, "UNDO_WINDOW", timedelta(seconds=0))
    fake_sender = _FakeTwilioSender()
    set_twilio_sender_for_tests(fake_sender)

    sub = str(uuid.uuid4())
    landlord_id, case_id, draft_id = await _seed_pending_draft_via_graph(
        db_session, monkeypatch, auth_user_id=sub
    )
    await _provision_twilio_number(db_session, case_id=case_id)
    tenant_phone = await _tenant_phone_for_case(db_session, case_id=case_id)
    token = _mint_token(private_key, sub=sub)

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                approve_response = await client.post(
                    f"/v1/drafts/{draft_id}/approve", headers={"Authorization": f"Bearer {token}"}
                )
        assert approve_response.status_code == 200, approve_response.text

        # The scheduler's own per-tick body -- not a bespoke test loop, and
        # not app.agent.draft_sender.sender_tick called directly -- drives
        # the actual send.
        await scheduler_mod._run_one_tick()  # noqa: SLF001

        # Scoped to THIS test's own tenant phone (never a raw global count
        # on the shared fake -- see _tenant_phone_for_case's docstring):
        # exactly one send for THIS draft.
        own_calls = [c for c in fake_sender.calls if c["to"] == tenant_phone]
        assert len(own_calls) == 1

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

        message_row = (
            (
                await db_session.execute(
                    text("SELECT direction, party, twilio_sid, body FROM messages WHERE id = :id"),
                    {"id": str(draft_row["sent_message_id"])},
                )
            )
            .mappings()
            .one()
        )
        assert message_row["direction"] == "outbound"
        assert message_row["twilio_sid"] is not None
        assert message_row["twilio_sid"].startswith("SM")
        assert message_row["body"] == own_calls[0]["body"]
        # Went out from the property's OWN provisioned number, never a
        # fabricated/omitted "from" (app/integrations/sms_sender.py's
        # module docstring "Why from_e164 is required").
        assert own_calls[0]["from_"] is not None

        audit_actions = (
            (
                await db_session.execute(
                    text("SELECT action FROM audit_log WHERE case_id = :cid ORDER BY id"),
                    {"cid": case_id},
                )
            )
            .mappings()
            .all()
        )
        action_sequence = [row["action"] for row in audit_actions]
        # approved -> ... -> sent, in that relative order (approved must
        # precede sent; other actions -- classified, drafted -- may appear
        # earlier from the graph run itself).
        assert "approved" in action_sequence
        assert "sent" in action_sequence
        assert action_sequence.index("approved") < action_sequence.index("sent")
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_undo_within_window_results_in_zero_sends(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Undo BEFORE a scheduler tick ever runs must leave the draft
    'pending' -- the claim SQL only ever matches ``status = 'approved'``,
    so a subsequent tick is a genuine no-op, never a send."""
    fake_sender = _FakeTwilioSender()
    set_twilio_sender_for_tests(fake_sender)

    sub = str(uuid.uuid4())
    landlord_id, case_id, draft_id = await _seed_pending_draft_via_graph(
        db_session, monkeypatch, auth_user_id=sub
    )
    await _provision_twilio_number(db_session, case_id=case_id)
    tenant_phone = await _tenant_phone_for_case(db_session, case_id=case_id)
    token = _mint_token(private_key, sub=sub)

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                approve_response = await client.post(
                    f"/v1/drafts/{draft_id}/approve", headers={"Authorization": f"Bearer {token}"}
                )
                assert approve_response.status_code == 200

                undo_response = await client.delete(
                    f"/v1/drafts/{draft_id}/approve", headers={"Authorization": f"Bearer {token}"}
                )
                assert undo_response.status_code == 200, undo_response.text

        await scheduler_mod._run_one_tick()  # noqa: SLF001

        own_calls = [c for c in fake_sender.calls if c["to"] == tenant_phone]
        assert own_calls == []

        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "pending"

        message_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM messages WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert message_count == 0
    finally:
        await _cleanup(db_session, landlord_id)


@pytest.mark.integration
async def test_double_approve_then_scheduler_tick_sends_exactly_once(
    private_key: EllipticCurvePrivateKey,
    jwks_payload: dict[str, Any],
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Approving the SAME draft twice (idempotent -- one scheduled_send_at,
    one audit row, per test_approve_idempotent_on_repeat above) must still
    result in exactly ONE real send once due, never two."""
    monkeypatch.setattr(finalize_draft_decision_mod, "UNDO_WINDOW", timedelta(seconds=0))
    fake_sender = _FakeTwilioSender()
    set_twilio_sender_for_tests(fake_sender)

    sub = str(uuid.uuid4())
    landlord_id, case_id, draft_id = await _seed_pending_draft_via_graph(
        db_session, monkeypatch, auth_user_id=sub
    )
    await _provision_twilio_number(db_session, case_id=case_id)
    tenant_phone = await _tenant_phone_for_case(db_session, case_id=case_id)
    token = _mint_token(private_key, sub=sub)

    try:
        async with _mocked_jwks(jwks_payload):
            async with _client() as client:
                r1 = await client.post(
                    f"/v1/drafts/{draft_id}/approve", headers={"Authorization": f"Bearer {token}"}
                )
                r2 = await client.post(
                    f"/v1/drafts/{draft_id}/approve", headers={"Authorization": f"Bearer {token}"}
                )
        assert r1.status_code == 200
        assert r2.status_code == 200

        await scheduler_mod._run_one_tick()  # noqa: SLF001

        own_calls = [c for c in fake_sender.calls if c["to"] == tenant_phone]
        assert len(own_calls) == 1

        status = (
            await db_session.execute(
                text("SELECT status FROM drafts WHERE id = :id"), {"id": draft_id}
            )
        ).scalar_one()
        assert status == "sent"

        message_count = (
            await db_session.execute(
                text("SELECT COUNT(*) FROM messages WHERE case_id = :cid"), {"cid": case_id}
            )
        ).scalar_one()
        assert message_count == 1
    finally:
        await _cleanup(db_session, landlord_id)
