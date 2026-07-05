"""Tests for ``verify_request_engine_role_separation`` (#22 safety review
item 13b) — the FastAPI startup self-check that proves ``APP_DATABASE_URL``
actually provides RLS role separation, rather than merely being set.

Marker: ``unit`` — no real DB needed. ``request_engine``/``engine`` are
monkeypatched with fakes returning canned rows, exactly as requested
("test with monkeypatched engines/fake rows; don't require a real second
role locally"). Behavioral proof against a REAL second role lives in
``tests/test_rls_isolation.py`` (which needs docker-compose Postgres);
this file is pure logic — given canned rows from each engine, does the
function raise exactly when it should.

Item 14 fix pinned here: comparing the request role's SERVER-reported
``current_user`` against the admin engine's SERVER-reported
``current_user`` (both via a real query), not against the admin engine's
CLIENT-SIDE connection-string username (``engine.url.username``) — under
Supavisor the client-side username is ``role.project-ref`` (e.g.
``postgres.abcdef123``) while Postgres itself reports the bare role
(``postgres``) as ``current_user``, so the old comparison would never
match even when it genuinely IS the same role. See
``test_raises_when_request_role_matches_admin_role`` below, which is
deliberately shaped to fail against that old (buggy) comparison and pass
against the fix.
"""

from __future__ import annotations

import pytest

import app.db.session as db_mod

# ---------------------------------------------------------------------------
# Fakes — no real asyncpg connection anywhere in this file.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, row: tuple[object, ...]) -> None:
        self._row = row

    def one(self) -> tuple[object, ...]:
        return self._row


class _FakeConnection:
    def __init__(self, row: tuple[object, ...]) -> None:
        self._row = row

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
        return _FakeResult(self._row)


class _FakeUrl:
    def __init__(self, username: str) -> None:
        self.username = username


class _FakeEngine:
    """Stands in for either ``request_engine`` or ``engine`` (the admin
    engine) — ``.connect()`` returns a fake connection whose one query
    always returns ``row``, regardless of the SQL text passed in.

    ``url_username`` is independently configurable (present for realism,
    and so the fix can be proven NOT to read it for the role comparison
    any more — only ``.connect()``'s server-reported row matters now).
    """

    def __init__(self, row: tuple[object, ...], *, url_username: str | None = None) -> None:
        self._row = row
        if url_username is not None:
            self.url = _FakeUrl(url_username)

    def connect(self) -> _FakeConnection:
        return _FakeConnection(self._row)


class _NeverConnectEngine:
    """Blows up if ``.connect()`` is ever called — proves the unset-
    ``app_database_url`` path short-circuits before touching either
    engine at all."""

    def connect(self) -> object:
        raise AssertionError("connect() must never be called when app_database_url is unset")


class _FailingConnection:
    """Simulates a connection-level failure (bad password / network) —
    raises on ``__aenter__``, exactly where a real asyncpg connect()
    failure would surface via ``async with engine.connect() as conn:``."""

    async def __aenter__(self) -> _FailingConnection:
        raise ConnectionRefusedError("simulated connection failure (should never leak verbatim)")

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _FailingEngine:
    def connect(self) -> _FailingConnection:
        return _FailingConnection()


# ---------------------------------------------------------------------------
# 1. app_database_url unset — no-op, never touches either engine.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_noop_when_app_database_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db_mod.settings, "app_database_url", None)
    monkeypatch.setattr(db_mod, "request_engine", _NeverConnectEngine())
    monkeypatch.setattr(db_mod, "engine", _NeverConnectEngine())

    await db_mod.verify_request_engine_role_separation()  # must not raise


# ---------------------------------------------------------------------------
# 2. app_database_url set — the two failure modes.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_raises_when_request_role_has_bypassrls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Even a genuinely DIFFERENT role fails the check if it holds
    BYPASSRLS — exactly the live-Supabase ``postgres``/``service_role``
    situation the migration's "LIVE ROLE FACTS" section documents."""
    monkeypatch.setattr(db_mod.settings, "app_database_url", "postgresql+asyncpg://x:y@h:6543/db")
    monkeypatch.setattr(db_mod, "request_engine", _FakeEngine(("service_role", True)))
    monkeypatch.setattr(db_mod, "engine", _FakeEngine(("postgres",)))

    with pytest.raises(db_mod.RoleSeparationVerificationError):
        await db_mod.verify_request_engine_role_separation()


