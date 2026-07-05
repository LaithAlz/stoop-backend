"""Unit tests for app.integrations.twilio — signature verification + URL
reconstruction. Pure functions, no I/O, no database, no app import needed
beyond the module under test.

The ``test_compute_signature_matches_twilio_documented_example`` test vector
below is Twilio's own published example (not just self-consistency): url
"https://mycompany.com/myapp.php?foo=1&bar=2", auth token "12345", and the
five listed POST params produce the documented signature
"RSOYDt4T1cUTdK1PDd93/VVr8B8=" — this is checked against our
``compute_signature`` unchanged, so a regression in the algorithm itself
(not just internal consistency) would be caught here.
"""

from __future__ import annotations

from starlette.requests import Request

from app.integrations.twilio import (
    compute_signature,
    reconstruct_signing_url,
    verify_signature,
)

# ---------------------------------------------------------------------------
# compute_signature / verify_signature
# ---------------------------------------------------------------------------

_TWILIO_DOC_AUTH_TOKEN = "12345"  # noqa: S105 -- Twilio's own published doc example, not a secret
_TWILIO_DOC_URL = "https://mycompany.com/myapp.php?foo=1&bar=2"
_TWILIO_DOC_PARAMS = {
    "CallSid": "CA1234567890ABCDE",
    "Caller": "+14158675309",
    "Digits": "1234",
    "From": "+14158675309",
    "To": "+18005551212",
}
_TWILIO_DOC_SIGNATURE = "RSOYDt4T1cUTdK1PDd93/VVr8B8="


def test_compute_signature_matches_twilio_documented_example() -> None:
    assert (
        compute_signature(_TWILIO_DOC_URL, _TWILIO_DOC_PARAMS, _TWILIO_DOC_AUTH_TOKEN)
        == _TWILIO_DOC_SIGNATURE
    )


def test_verify_signature_accepts_valid_signature() -> None:
    assert verify_signature(
        _TWILIO_DOC_URL, _TWILIO_DOC_PARAMS, _TWILIO_DOC_SIGNATURE, _TWILIO_DOC_AUTH_TOKEN
    )


def test_verify_signature_rejects_wrong_signature() -> None:
    assert not verify_signature(
        _TWILIO_DOC_URL, _TWILIO_DOC_PARAMS, "not-the-right-signature==", _TWILIO_DOC_AUTH_TOKEN
    )


def test_verify_signature_rejects_missing_signature() -> None:
    assert not verify_signature(_TWILIO_DOC_URL, _TWILIO_DOC_PARAMS, None, _TWILIO_DOC_AUTH_TOKEN)
    assert not verify_signature(_TWILIO_DOC_URL, _TWILIO_DOC_PARAMS, "", _TWILIO_DOC_AUTH_TOKEN)


def test_verify_signature_rejects_empty_auth_token() -> None:
    """Fail closed: an empty/unconfigured auth token must never be treated
    as "skip verification"."""
    assert not verify_signature(_TWILIO_DOC_URL, _TWILIO_DOC_PARAMS, _TWILIO_DOC_SIGNATURE, "")


def test_verify_signature_rejects_tampered_param_value() -> None:
    tampered = dict(_TWILIO_DOC_PARAMS)
    tampered["Digits"] = "9999"
    assert not verify_signature(
        _TWILIO_DOC_URL, tampered, _TWILIO_DOC_SIGNATURE, _TWILIO_DOC_AUTH_TOKEN
    )


def test_verify_signature_rejects_added_param() -> None:
    tampered = dict(_TWILIO_DOC_PARAMS)
    tampered["ExtraParam"] = "sneaky"
    assert not verify_signature(
        _TWILIO_DOC_URL, tampered, _TWILIO_DOC_SIGNATURE, _TWILIO_DOC_AUTH_TOKEN
    )


def test_verify_signature_rejects_removed_param() -> None:
    tampered = dict(_TWILIO_DOC_PARAMS)
    del tampered["Digits"]
    assert not verify_signature(
        _TWILIO_DOC_URL, tampered, _TWILIO_DOC_SIGNATURE, _TWILIO_DOC_AUTH_TOKEN
    )


def test_verify_signature_rejects_different_url() -> None:
    assert not verify_signature(
        "https://mycompany.com/myapp.php?foo=1&bar=3",
        _TWILIO_DOC_PARAMS,
        _TWILIO_DOC_SIGNATURE,
        _TWILIO_DOC_AUTH_TOKEN,
    )


def test_verify_signature_rejects_wrong_auth_token() -> None:
    assert not verify_signature(
        _TWILIO_DOC_URL, _TWILIO_DOC_PARAMS, _TWILIO_DOC_SIGNATURE, "wrong-token"
    )


def test_verify_signature_is_order_independent_over_params() -> None:
    """Params are sorted internally — a Mapping presented in a different
    iteration order must produce the identical signature."""
    reordered = dict(reversed(list(_TWILIO_DOC_PARAMS.items())))
    assert (
        compute_signature(_TWILIO_DOC_URL, reordered, _TWILIO_DOC_AUTH_TOKEN)
        == _TWILIO_DOC_SIGNATURE
    )


# ---------------------------------------------------------------------------
# reconstruct_signing_url
# ---------------------------------------------------------------------------


def _make_request(
    *,
    path: str = "/webhooks/twilio/sms",
    query: str = "",
    headers: list[tuple[str, str]] | None = None,
    scheme: str = "http",
    server: tuple[str, int] = ("testserver", 80),
) -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "query_string": query.encode(),
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or [])],
        "scheme": scheme,
        "server": server,
    }
    return Request(scope)  # type: ignore[arg-type]


def test_reconstruct_signing_url_uses_public_base_url_when_set() -> None:
    request = _make_request(headers=[("host", "internal-fly-machine:8080")])
    url = reconstruct_signing_url(request, public_base_url="https://api.stoop.example")
    assert url == "https://api.stoop.example/webhooks/twilio/sms"


def test_reconstruct_signing_url_public_base_url_strips_trailing_slash() -> None:
    request = _make_request()
    url = reconstruct_signing_url(request, public_base_url="https://api.stoop.example/")
    assert url == "https://api.stoop.example/webhooks/twilio/sms"


def test_reconstruct_signing_url_public_base_url_includes_query() -> None:
    request = _make_request(query="foo=1&bar=2")
    url = reconstruct_signing_url(request, public_base_url="https://api.stoop.example")
    assert url == "https://api.stoop.example/webhooks/twilio/sms?foo=1&bar=2"


def test_reconstruct_signing_url_falls_back_to_forwarded_headers() -> None:
    """When public_base_url is unset, X-Forwarded-Proto/Host from the
    trusted proxy hop (Fly.io) are honored."""
    request = _make_request(
        headers=[
            ("x-forwarded-proto", "https"),
            ("x-forwarded-host", "api.stoop.example"),
            ("host", "internal-fly-machine:8080"),
        ],
    )
    url = reconstruct_signing_url(request, public_base_url=None)
    assert url == "https://api.stoop.example/webhooks/twilio/sms"


def test_reconstruct_signing_url_falls_back_to_plain_host_header() -> None:
    """No forwarded headers at all -- falls back to scheme + Host header."""
    request = _make_request(headers=[("host", "testserver")], scheme="http")
    url = reconstruct_signing_url(request, public_base_url=None)
    assert url == "http://testserver/webhooks/twilio/sms"


def test_reconstruct_signing_url_falls_back_to_url_netloc_with_no_headers() -> None:
    request = _make_request(headers=[])
    url = reconstruct_signing_url(request, public_base_url=None)
    assert url == "http://testserver/webhooks/twilio/sms"
