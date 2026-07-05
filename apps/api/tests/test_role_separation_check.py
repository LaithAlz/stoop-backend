"""Tests for ``verify_request_engine_role_separation`` (#22 safety review
item 13b) — the FastAPI startup self-check that proves ``APP_DATABASE_URL``
actually provides RLS role separation, rather than merely being set.

Marker: ``unit`` — no real DB needed. ``request_engine``/``engine`` are
monkeypatched with fakes returning canned rows, exactly as the coordinator
asked ("test with monkeypatched engines/fake rows; don't require a real
second role locally"). Behavioral proof against a REAL second role lives
in ``tests/test_rls_isolation.py`` (which needs docker-compose Postgres);
this file is pure logic — given a `(current_user, rolbypassrls)` row and
an admin username, does the function raise exactly when it should.
"""

from __future__ import annotations

import pytest

import app.db.session as db_mod

# ---------------------------------------------------------------------------
# Fakes — no real asyncpg connection anywhere in this file.
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, row: tuple[str, bool]) -> None:
        self._row = row

    def one(self) -> tuple[str, bool]:
        return self._row


class _FakeConnection:
    def __init__(self, row: tuple[str, bool]) -> None:
        self._row = row

    async def __aenter__(self) -> _FakeConnection:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def execute(self, *_args: object, **_kwargs: object) -> _FakeResult:
        return _FakeResult(self._row)


class _FakeRequestEngine:
    """Stands in for ``request_engine`` — ``.connect()`` returns a fake
    connection whose one query always returns the given ``(current_user,
    rolbypassrls)`` row, regardless of the SQL text passed in."""

    def __init__(self, row: tuple[str, bool]) -> None:
        self._row = row

    def connect(self) -> _FakeConnection:
        return _FakeConnection(self._row)


class _NeverConnectRequestEngine:
    """Blows up if ``.connect()`` is ever called — proves the unset-
    ``app_database_url`` path short-circuits before touching the request
    engine at all."""

    def connect(self) -> object:
        raise AssertionError(
            "request_engine.connect() must never be called when app_database_url is unset"
        )


class _FakeUrl:
    def __init__(self, username: str) -> None:
        self.username = username


class _FakeAdminEngine:
    """Stands in for ``engine`` — only ``.url.username`` is read."""

    def __init__(self, username: str) -> None:
        self.url = _FakeUrl(username)


# ---------------------------------------------------------------------------
# 1. app_database_url unset — no-op, never touches request_engine.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_noop_when_app_database_url_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db_mod.settings, "app_database_url", None)
    monkeypatch.setattr(db_mod, "request_engine", _NeverConnectRequestEngine())

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
    monkeypatch.setattr(db_mod, "request_engine", _FakeRequestEngine(("service_role", True)))
    monkeypatch.setattr(db_mod, "engine", _FakeAdminEngine("postgres"))

    with pytest.raises(db_mod.RoleSeparationVerificationError):
        await db_mod.verify_request_engine_role_separation()


@pytest.mark.unit
async def test_raises_when_request_role_matches_admin_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy-paste error (APP_DATABASE_URL == DATABASE_URL) resolves to
    the SAME current_user on both engines — must also be rejected, even
    with rolbypassrls false."""
    monkeypatch.setattr(db_mod.settings, "app_database_url", "postgresql+asyncpg://x:y@h:6543/db")
    monkeypatch.setattr(db_mod, "request_engine", _FakeRequestEngine(("postgres", False)))
    monkeypatch.setattr(db_mod, "engine", _FakeAdminEngine("postgres"))

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
    monkeypatch.setattr(db_mod, "request_engine", _FakeRequestEngine(("service_role", True)))
    monkeypatch.setattr(db_mod, "engine", _FakeAdminEngine("postgres"))

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
    monkeypatch.setattr(db_mod, "request_engine", _FakeRequestEngine(("app_role", False)))
    monkeypatch.setattr(db_mod, "engine", _FakeAdminEngine("postgres"))

    await db_mod.verify_request_engine_role_separation()  # must not raise


# ---------------------------------------------------------------------------
# 4. Wiring pin — app.main's lifespan actually calls this, not just defines it.
# ---------------------------------------------------------------------------


@pytest.mark.unit
async def test_lifespan_calls_verify_request_engine_role_separation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pin: ``app.main``'s ``lifespan`` must actually invoke
    ``verify_request_engine_role_separation`` at startup — a future edit
    that only defines the check without wiring it into ``create_app()``
    would otherwise silently never run it."""
    called = False

    async def _fake_verify() -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("app.main.verify_request_engine_role_separation", _fake_verify)

    from app.main import _lifespan
    from app.main import app as fastapi_app

    async with _lifespan(fastapi_app):
        pass

    assert called is True