@pytest.mark.unit
async def test_raises_when_request_role_matches_admin_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Realistic Supavisor shapes (#22 safety review item 14): the admin
    engine's CLIENT-SIDE connection-string username is ``role.project-ref``
    (``postgres.abcdef123``), but its SERVER-reported ``current_user`` is
    the bare role (``postgres``). A copy-paste error
    (``APP_DATABASE_URL == DATABASE_URL``) makes the request engine report
    that SAME bare ``current_user``.

    This must be caught by comparing server-reported identity to
    server-reported identity. The OLD (buggy) implementation compared the
    request engine's server-reported ``current_user`` against the ADMIN
    engine's CLIENT-SIDE ``url.username`` instead — ``"postgres" !=
    "postgres.abcdef123"`` — and would NOT have raised here, silently
    missing exactly this real-world mistake. This test fails against that
    old comparison and passes against the fix (mutation-check: swap the
    fixed implementation's ``admin_row[0]`` for ``engine.url.username`` and
    this test goes red).
    """
    monkeypatch.setattr(db_mod.settings, "app_database_url", "postgresql+asyncpg://x:y@h:6543/db")
    monkeypatch.setattr(db_mod, "request_engine", _FakeEngine(("postgres", False)))
    monkeypatch.setattr(
        db_mod, "engine", _FakeEngine(("postgres",), url_username="postgres.abcdef123")
    )

    with pytest.raises(db_mod.RoleSeparationVerificationError):
        await db_mod.verify_request_engine_role_separation()


@pytest.mark.unit
async def test_raises_message_and_log_never_contain_connection_string(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Never-break rule #5: the raised exception's message must not leak
    the connection string, host, or password — only role names."""
    fake_url = "postgresql+asyncpg://app_role:supersecretpassword@realhost:6543/db"
    monkeypatch.setattr(db_mod.settings, "app_database_url", fake_url)
    monkeypatch.setattr(db_mod, "request_engine", _FakeEngine(("service_role", True)))
    monkeypatch.setattr(db_mod, "engine", _FakeEngine(("postgres",)))

    with pytest.raises(db_mod.RoleSeparationVerificationError) as exc_info:
        await db_mod.verify_request_engine_role_separation()

    message = str(exc_info.value)
    assert "supersecretpassword" not in message
    assert "realhost" not in message
    assert fake_url not in message


# ---------------------------------------------------------------------------
# 3. app_database_url set, genuinely separated — passes silently.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_passes_when_request_role_is_separated_and_not_bypassrls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db_mod.settings, "app_database_url", "postgresql+asyncpg://x:y@h:6543/db")
    monkeypatch.setattr(db_mod, "request_engine", _FakeEngine(("app_role", False)))
    monkeypatch.setattr(
        db_mod, "engine", _FakeEngine(("postgres",), url_username="postgres.abcdef123")
    )

    await db_mod.verify_request_engine_role_separation()  # must not raise


# ---------------------------------------------------------------------------
# 4. Connection-level failures — wrapped, labeled, secret-free (item 16).
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_request_engine_connection_failure_reraises_labeled_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db_mod.settings, "app_database_url", "postgresql+asyncpg://x:y@h:6543/db")
    monkeypatch.setattr(db_mod, "request_engine", _FailingEngine())
    monkeypatch.setattr(db_mod, "engine", _FakeEngine(("postgres",)))

    with pytest.raises(db_mod.RoleSeparationVerificationError) as exc_info:
        await db_mod.verify_request_engine_role_separation()

    message = str(exc_info.value)
    assert "could not connect" in message.lower()
    assert "simulated connection failure" not in message


@pytest.mark.unit
async def test_admin_engine_connection_failure_reraises_labeled_and_secret_free(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(db_mod.settings, "app_database_url", "postgresql+asyncpg://x:y@h:6543/db")
    monkeypatch.setattr(db_mod, "request_engine", _FakeEngine(("app_role", False)))
    monkeypatch.setattr(db_mod, "engine", _FailingEngine())

    with pytest.raises(db_mod.RoleSeparationVerificationError) as exc_info:
        await db_mod.verify_request_engine_role_separation()

    message = str(exc_info.value)
    assert "could not connect" in message.lower()
    assert "simulated connection failure" not in message


# ---------------------------------------------------------------------------
# 5. Wiring pin — app.main's lifespan actually calls this, not just defines it.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_lifespan_calls_verify_request_engine_role_separation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin: ``app.main``'s ``lifespan`` must actually invoke
    ``verify_request_engine_role_separation`` at startup — a future edit
    that only defines the check without wiring it into ``create_app()``
    would otherwise silently never run it.

    Also pins that ``setup_checkpointer()`` (#24) runs AFTER it,
    unconditionally — faked away here (not a real DB call) since this test
    is only about the WIRING, not either function's own behaviour (each has
    its own dedicated tests: this file's role-separation tests, and
    ``tests/test_checkpointer.py``'s integration round trip).
    """
    order: list[str] = []

    async def _fake_verify() -> None:
        order.append("verify")

    async def _fake_setup_checkpointer() -> None:
        order.append("checkpointer")

    monkeypatch.setattr("app.main.verify_request_engine_role_separation", _fake_verify)
    monkeypatch.setattr("app.main.setup_checkpointer", _fake_setup_checkpointer)

    from app.main import _lifespan
    from app.main import app as fastapi_app

    async with _lifespan(fastapi_app):
        pass

    # Order matters: the role-separation gate must pass BEFORE the
    # checkpointer opens its pool — a swapped ordering would boot the
    # graph infrastructure on a misconfigured deployment.
    assert order == ["verify", "checkpointer"]
