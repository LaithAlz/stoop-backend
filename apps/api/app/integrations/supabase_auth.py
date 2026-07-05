"""Supabase JWT verification — JWKS-backed, asymmetric (ES256 / RS256 only).

Security properties enforced here:
- JWKS fetched asynchronously via httpx; cached for 24 h in the module-level
  ``_JwksState`` singleton, protected by its asyncio.Lock (concurrency-safe
  on a single Python process; each Fly machine has its own cache). Three
  independent rate limiters bound upstream fetches: a kid-miss forced-refresh
  window, a degenerate-200 cooldown, and a fetch-exception cooldown — see
  ``_JwksState`` and the three ``*_SECONDS`` constants.
- Explicit algorithm allowlist: ["ES256", "RS256"] only.  NEVER accepts
  "alg: none" or any HS* symmetric algorithm — prevents alg-confusion
  attacks (where an attacker signs HS256 using the public key as the
  secret).
- Verifies: signature, exp, iss (settings.supabase_jwt_issuer), aud
  ("authenticated").
- role claim MUST equal "authenticated" — rejects service_role tokens.
- Identity object is frozen (immutable after creation).
- NEVER logs the token, Authorization header, or any sub-string thereof.
  Only auth_user_id (the sub UUID) is logged after successful verification.

Never-break rule #5: no token, JWT sub-string, or Authorization header
value appears in any log line, exception message, or the error envelope.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from uuid import UUID

import httpx
import jwt
import structlog

from app.config import settings

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ALLOWED_ALGORITHMS: list[str] = ["ES256", "RS256"]
_AUD = "authenticated"
_JWKS_TTL_SECONDS: float = 86_400.0  # 24 hours

# Rate limit for the "force a refresh on unknown-kid" path (see
# ``_refresh_jwks_on_kid_miss`` below) ONLY. Key rotation is rare (Supabase
# key rotation happens on the order of weeks/months), so one forced refresh
# per minute is more than enough to pick up a rotated key quickly while
# bounding how often an attacker spamming random ``kid`` values can force us
# to hit Supabase's JWKS endpoint. Config-free on purpose: this is a DoS
# guard, not a deployment knob. (Issue #158 follow-up: this constant used to
# also silently double as the routine path's degenerate-200 cooldown below;
# the two are now independent knobs — see ``_DEGENERATE_FETCH_COOLDOWN_SECONDS``
# — that happen to share this numeric value, not a dependency between them.)
_FORCED_REFRESH_WINDOW_SECONDS: float = 60.0

# Rate limit for the ROUTINE ``_get_jwks`` fetch path's degenerate/empty
# ``{"keys": []}`` (or ``{}``) 200-response cooldown ONLY (issue #147
# follow-up; split into its own constant in issue #158's follow-up refactor).
# Bounds a sustained Supabase degenerate-200 incident to one serialized
# upstream fetch per window instead of one fetch per request for the whole
# incident. Same value as ``_FORCED_REFRESH_WINDOW_SECONDS`` above by
# coincidence, not by dependency — kept as an independent knob so the two
# call sites can be tuned separately.
_DEGENERATE_FETCH_COOLDOWN_SECONDS: float = 60.0

# Rate limit for the ROUTINE ``_get_jwks`` fetch path's fetch-EXCEPTION
# cooldown ONLY (issue #158): connection errors, 5xx via
# ``raise_for_status``, timeouts, malformed JSON — anything that makes
# ``_fetch_jwks`` raise rather than return a (possibly degenerate) body.
# Deliberately much shorter than the 60s windows above: reusing the 60s
# window here was considered and rejected, because a single transient
# network blip would then impose a 60s no-retry window on the ONLY path
# that can warm a cold cache. This bounds a fast-5xx storm to ~1 upstream
# attempt per process per window while capping cold-cache recovery from a
# transient blip at this window instead.
_FETCH_EXCEPTION_COOLDOWN_SECONDS: float = 5.0

# ---------------------------------------------------------------------------
# JWKS cache + rate-limit/cooldown state (module-level singleton)
# ---------------------------------------------------------------------------


class _JwksState:
    """Mutable JWKS cache plus its rate-limit/cooldown bookkeeping.

    Consolidates what used to be four separate module globals (issue #158
    follow-up refactor) into one object, so there is a single thing to reset
    between tests and a single thing to reason about under the lock:

    Attributes
    ----------
    cache : tuple[dict[str, Any], float] | None
        ``(jwks_data, fetched_at_monotonic)``. ``None`` means cold (nothing
        has ever been cached in this process).
    lock : asyncio.Lock
        Serializes JWKS fetches so concurrent coroutines coalesce into a
        single upstream attempt instead of a thundering herd.
    last_forced_refresh : float | None
        Monotonic timestamp of the last *forced* refresh triggered by an
        unknown-``kid`` miss (as opposed to a routine TTL-expiry refresh),
        rate-limited by ``_FORCED_REFRESH_WINDOW_SECONDS``. ``None`` means no
        forced refresh has happened yet in this process. Stamped BEFORE the
        fetch is attempted — see ``_refresh_jwks_on_kid_miss``.
    last_degenerate_fetch : float | None
        Monotonic timestamp of the last time the ROUTINE ``_get_jwks`` fetch
        path observed a degenerate/empty ``{"keys": []}`` (or ``{}``) 200
        response (issue #147 follow-up), rate-limited by
        ``_DEGENERATE_FETCH_COOLDOWN_SECONDS``. Distinct from
        ``last_forced_refresh`` above, which rate-limits the separate
        kid-miss forced-refresh path. ``None`` means no degenerate response
        has been observed on the routine path yet (or the most recent one
        has been superseded by a successful, non-degenerate fetch — see
        ``_get_jwks``). Stamped on OBSERVATION (only once the fetch is known
        to have come back degenerate), not on the bare attempt.
    last_fetch_exception : float | None
        Monotonic timestamp of the last time the ROUTINE ``_get_jwks`` fetch
        path observed ``_fetch_jwks`` RAISE (connection error, 5xx via
        ``raise_for_status``, timeout, malformed JSON) rather than returning
        a body — rate-limited by ``_FETCH_EXCEPTION_COOLDOWN_SECONDS`` (issue
        #158). Independent of ``last_degenerate_fetch``: an exception never
        arms the degenerate stamp, and a degenerate 200 never arms this one.
        Stamped on OBSERVATION (only once the fetch is known to have
        raised), inside the lock, before re-raising — the same
        stamp-on-observation choice as ``last_degenerate_fetch``, and the
        opposite of ``last_forced_refresh``'s stamp-on-attempt (see
        ``_get_jwks`` and ``_refresh_jwks_on_kid_miss`` for why each made
        its own choice). ``None`` means no fetch exception has been observed
        yet on the routine path (or it has been cleared by a subsequent
        non-raising fetch, degenerate or not).
    """

    def __init__(self) -> None:
        self.cache: tuple[dict[str, Any], float] | None = None
        self.lock: asyncio.Lock = asyncio.Lock()
        self.last_forced_refresh: float | None = None
        self.last_degenerate_fetch: float | None = None
        self.last_fetch_exception: float | None = None

    def reset_for_tests(self) -> None:
        """Restore every field to its cold-start value, including a FRESH
        ``asyncio.Lock``.

        The lock must be replaced, not merely left alone: with
        ``asyncio_default_fixture_loop_scope=function``, each test runs in
        its own event loop, and reusing the same ``asyncio.Lock`` instance
        across loops can raise ``RuntimeError: ... attached to a different
        loop`` from inside ``_get_jwks`` — an intermittent, order-dependent
        flake. See ``tests/conftest.py``'s ``_reset_jwks_auth_state`` fixture,
        which calls this once per test for exactly this reason.
        """
        self.cache = None
        self.lock = asyncio.Lock()
        self.last_forced_refresh = None
        self.last_degenerate_fetch = None
        self.last_fetch_exception = None


_jwks_state = _JwksState()


def _now() -> float:
    """Monotonic clock used for cache/rate-limit bookkeeping.

    A thin wrapper around ``time.monotonic()`` so tests can monkeypatch a
    single seam to simulate the passage of time (e.g. to exercise the
    forced-refresh rate-limit window) without real ``sleep()`` calls.
    """
    return time.monotonic()


# ---------------------------------------------------------------------------
# Custom exceptions
# ---------------------------------------------------------------------------


class AuthError(Exception):
    """Raised by the verifier on any authentication failure.

    ``code`` is a stable snake_case string used in the JSON error envelope.
    ``message`` is human-readable and intentionally generic — it must not
    reveal which specific check failed (and must never contain token material).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _JwksFetchCooldownError(Exception):
    """Internal-only: raised by ``_get_jwks`` in place of a real fetch
    attempt when the fetch-exception cooldown (issue #158) is active and
    there is no cached JWKS to fall back on serving.

    Deliberately mirrors the shape of a genuine ``_fetch_jwks`` failure (a
    raised exception, not a returned degenerate dict) so ``verify_jwt``'s
    existing generic ``except Exception -> AuthError("invalid_token", ...)``
    mapping handles this identically to a real fetch failure — callers of
    ``verify_jwt`` cannot distinguish "we tried and it failed" from "we
    didn't try because we just failed a moment ago", which is the point:
    both fail closed the same way. Never logged, never exposed — carries no
    information beyond a generic message.
    """


# ---------------------------------------------------------------------------
# Frozen identity object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthUser:
    """Verified identity extracted from a Supabase JWT.

    Fields
    ------
    user_id : UUID
        The stable ``sub`` claim — FK target for ``landlords.auth_user_id``.
    email : str | None
        The ``email`` claim.  May change if the user updates their address;
        treat as display-only contact info, never as an authorization key.
        Normalized at the ``verify_jwt`` boundary (issue #135 safety
        review): an empty string, a whitespace-only string, or a non-string
        claim value all collapse to ``None`` here — a real value is always
        a non-empty, stripped string. This matters because GoTrue emits
        ``"email": ""`` (present, empty) rather than omitting the claim for
        phone-only signups; callers checking ``is None``/truthiness need
        exactly one consistent "no email" signal, not two.
    full_name : str | None
        ``user_metadata.full_name`` — **user-writable**; use only for display.
        Never use as an authorization signal.
    """

    user_id: UUID
    email: str | None
    full_name: str | None


# ---------------------------------------------------------------------------
# Internal JWKS helpers
# ---------------------------------------------------------------------------


async def _fetch_jwks() -> dict[str, Any]:
    """Fetch the JWKS from Supabase and return the parsed dict.

    Called only when the cache is cold or expired. Runs in an async context
    using httpx (compatible with respx mocking in tests).
    """
    async with httpx.AsyncClient() as client:
        response = await client.get(
            settings.supabase_jwks_url,
            timeout=10.0,
        )
        response.raise_for_status()
        data: dict[str, Any] = response.json()
        return data


def _store_fetch_result_or_fallback(data: dict[str, Any]) -> dict[str, Any]:
    """Shared cache-write guard for both JWKS fetch paths.

    Used by both ``_get_jwks`` and ``_refresh_jwks_on_kid_miss`` (issue #158
    follow-up refactor — the drift between two near-identical copies of this
    logic is exactly what caused issue #147). A fetch that succeeds (HTTP
    200) but returns a degenerate/empty ``{"keys": []}`` body — or ``{}``,
    the ``"keys"`` field entirely absent; both are equally falsy under
    ``dict.get`` — must never clobber a known-good cache, and must never
    itself be cached:

    - Non-degenerate (``data.get("keys")`` truthy): replace the cache with
      ``(data, _now())`` and return ``data``.
    - Degenerate, existing cache present: a stale-but-real key set beats an
      empty one, so keep the existing cache entry UNCHANGED (do NOT bump
      ``fetched_at``) and return its data. Note for the routine
      ``_get_jwks`` caller specifically: the existing entry here may already
      be expired past the 24h TTL (that's *why* a refetch was attempted at
      all) — see that function's docstring for the resulting availability
      tradeoff, which is specific to its own cooldown, not this guard.
    - Degenerate, no existing cache: return the degenerate ``data`` as-is,
      WITHOUT caching it, so verification fails closed on *this* request
      only — the next call retries the fetch from scratch rather than being
      stuck with a cached empty key set for a full TTL.

    Callers are responsible for their own rate-limit/cooldown stamping
    (stamp-on-attempt vs stamp-on-observation — the two existing callers
    make different, deliberate choices; see each one's own docstring). This
    helper only ever runs once a fetch has already completed without
    raising — it has no opinion on exceptions.
    """
    if data.get("keys"):
        _jwks_state.cache = (data, _now())
        return data
    if _jwks_state.cache is not None:
        return _jwks_state.cache[0]
    return data


async def _get_jwks() -> dict[str, Any]:
    """Return the cached JWKS, fetching from Supabase if the cache is cold/expired.

    Thread-safety: asyncio.Lock ensures only one coroutine performs a fetch
    at a time; others wait then use the freshly-cached result.

    Two independent rate limiters guard the two ways a routine fetch
    attempt can go wrong, BOTH stamped on OBSERVATION (i.e. only once the
    outcome of the fetch is known, not on the bare attempt) — the opposite
    choice from ``_refresh_jwks_on_kid_miss``'s forced-refresh window, which
    stamps BEFORE attempting the fetch (see that function's docstring for
    why):

    - ``_DEGENERATE_FETCH_COOLDOWN_SECONDS`` (issue #147 follow-up): a
      confirmed degenerate/empty 200 response. See
      ``_store_fetch_result_or_fallback`` for the accompanying cache-write
      guard, and its docstring for the stale-beyond-TTL availability
      tradeoff that a SUSTAINED degenerate incident can produce here
      specifically (combined with this cooldown, a stale-but-real cache may
      keep being served for longer than the nominal 24h TTL — a deliberate
      tradeoff, bounded by the incident eventually clearing).
    - ``_FETCH_EXCEPTION_COOLDOWN_SECONDS`` (issue #158): a confirmed fetch
      exception (connection error, 5xx via ``raise_for_status``, timeout,
      malformed JSON). Bounds a fast-5xx Supabase incident to ~1 upstream
      attempt per process per window instead of one attempt per inbound
      request, while capping cold-cache recovery from a transient blip at
      this (much shorter) window rather than reusing the 60s degenerate
      window above — deliberately rejected, because a single transient
      network blip must not impose a 60s no-retry window on the only path
      that can warm a cold cache. When this cooldown is active: a cached
      (even if TTL-stale) entry is served as-is — the same stale-beyond-TTL
      availability tradeoff noted above can apply here too during a
      SUSTAINED exception incident; with no cache to fall back on, this
      raises ``_JwksFetchCooldownError`` instead of attempting the fetch —
      the same failure shape ``verify_jwt`` already handles generically.
      Cleared on any fetch that completes without raising (degenerate or
      not — degenerate-vs-not is this cooldown's own separate concern).
    """
    # Fast path (no lock): check if we have a fresh cache.
    if _jwks_state.cache is not None:
        data, fetched_at = _jwks_state.cache
        if _now() - fetched_at < _JWKS_TTL_SECONDS:
            return data

    # Slow path: acquire the lock, re-check (double-check locking), then fetch.
    async with _jwks_state.lock:
        if _jwks_state.cache is not None:
            data, fetched_at = _jwks_state.cache
            if _now() - fetched_at < _JWKS_TTL_SECONDS:
                return data

        now = _now()

        # Degenerate-fetch cooldown (issue #147 follow-up): if a recent
        # fetch on THIS (routine) path already came back with a
        # degenerate/empty body within the last
        # _DEGENERATE_FETCH_COOLDOWN_SECONDS, skip hitting Supabase again
        # this time. Mirrors the kid-miss rate limit
        # (last_forced_refresh / _refresh_jwks_on_kid_miss) but closes a gap
        # specific to this path: without it, a sustained Supabase
        # degenerate-200 incident would drive one serialized upstream fetch
        # per request for every cold/TTL-expired cache, for as long as the
        # incident lasted. Falls back to the stale cache if one exists (a
        # stale real key set beats an empty one), else fails closed with an
        # empty key set.
        if (
            _jwks_state.last_degenerate_fetch is not None
            and now - _jwks_state.last_degenerate_fetch < _DEGENERATE_FETCH_COOLDOWN_SECONDS
        ):
            if _jwks_state.cache is not None:
                return _jwks_state.cache[0]
            return {"keys": []}

        # Fetch-exception cooldown (issue #158): if a recent fetch on THIS
        # (routine) path already RAISED (rather than returning a degenerate
        # 200) within the last _FETCH_EXCEPTION_COOLDOWN_SECONDS, skip
        # hitting Supabase again this time. This is what bounds a fast-5xx
        # storm to ~1 attempt per window per process instead of one attempt
        # per inbound request. Falls back to the cached entry if one
        # exists (even if it's past its TTL — a stale real key set beats
        # failing closed); otherwise fails closed the same way the original
        # exception would have, by raising rather than returning a
        # degenerate dict, so this never reaches _find_signing_key / the
        # kid-miss forced-refresh path on a cold cache (there is no jwks
        # dict to hand it). If the cache-fallback branch is taken instead
        # and that stale entry's kid has since rotated, the normal kid-miss
        # path still cascades as usual, rate-limited independently by
        # _FORCED_REFRESH_WINDOW_SECONDS.
        if (
            _jwks_state.last_fetch_exception is not None
            and now - _jwks_state.last_fetch_exception < _FETCH_EXCEPTION_COOLDOWN_SECONDS
        ):
            if _jwks_state.cache is not None:
                return _jwks_state.cache[0]
            raise _JwksFetchCooldownError(
                "JWKS fetch skipped: recent fetch-exception cooldown active."
            )

        try:
            data = await _fetch_jwks()
        except Exception:
            # Stamp the cooldown on the OBSERVED EXCEPTION (i.e. only once
            # we know the fetch actually raised), inside the lock, before
            # re-raising — the same stamp-on-observation choice as the
            # degenerate cooldown below (and the opposite of
            # _refresh_jwks_on_kid_miss's stamp-on-attempt — see that
            # function's docstring). Then propagate the original exception
            # unchanged; verify_jwt's existing generic exception mapping
            # already handles it identically either way.
            _jwks_state.last_fetch_exception = _now()
            raise

        # The fetch completed without raising: clear the exception cooldown
        # so recovery from a prior transient blip is instant, and so a
        # LATER, separate blip starts its own fresh cooldown rather than
        # inheriting stale state. This clears regardless of whether the
        # response turns out to be degenerate below — that's the separate,
        # independent concern of last_degenerate_fetch.
        _jwks_state.last_fetch_exception = None

        if not data.get("keys"):
            # Stamp the cooldown on the DEGENERATE OBSERVATION (i.e. only
            # once we know the fetch actually came back empty), not on the
            # bare attempt — same reasoning as the exception cooldown above.
            _jwks_state.last_degenerate_fetch = _now()
            return _store_fetch_result_or_fallback(data)

        # Non-degenerate fetch: clear the cooldown stamp so recovery from a
        # prior degenerate incident is instant, not bounded by the window.
        _jwks_state.last_degenerate_fetch = None
        return _store_fetch_result_or_fallback(data)


async def _refresh_jwks_on_kid_miss() -> dict[str, Any]:
    """Force a single, rate-limited JWKS refresh *attempt* after an
    unknown-``kid`` miss.

    Supabase rotates its signing keys occasionally; when it does, every
    newly-issued token carries a ``kid`` absent from our (up to 24h-old)
    cache. Without this, every authenticated request would fail-closed until
    the TTL naturally expires — an availability outage, not a security one.

    Rate limiting: at most one forced refresh *attempt* per
    ``_FORCED_REFRESH_WINDOW_SECONDS`` window, tracked via
    ``_jwks_state.last_forced_refresh``. It is stamped BEFORE the fetch is
    attempted, not only on success — the window must bound *attempts*, not
    successes. If the fetch itself fails (connection error, 5xx via
    ``raise_for_status``, malformed JSON), the window is still consumed:
    otherwise an attacker spamming unknown ``kid`` values while Supabase's
    JWKS endpoint happens to be erroring could drive one upstream attempt
    per request — hammering the endpoint exactly while it's recovering. The
    tradeoff is that a transiently-failed refresh can delay picking up a
    real rotation by up to the window (60s), which the issue explicitly
    accepts. This stamp-on-attempt design is deliberately left untouched by
    issue #158, which only added a (much shorter) stamp-on-observation
    cooldown to the separate routine ``_get_jwks`` path — see that
    function's docstring.

    Cache-write guard: see ``_store_fetch_result_or_fallback`` (shared with
    ``_get_jwks``, issue #158 follow-up refactor). A fetch that succeeds
    (HTTP 200) but returns a degenerate/empty ``{"keys": []}`` body must NOT
    clobber a known-good cache — otherwise a single bad-but-200 upstream
    response could fail closed ALL dashboard auth for up to 24h (the full
    TTL). The cache is only replaced when the fetched payload has a
    non-empty ``"keys"`` list; otherwise the existing cache (if any) is
    kept, and the caller's retry simply misses and fails closed as normal.

    Concurrency: the rate-limit check, the stamp, and the fetch attempt all
    happen under ``_jwks_state.lock`` so that concurrent kid-misses coalesce
    into a single attempt — the first coroutine through the lock stamps
    ``last_forced_refresh`` and performs the fetch; any coroutine that
    acquires the lock afterwards, within the window, sees the stamp and
    skips its own attempt (regardless of whether the first attempt
    succeeded, failed, or returned a degenerate body).

    Returns whatever JWKS is currently cached — either freshly refetched
    (only if it contained keys), or the previously-cached copy. The caller
    re-attempts ``_find_signing_key`` against the result and gets a normal
    ``invalid_token`` if the ``kid`` still isn't present.
    """
    async with _jwks_state.lock:
        now = _now()
        rate_limited = (
            _jwks_state.last_forced_refresh is not None
            and now - _jwks_state.last_forced_refresh < _FORCED_REFRESH_WINDOW_SECONDS
        )
        if rate_limited:
            # Rate-limited: another kid-miss already attempted a refresh
            # recently (possibly a concurrent one that just released the
            # lock, or one that failed outright). Reuse whatever is cached
            # rather than attempting another fetch.
            if _jwks_state.cache is not None:
                return _jwks_state.cache[0]
            # Reachable (issue #147 follow-up corrected this comment: it is
            # NOT unreachable). _jwks_state.cache being None here means no
            # real keys have EVER been cached in this process — a
            # persistent degenerate upstream (routine _get_jwks fetches keep
            # observing empty bodies too, per its own no-cache guard) combined
            # with a kid-miss landing inside this function's rate-limit
            # window lands right here. There's nothing to fall back to; fail
            # closed with an empty key set (the caller's _find_signing_key
            # then raises its own invalid_token as normal).
            return {"keys": []}

        # Stamp BEFORE attempting the fetch — see docstring: the window
        # must bound attempts, not just successes.
        _jwks_state.last_forced_refresh = now

        data = await _fetch_jwks()

        return _store_fetch_result_or_fallback(data)


def _find_signing_key(jwks: dict[str, Any], kid: str) -> Any:
    """Return the PyJWK object matching ``kid`` from a JWKS dict.

    Raises AuthError (invalid_token) if no key matches.
    """
    from jwt import PyJWKSet

    try:
        jwk_set = PyJWKSet.from_dict(jwks)
    except Exception as exc:
        raise AuthError("invalid_token", "Authentication failed.") from exc

    for key in jwk_set.keys:
        if key.key_id == kid:
            return key.key

    raise AuthError("invalid_token", "Authentication failed.")


# ---------------------------------------------------------------------------
# Public verification function
# ---------------------------------------------------------------------------


async def verify_jwt(token: str) -> AuthUser:
    """Verify a Supabase access token and return the authenticated identity.

    Security checks (in order):
    1. Decode the unverified header to extract ``kid``.
    2. Fetch/cache JWKS and locate the signing key by ``kid``.
    3. Decode + verify using PyJWT with an explicit algorithm allowlist.
       This verifies: signature, ``exp``, ``iss``, ``aud``.
    4. Assert ``role == "authenticated"`` to reject service_role tokens.
    5. Map claims to a frozen AuthUser.

    NEVER logs the token or any substring of it.

    Raises
    ------
    AuthError
        On any verification failure.  The ``code`` field distinguishes
        ``expired`` from generic ``invalid_token`` / ``forbidden_role`` /
        ``invalid_token`` (unknown kid).
    """
    # Step 1 — extract kid from the unverified header.
    # We do NOT decode the payload here, only the header.
    try:
        header = jwt.get_unverified_header(token)
    except jwt.exceptions.DecodeError as exc:
        raise AuthError("invalid_token", "Authentication failed.") from exc

    kid: str | None = header.get("kid")
    if not kid:
        raise AuthError("invalid_token", "Authentication failed.")

    # Guard: reject any token that declares a non-allowlisted algorithm
    # before we even touch the key material.  PyJWT's decode() already
    # enforces the allowlist but this catches it one step earlier and makes
    # the intent explicit.
    alg: str | None = header.get("alg")
    if alg not in _ALLOWED_ALGORITHMS:
        raise AuthError("invalid_token", "Authentication failed.")

    # Step 2 — fetch/cache JWKS and find the matching signing key.
    try:
        jwks = await _get_jwks()
    except Exception as exc:
        raise AuthError("invalid_token", "Authentication failed.") from exc

    try:
        signing_key = _find_signing_key(jwks, kid)
    except AuthError:
        # Unknown kid: our cache may simply be stale because Supabase
        # rotated its signing keys. Force a single, rate-limited refresh
        # (see _refresh_jwks_on_kid_miss) and retry exactly once against the
        # refreshed set. If the kid is still missing after that, the
        # AuthError from this second lookup propagates as a normal
        # invalid_token — no further retries, no loops.
        try:
            jwks = await _refresh_jwks_on_kid_miss()
        except Exception as exc:
            raise AuthError("invalid_token", "Authentication failed.") from exc
        signing_key = _find_signing_key(jwks, kid)

    # Step 3 — full verification with explicit allowlist.
    # PyJWT verifies: signature, exp, iss, aud.
    # algorithms= locks out "none" and all HS* algorithms.
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            signing_key,
            algorithms=_ALLOWED_ALGORITHMS,
            audience=_AUD,
            issuer=settings.supabase_jwt_issuer,
            # PyJWT only validates a claim if it is PRESENT. Require exp/iss/aud
            # so a validly-signed token that simply omits exp can't become a
            # non-expiring credential (anything able to mint under the project
            # keys must still produce short-lived tokens). Missing → rejected.
            options={"require": ["exp", "iss", "aud"]},
        )
    except jwt.ExpiredSignatureError as exc:
        # Distinct code so clients can refresh silently.
        raise AuthError("expired", "Token has expired.") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("invalid_token", "Authentication failed.") from exc

    # Step 4 — reject service_role tokens.
    if claims.get("role") != "authenticated":
        raise AuthError("forbidden_role", "Authentication failed.")

    # Step 5 — map claims to frozen identity.
    try:
        user_id = UUID(claims["sub"])
    except (KeyError, ValueError) as exc:
        raise AuthError("invalid_token", "Authentication failed.") from exc

    # Normalize the email claim at this boundary (issue #135 safety review):
    # GoTrue serializes the JWT `email` claim from a Go string with no
    # `omitempty`, so a real phone-only signup emits `"email": ""`
    # (present, empty) rather than an absent claim — a bare `.get("email")`
    # would let that empty string sail past any `is None` check downstream.
    # A non-string claim (malformed/junk token) is treated the same way.
    # Collapsing empty/whitespace-only/non-string to None here means every
    # caller of `AuthUser.email` gets one consistent "no email" signal.
    _raw_email: Any = claims.get("email")
    email: str | None = (
        _raw_email.strip() if isinstance(_raw_email, str) and _raw_email.strip() else None
    )
    user_metadata: dict[str, Any] = claims.get("user_metadata") or {}
    full_name: str | None = user_metadata.get("full_name")

    identity = AuthUser(user_id=user_id, email=email, full_name=full_name)

    # Safe to log: only the UUID, never the token.
    log.info("auth_verified", auth_user_id=str(identity.user_id))

    return identity
