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

import app.db.session as db_mod
import app.integrations.supabase_auth as auth_mod
from app.agent.checkpointer import close_checkpointer, setup_checkpointer
from app.agent.graph import run_graph
from app.integrations import anthropic as anthropic_mod
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
async def test_no_auth_header_returns_401(db_session: AsyncSession) -> None:
    async with _client() as client:
        response = await client.post(f"/v1/drafts/{uuid.uuid4()}/approve")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "missing_token"
