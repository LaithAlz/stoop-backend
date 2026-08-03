"""Stoop API — centralised environment / settings.

All environment variables for Phase 1 are declared here as typed,
validated Pydantic fields.  A missing required variable raises
``pydantic.ValidationError`` at startup — no silent runtime surprises.

Usage everywhere else::

    from app.config import settings

    if settings.is_production:
        ...

IMPORTANT: Never log or print the ``settings`` object — it carries secrets.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Twilio Account SIDs are always "AC" + 32 lowercase-hex characters (34
# chars total) -- production-only shape gate, see
# Settings._require_valid_twilio_account_sid_format_in_production below.
_TWILIO_ACCOUNT_SID_RE = re.compile(r"^AC[0-9a-fA-F]{32}$")

# A single DASHBOARD_ORIGINS entry must be a bare `scheme://host[:port]`
# origin: lowercase http/https scheme, a lowercase dotted hostname (or an
# IPv4 address -- each dot-separated label is just an alphanumeric/hyphen
# run under this same pattern), an optional numeric port, nothing else --
# no trailing slash, no path, no wildcard. All-environments shape gate, see
# Settings._validate_dashboard_origin_shapes below.
_DASHBOARD_ORIGIN_RE = re.compile(
    r"^(?P<scheme>https?)://"
    r"(?P<host>[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*)"
    r"(?::(?P<port>[0-9]{1,5}))?$"
)


class Settings(BaseSettings):
    """All Phase 1 environment variables, validated at startup."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Runtime environment
    # ------------------------------------------------------------------

    environment: Literal["dev", "staging", "production"] = "dev"
    """Which deployment environment we're running in.

    Defaults to ``"dev"`` so local and test imports work without setting
    ``ENVIRONMENT``.  Any other value is a startup error (typo protection).
    """

    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    """Minimum log level passed to structlog / stdlib logging."""

    # ------------------------------------------------------------------
    # Database (sensitive — no defaults)
    # ------------------------------------------------------------------

    database_url: str = Field(
        ...,
        description=(
            "Async SQLAlchemy connection string, e.g. "
            "postgresql+asyncpg://USER:PASSWORD@HOST:6543/postgres"
        ),
    )

    app_database_url: str | None = Field(
        default=None,
        description=(
            "Optional SECOND connection string for REQUEST-path sessions "
            "(app/db/session.py's get_session), using the app_role Postgres "
            "login that migration 0005's RLS policies key off (#22). Same "
            "Supavisor pooler host as DATABASE_URL, different user/password "
            "-- e.g. postgresql+asyncpg://app_role.PROJECT_REF:PASSWORD@HOST:6543/postgres. "
            "Leave unset (default: local dev, CI, and production until the "
            "one-time operator step in app/db/session.py's module docstring "
            "is done) -- request sessions then fall back to the admin "
            "engine (DATABASE_URL) and a one-time startup WARNING notes "
            "that RLS is not yet enforced by role separation. app_role has "
            "NO password until an operator sets one directly against the "
            "database (never in a migration, never here). REQUIRED when "
            "ENVIRONMENT=production -- see _require_app_database_url_in_production."
        ),
    )

    # ------------------------------------------------------------------
    # Supabase (sensitive — no defaults)
    # ------------------------------------------------------------------

    supabase_url: str = Field(
        ...,
        description="Base URL of the Supabase project, e.g. https://xyz.supabase.co",
    )

    supabase_jwks_url: str = Field(
        ...,
        description=(
            "JWKS endpoint used for JWT verification, e.g. "
            "https://xyz.supabase.co/auth/v1/.well-known/jwks.json"
        ),
    )

    supabase_jwt_issuer: str = Field(
        ...,
        description=("Expected 'iss' claim in Supabase JWTs, e.g. https://xyz.supabase.co/auth/v1"),
    )

    supabase_service_role_key: str = Field(
        ...,
        description="Supabase service-role key (Fly secret — never expose to clients).",
    )

    # ------------------------------------------------------------------
    # Twilio (webhook signature verification — #40/#152; sensitive)
    # ------------------------------------------------------------------

    twilio_auth_token: str = Field(
        ...,
        description=(
            "Twilio Auth Token used to verify X-Twilio-Signature on inbound "
            "webhooks (app/integrations/twilio.py). NEVER logged, NEVER "
            "included in any error message. Required — a real Twilio "
            "account/number already exists for this project (see .env); "
            "unlike app_database_url this has no safe fallback because "
            "there is no way to verify a webhook signature without it."
        ),
    )

    twilio_account_sid: str = Field(
        ...,
        description=(
            "Twilio Account SID — paired with twilio_auth_token to construct "
            "the outbound REST client (app/integrations/twilio_send.py, #108). "
            "Used ONLY by the emergency escalation chain today (the other "
            "sanctioned sender, the approve-flow draft sender, is #44, "
            "unbuilt). NEVER logged, NEVER included in any error message. "
            "Required — the same real Twilio account referenced by "
            "twilio_auth_token already has this SID (see .env)."
        ),
    )

    public_base_url: str | None = Field(
        default=None,
        description=(
            "The public HTTPS origin Twilio is configured to POST webhooks "
            "to, e.g. https://api.stoop.example — used to reconstruct the "
            "EXACT url Twilio signed, proxy-aware. When set, the signed url "
            "is public_base_url + request path (+ query). When unset "
            "(local dev default), app/integrations/twilio.py falls back to "
            "request.url, honoring X-Forwarded-Proto/X-Forwarded-Host from "
            "the trusted proxy hop (Fly.io terminates TLS at its edge — see "
            "that module's reconstruct_signing_url docstring). REQUIRED when "
            "ENVIRONMENT=production -- see _require_public_base_url_in_production: "
            "signature verification must not depend on trusting proxy headers "
            "in production."
        ),
    )

    twilio_messaging_service_sid: str | None = Field(
        default=None,
        description=(
            "Optional Twilio Messaging Service SID for automatic A2P 10DLC/"
            "CASL campaign association on newly-provisioned property numbers "
            "(app/property_provisioning.py, #53). Unset today -- A2P "
            "registration is still pending externally (architecture.md: "
            "'a milestone-1 task, not an afterthought'). When unset, "
            "POST /v1/properties still provisions a fully working number, "
            "just without the campaign association -- logged and skipped "
            "gracefully, never failing provisioning on it. Set this the day "
            "a real Messaging Service + campaign exists to start associating "
            "every NEWLY provisioned number automatically; it has no effect "
            "on numbers provisioned before it was set (no retroactive "
            "backfill)."
        ),
    )

    max_properties_per_landlord: int = Field(
        default=25,
        description=(
            "Hard cap on how many properties (and therefore live, "
            "real-money Twilio numbers) a single landlord can provision "
            "(app/routers/properties.py, #53 safety review finding H1). "
            "Checked BEFORE any Twilio call, purely as a guard against "
            "unbounded spend from a buggy or malicious client hammering "
            "POST /v1/properties -- this is NOT an entitlement/paywall "
            "gate (never-break rule #1: the emergency line is never "
            "paywalled or throttled) -- every landlord, free or paid, "
            "gets the identical cap. 25 is a generous ceiling for a v1 "
            "self-serve landlord; raise the default here (never via a "
            "feature flag -- this is a cost/safety guard, not a rollout "
            "knob or pricing cohort) if a real landlord ever needs more."
        ),
    )

    trust_graduation_threshold: int = Field(
        default=10,
        ge=3,
        description=(
            "FOUNDER-PROVISIONAL (#60) -- number of CONSECUTIVE clean "
            "(unedited) sends on a (property, 'routine') trust_metrics row "
            "before autonomy_unlocked flips true and that pairing starts "
            "auto-sending routine drafts without landlord approval. No "
            "founder ruling on this EXACT number exists yet (verified "
            "against docs + issue history at implementation time) -- 10 is "
            "a reasonable placeholder pending ratification, not a "
            "considered product decision. The founder must ratify or "
            "adjust this one number before launch. Change ONLY here -- "
            "never hardcode a graduation count anywhere in app/agent or "
            "app/routers (grep for this field name if you're tempted to). "
            "Applies EXCLUSIVELY to severity='routine' rows -- "
            "'urgent'/'emergency' rows never graduate regardless of this "
            "value (schema-v1.md's own trust_metrics.autonomy_unlocked "
            "comment: 'only ever true for routine in v1'; CLAUDE.md rule "
            "3). This is a product/safety threshold, never a feature flag "
            "(rule 7 forbids flag reads anywhere near auto-send). "
            "`ge=3` (safety review, LOW-3): a 0/1/negative env value would "
            "collapse the ladder to 'graduate on the first or second send' "
            "-- refuse to boot with a value that low rather than silently "
            "accept a near-instant-trust misconfiguration."
        ),
    )

    auto_send_daily_case_cap: int = Field(
        default=5,
        ge=1,
        description=(
            "FOUNDER-PROVISIONAL (#60 safety review MEDIUM-2) -- hard cap "
            "on how many auto_sent replies a single CASE may receive "
            "within a trailing 24h window before auto-send falls back to "
            "the normal landlord-approval interrupt (the SAME fail-closed "
            "edge app/agent/graph.py's _route_after_draft_response already "
            "uses for a trust-lookup failure). Counted directly off "
            "audit_log 'auto_sent' rows (append-only INSERT-count -- the "
            "honest source, never a separate mutable counter that could "
            "drift from the audit trail). Exists because auto-send is the "
            "ONLY human-free send path in this codebase (besides the "
            "emergency safety path) -- a runaway back-and-forth or a "
            "misclassified conversation must not be able to fire an "
            "unbounded number of unattended sends on one case. Never a "
            "feature flag (rule 7) -- change the default here only."
        ),
    )

    # ------------------------------------------------------------------
    # Anthropic (agent — #26/#9+; sensitive — no default)
    # ------------------------------------------------------------------

    anthropic_api_key: str = Field(
        ...,
        description=(
            "Anthropic API key used by the agent's classify_severity/"
            "draft_response nodes (app/integrations/anthropic.py, lands "
            "with #9+). Required -- a real key already exists for this "
            "project (see .env). NEVER logged, NEVER included in any error "
            "message."
        ),
    )

    # ------------------------------------------------------------------
    # LangSmith (agent tracing — #26; optional, like sentry_dsn)
    # ------------------------------------------------------------------

    langsmith_api_key: str | None = Field(
        default=None,
        description=(
            "LangSmith API key for LangGraph/LangChain tracing (#26). "
            "Leave unset to disable tracing entirely -- there is no "
            "LangSmith account yet. When set, "
            "app/observability.py's init_langsmith_tracing() exports the "
            "LANGSMITH_TRACING/LANGSMITH_API_KEY/LANGSMITH_PROJECT env "
            "vars the langsmith SDK reads ambiently; when unset, none of "
            "those env vars are ever exported and nothing about tracing "
            "is attempted -- a missing/absent LangSmith account must "
            "never break app startup or agent runs."
        ),
    )

    langsmith_project: str | None = Field(
        default=None,
        description=(
            "LangSmith project name traces are grouped under (#26). Only "
            "meaningful when langsmith_api_key is set; the langsmith SDK "
            "falls back to its own 'default' project when unset."
        ),
    )

    # ------------------------------------------------------------------
    # Observability (optional)
    # ------------------------------------------------------------------

    sentry_dsn: str | None = None
    """Sentry DSN.  Leave unset (or blank) to disable Sentry entirely."""

    # ------------------------------------------------------------------
    # CORS (dashboard origins — #251; not sensitive, no secrets)
    # ------------------------------------------------------------------

    dashboard_origins: str = Field(
        default="http://localhost:5173,http://localhost:3000",
        description=(
            "Comma-separated allowlist of browser origins permitted to call "
            "this API cross-origin (app/main.py's CORSMiddleware, #251) -- "
            "e.g. the web dashboard's Vite dev server. NEVER '*' -- CORS "
            "here is a fixed origin allowlist, never a wildcard (see "
            "_reject_wildcard_dashboard_origin below, which refuses to "
            "boot on a literal '*'). Parsed into `dashboard_origins_list` "
            "(below) -- entries are comma-split, surrounding whitespace on "
            "each entry is stripped, blank entries are dropped. An empty/"
            "blank value is a SAFE default in dev/staging (an empty "
            "allowlist -- CORSMiddleware then permits zero browser "
            "origins), never a startup crash and never a silent fallback "
            "to '*' -- but REQUIRED, non-empty, non-localhost, and https:// "
            "when ENVIRONMENT=production (safety review, #251 F2 / #255 N1 "
            "-- see _require_non_local_dashboard_origins_in_production "
            "below). "
            "Every entry must be shaped exactly `scheme://host[:port]` "
            "(lowercase, http or https, no trailing slash, no path) -- see "
            "_validate_dashboard_origin_shapes below."
        ),
    )

    @field_validator("dashboard_origins", mode="after")
    @classmethod
    def _reject_wildcard_dashboard_origin(cls, v: str) -> str:
        """Refuse a literal ``*`` anywhere in ``DASHBOARD_ORIGINS`` (#251).

        CORS for the dashboard is a fixed origin allowlist, never a
        wildcard. This API never uses cookies (``allow_credentials=False``,
        ``app/main.py``), so browsers wouldn't honor a bare ``'*'`` for a
        credentialed request anyway -- but the quieter, real risk is that a
        wildcard would let ANY website's JavaScript read this API's
        authenticated bearer-token responses cross-origin, not just the
        intended dashboard. Refusing at config-parse time catches a
        copy-pasted ``'*'`` (a common CORS "quick fix" reflex) before it
        ever reaches ``CORSMiddleware``, the same fail-fast-at-startup
        discipline every other boot gate in this class already applies.

        Conscious tradeoff (safety review, #251 F3): this refuses to BOOT
        the whole process over what is, by itself, only a browser-facing
        misconfiguration -- a bare ``'*'`` here would take down emergency
        SMS/voice intake too, since the process never starts. Kept anyway,
        for two reasons: (1) consistency with the RLS/Twilio boot gates
        already in this class (``_require_app_database_url_in_production``,
        ``_require_valid_twilio_account_sid_format_in_production``) --
        this codebase's established answer to "a config value could
        silently make a safety/security property false" is fail-closed at
        startup, not fail-open into a degraded runtime; and (2) ``'*'``
        genuinely lets ANY site's JS read every authenticated response --
        materially worse than the failure mode ``_validate_dashboard_
        origin_shapes`` below guards (a merely-wrong-shaped entry, which
        that validator now also catches, closing the class of
        accepted-but-useless entries like ``https://*.host`` at the SAME
        deploy-config stage, before a rolling deploy could mask it behind
        a still-serving previous instance).
        """
        if any(origin.strip() == "*" for origin in v.split(",")):
            raise ValueError(
                "DASHBOARD_ORIGINS must never contain '*' -- CORS uses a "
                "fixed origin allowlist, never a wildcard (#251)."
            )
        return v

    @field_validator("dashboard_origins", mode="after")
    @classmethod
    def _validate_dashboard_origin_shapes(cls, v: str) -> str:
        """Reject any non-blank ``DASHBOARD_ORIGINS`` entry that isn't a
        bare ``scheme://host[:port]`` origin (safety review, #251 F2b).

        Verified empirically against the un-gated version of this field:
        every plausible misconfiguration -- a trailing slash
        (``https://app.stoop.example/``), a path suffix
        (``https://app.stoop.example/dashboard``), mixed case
        (``Https://App.Stoop.Example``), a stray semicolon, a bare host
        with no scheme (``app.stoop.example``), or an accepted-but-useless
        wildcard subdomain (``https://*.host`` -- a shape Starlette's
        ``CORSMiddleware`` treats as a literal string to compare against
        ``Origin``, never a pattern, so it can never actually match a real
        browser ``Origin`` header) -- silently parses into
        ``dashboard_origins_list`` as one more entry that then NEVER
        matches any real browser ``Origin`` header. The net runtime effect
        is identical to an empty allowlist (the dashboard is silently
        bricked), but with zero startup signal -- exactly the "discovered
        by customer complaint" failure mode this validator closes, by
        refusing to boot instead.

        ``_DASHBOARD_ORIGIN_RE`` intentionally allows only lowercase
        scheme/host characters, digits, hyphens, and dots, an optional
        numeric port, and nothing else -- both ``localhost`` and IPv4
        addresses like ``127.0.0.1`` match its ``host`` group (each
        dot-separated label is just an alphanumeric/hyphen run), so the
        dev default and every real production origin validate the same
        way, through the same regex.
        """
        for origin in (o.strip() for o in v.split(",")):
            if origin and not _DASHBOARD_ORIGIN_RE.match(origin):
                raise ValueError(
                    f"DASHBOARD_ORIGINS entry {origin!r} is not a valid origin -- "
                    "expected 'scheme://host[:port]' (lowercase, http or https "
                    "scheme, no trailing slash, no path, no wildcard) -- refusing "
                    "to boot with a malformed CORS allowlist entry (#251)."
                )
        return v

    @property
    def dashboard_origins_list(self) -> list[str]:
        """Parsed ``dashboard_origins`` -- comma-separated, whitespace
        -tolerant, blank entries dropped (#251). ``""`` or a whitespace
        -only value parses to ``[]``, so ``CORSMiddleware`` ends up with an
        empty allowlist (permits no browser origins) rather than crashing
        startup or silently falling back to ``'*'``.
        """
        return [origin.strip() for origin in self.dashboard_origins.split(",") if origin.strip()]

    # NOTE: the production boot gate for dashboard_origins
    # (_require_non_local_dashboard_origins_in_production) is declared at
    # the BOTTOM of this class, alongside the other production boot gates
    # (_require_app_database_url_in_production et al), not here next to
    # this field's other validators -- see that method's own docstring for
    # why the ORDER of `@model_validator(mode="after")` methods matters
    # (pydantic v2 runs them as a fail-fast chain in declaration order; the
    # first one to raise is the ONLY error reported, so this validator
    # must be declared AFTER the pre-existing gates to avoid masking
    # THEIR error messages in the (unrelated) production scenarios they
    # each individually test).

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_production(self) -> bool:
        """True when running in the production environment.

        Referenced by issue #7 (logging / Sentry wiring) and beyond.
        """
        return self.environment == "production"

    # ------------------------------------------------------------------
    # Production boot gate (#22 safety review items 3 and 13a)
    # ------------------------------------------------------------------

    @field_validator("app_database_url", mode="after")
    @classmethod
    def _normalize_app_database_url(cls, v: str | None) -> str | None:
        """Treat a whitespace-only value the same as unset (#22 safety
        review item 13a).

        A blank/placeholder Fly secret (``APP_DATABASE_URL="   "``, or one
        accidentally set to the empty string) would otherwise be truthy
        (a non-empty Python string) and sail past the boot gate below,
        then fail later at ``create_async_engine`` with an obscure parse
        error instead of this module's clear, intentional message.
        Normalizing here means every consumer of ``settings.app_database_url``
        (this boot gate, ``app/db/session.py``'s fallback branch, the
        startup role-separation self-check) sees a single consistent
        "is it actually set" signal, instead of each having to re-implement
        the same ``.strip()`` check.
        """
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @model_validator(mode="after")
    def _require_app_database_url_in_production(self) -> Settings:
        """Refuse to boot in production without RLS role separation (#22).

        ``app_database_url`` unset is a deliberately safe DEFAULT for local
        dev/CI/production-before-the-operator-step (see
        ``app/db/session.py``'s module docstring) — request sessions fall
        back to the admin engine and only a WARNING is logged. That
        fallback is fine right up until real tenant data exists. It is NOT
        an acceptable steady state for a production boot: once
        ``ENVIRONMENT=production`` is set, this refuses to start at all
        rather than silently run every request through the admin engine,
        unscoped by RLS, indefinitely. No secrets in this message — it
        only ever fires because a value is ABSENT (the field validator
        above already normalized a whitespace-only value to ``None``, so
        this check sees the same "unset" either way).
        """
        if self.environment == "production" and not self.app_database_url:
            raise ValueError(
                "APP_DATABASE_URL is required when ENVIRONMENT=production "
                "(RLS role separation, #22) -- refusing to boot without it. "
                "See app/db/session.py's module docstring for the one-time "
                "operator step (ALTER ROLE app_role LOGIN PASSWORD ...; "
                "then set the APP_DATABASE_URL Fly secret)."
            )
        return self

    # ------------------------------------------------------------------
    # Production boot gate (#40/#152 consolidated review item 5) --
    # mirrors _normalize_app_database_url / _require_app_database_url_in_
    # production exactly, same rationale, different field.
    # ------------------------------------------------------------------

    @field_validator("public_base_url", mode="after")
    @classmethod
    def _normalize_public_base_url(cls, v: str | None) -> str | None:
        """Treat a whitespace-only value the same as unset — same reasoning
        as ``_normalize_app_database_url`` above (a blank/placeholder Fly
        secret must not silently sail past the boot gate below)."""
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @model_validator(mode="after")
    def _require_public_base_url_in_production(self) -> Settings:
        """Refuse to boot in production without a configured
        ``public_base_url`` (#40/#152 consolidated safety review).

        ``app/integrations/twilio.py``'s ``reconstruct_signing_url`` falls
        back to trusting ``X-Forwarded-Proto``/``X-Forwarded-Host`` request
        headers when ``public_base_url`` is unset — safe ONLY because
        Fly.io is the single, trusted proxy hop in front of this app today.
        That fallback is a reasonable DEFAULT for local dev (see
        ``public_base_url``'s field description) but is NOT an acceptable
        steady state for a production boot: Twilio signature verification
        (the only thing standing between this webhook and an unauthenticated
        caller) must not depend on trusting proxy headers whose provenance
        this config layer cannot itself verify. Mirrors
        ``_require_app_database_url_in_production``'s precedent exactly.
        """
        if self.environment == "production" and not self.public_base_url:
            raise ValueError(
                "PUBLIC_BASE_URL is required when ENVIRONMENT=production "
                "(#40/#152) -- refusing to boot without it. Twilio signature "
                "verification must not depend on trusting proxy headers in "
                "production; set PUBLIC_BASE_URL to the public HTTPS origin "
                "Twilio is configured to POST webhooks to."
            )
        return self

    # ------------------------------------------------------------------
    # Production boot gate (#108 safety review, 2026-07-12, finding 7) --
    # mirrors _require_public_base_url_in_production's PATTERN: a strict
    # SHAPE check gated to production only, so dev/test placeholder values
    # (which need not look like a real Twilio SID) keep working unchanged.
    # ------------------------------------------------------------------

    @field_validator("twilio_account_sid", mode="after")
    @classmethod
    def _normalize_twilio_account_sid(cls, v: str) -> str:
        """Reject a blank/whitespace-only value in EVERY environment (this
        field has no safe "unset" fallback the way ``app_database_url``/
        ``public_base_url`` do -- there is no code path that works without
        a real Account SID once the outbound Twilio client is ever
        constructed), while still allowing loosely-shaped dev/test
        placeholders through -- the STRICT shape check below is
        production-only. Mirrors ``_normalize_app_database_url``/
        ``_normalize_public_base_url``'s "a blank Fly secret must not
        silently sail past validation" rationale.
        """
        stripped = v.strip()
        if not stripped:
            raise ValueError(
                "TWILIO_ACCOUNT_SID must not be blank -- required to construct "
                "the outbound Twilio client (app/integrations/twilio_send.py, #108)."
            )
        return stripped

    @model_validator(mode="after")
    def _require_valid_twilio_account_sid_format_in_production(self) -> Settings:
        """Refuse to boot in production unless ``twilio_account_sid`` is
        non-empty AND shaped like a real Twilio Account SID (``AC`` +
        32 hex characters, 34 characters total) — safety review, 2026-07-12,
        finding 7. A misconfigured/placeholder value (e.g. copy-pasted from
        ``.env.example``, or truncated) would otherwise sail past startup
        and only surface as a confusing 401 from Twilio's API the first
        time the emergency chain tries to place a real call or send a real
        SMS — precisely the worst moment to discover a config typo. Dev/
        test keep using loosely-shaped placeholders unchanged (this check
        is production-only, mirroring
        ``_require_public_base_url_in_production``'s exact gating pattern).
        No secrets in this message — the SID itself is not secret (it is
        Twilio's public account identifier, always sent in the clear as
        part of the REST URL/Basic-Auth username), but is still never
        echoed here on principle (same discipline as every other boot-gate
        message in this class).
        """
        if self.environment != "production":
            return self
        sid = self.twilio_account_sid
        if not _TWILIO_ACCOUNT_SID_RE.match(sid):
            raise ValueError(
                "TWILIO_ACCOUNT_SID does not look like a real Twilio Account SID "
                "(expected 'AC' followed by 32 hex characters) -- refusing to boot "
                "in production with what looks like a placeholder or typo'd value."
            )
        return self

    # ------------------------------------------------------------------
    # Production boot gate (safety review, #251 F2a — dashboard_origins)
    # -- declared LAST among the `@model_validator(mode="after")` methods
    # in this class, deliberately: pydantic v2 runs them as a fail-fast
    # chain in DECLARATION order (the first one to raise is the ONLY
    # error reported -- verified empirically while building this gate),
    # so this must run AFTER _require_app_database_url_in_production /
    # _require_public_base_url_in_production /
    # _require_valid_twilio_account_sid_format_in_production above, or it
    # would mask THEIR error messages for any production Settings
    # construction that also happens to leave DASHBOARD_ORIGINS at its
    # (production-invalid) localhost default -- exactly the failure mode
    # that broke several pre-existing tests in tests/test_config.py the
    # first time this validator was declared earlier in the class.
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def _require_non_local_dashboard_origins_in_production(self) -> Settings:
        """Refuse to boot in production with an empty CORS allowlist, one
        that still points at a local dev origin, or one that isn't
        ``https://`` (safety review, #251 F2a; #255 N1) -- mirrors
        ``_require_app_database_url_in_production``'s /
        ``_require_public_base_url_in_production``'s exact gating pattern
        (production-only; dev/staging keep the documented local-dev
        fallback unchanged).

        Verified empirically: ``ENVIRONMENT=production`` with
        ``DASHBOARD_ORIGINS`` left UNSET booted clean on the dev default
        (``http://localhost:5173,http://localhost:3000``) before this
        gate existed -- a production deploy would silently serve CORS
        headers for two origins that can never be a real browser's
        ``Origin`` in production, i.e. an allowlist that is effectively
        empty, with no startup signal. This gate closes that specific
        "boots clean, dashboard is silently bricked" failure mode by
        checking each entry's HOST (the part between ``://`` and an
        optional ``:port``, after ``_validate_dashboard_origin_shapes``
        above has already guaranteed that shape) against ``localhost``/
        ``127.``-prefixed loopback addresses.

        #255 N1 (safety re-verify of #251/#254): the checks above still
        let a plaintext ``http://`` production origin through -- anyone in
        a network position to control that origin (e.g. on the same
        network as, or upstream of, a landlord's browser) could read this
        API's authenticated responses cross-origin. Every entry must start
        ``https://`` in production; dev/staging keep using
        ``http://localhost:...`` unchanged. Checked AFTER the localhost
        check above (not before) so an entry that is both local AND
        plaintext (e.g. the dev default itself) still raises the more
        specific "local dev origin" message.
        """
        if self.environment != "production":
            return self
        origins = self.dashboard_origins_list
        if not origins:
            raise ValueError(
                "DASHBOARD_ORIGINS is required when ENVIRONMENT=production "
                "(#251) -- refusing to boot with an empty CORS allowlist; "
                "the dashboard would be unable to read ANY response."
            )
        for origin in origins:
            host = origin.split("://", 1)[-1].split(":", 1)[0]
            if host == "localhost" or host.startswith("127."):
                raise ValueError(
                    f"DASHBOARD_ORIGINS entry {origin!r} looks like a local dev "
                    "origin (localhost/127.0.0.1) -- refusing to boot in "
                    "production with a local origin still in the CORS "
                    "allowlist (#251)."
                )
            if not origin.startswith("https://"):
                raise ValueError(
                    f"DASHBOARD_ORIGINS entry {origin!r} is not https:// -- "
                    "refusing to boot in production with a plaintext CORS "
                    "origin (#255); anyone in a network position to control "
                    "that origin could read this API's authenticated "
                    "responses cross-origin."
                )
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached ``Settings`` singleton.

    The ``@lru_cache`` ensures env vars are read exactly once per process.
    Tests can call ``get_settings.cache_clear()`` to force a fresh read.
    """
    return Settings()


# Module-level singleton — imported by the rest of the application.
# Constructed once at import time; a missing required var raises
# ``pydantic.ValidationError`` immediately (fast-fail at startup).
settings: Settings = get_settings()
