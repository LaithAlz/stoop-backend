"""Generic application-level error → standard error envelope.

Any route handler can raise ``AppError`` to short-circuit a request with a
non-2xx response using the error envelope defined in
``docs/03-engineering/api-contracts.md``::

    {"error": {"code": "...", "message": "...", "request_id": "..."}}

This is distinct from ``AuthError`` (``app/integrations/supabase_auth.py``),
which is raised exclusively by JWT verification and always maps to 401.
``AppError`` carries its own ``status_code`` so a handler can express any
business-rule failure (403, 404, 409, ...) through the same envelope
machinery instead of each router inventing its own response shape.

Security: never construct an ``AppError`` with token, phone number, email
address, ``sub``/``auth_user_id``, or message-body content in ``message`` —
same never-log rule as ``AuthError`` (never-break rule #5). ``message``
must always be a static, module-level string constant (or a literal passed
at the call site) — never built by interpolating request/claim/DB data into
it. Any per-request detail belongs in ``code`` (a stable, non-identifying
snake_case string), never in ``message``; this keeps ``message`` reviewable
once, statically, instead of re-auditing every call site for what it might
interpolate.

``extra`` (#44/#45): an OPTIONAL dict of additional, JSON-serializable
fields merged into the ``"error"`` envelope object alongside ``code``/
``message``/``request_id`` — e.g. ``docs/03-engineering/api-contracts.md``'s
``POST /v1/drafts/{id}/approve`` 409 ``draft_stale`` response, whose body
"includes ``fresh_draft_id``". Same never-PII rule as ``message`` — a uuid
(or ``None``) is the only shape any current call site uses.
"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Raised by route handlers for a business-rule (non-auth) failure.

    ``status_code`` — the HTTP status code to return.
    ``code`` — a stable snake_case string used in the JSON error envelope.
    ``message`` — human-readable and intentionally generic; must be a static
    string (never interpolated with user/claim/DB data) and must never
    contain token, phone number, email address, ``sub``/``auth_user_id``, or
    message-body material.
    ``extra`` — optional additional envelope fields (see module docstring).
    """

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra: dict[str, Any] = extra or {}
