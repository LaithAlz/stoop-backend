"""Unit test for the error-envelope merge order (#44/#45 safety review,
LOW): ``exc.extra`` must be spread BEFORE the three reserved keys
(``code``/``message``/``request_id``) in ``app/main.py``'s
``_app_error_handler``, so a caller can never override the
statically-reviewed message via ``extra``.

Marker: ``unit`` (default) — no DB, no network.
"""

from __future__ import annotations

import json

from app.errors import AppError
from app.main import _app_error_handler


def test_app_error_extra_can_never_override_reserved_keys() -> None:
    exc = AppError(
        status_code=409,
        code="draft_stale",
        message="Real, statically-reviewed message.",
        extra={
            "code": "hijacked",
            "message": "hijacked",
            "request_id": "hijacked",
            "fresh_draft_id": "abc-123",
        },
    )

    response = _app_error_handler(None, exc)  # type: ignore[arg-type]
    body = json.loads(response.body)

    assert body["error"]["code"] == "draft_stale"
    assert body["error"]["message"] == "Real, statically-reviewed message."
    # The endpoint-specific extra field IS present -- only the three
    # reserved keys are protected from an override.
    assert body["error"]["fresh_draft_id"] == "abc-123"
