"""Twilio inbound-webhook signature verification — pure functions, no I/O.

Implements Twilio's documented request-validation algorithm exactly:

    signature = base64(
        HMAC-SHA1(
            auth_token,
            url + "".join(sorted(f"{name}{value}" for name, value in params))
        )
    )

compared to the ``X-Twilio-Signature`` request header with a constant-time
comparison. Used by both ``/webhooks/twilio/sms`` (#40) and
``/webhooks/twilio/status`` (#152) — every inbound Twilio request is
rejected with 403 BEFORE any parsing/persistence if this fails (never-break
rule #5: the auth token and the signature itself are never logged, only
whether verification passed).

``compute_signature``/``verify_signature`` take the auth token as an
explicit parameter rather than reading ``app.config.settings`` internally —
this keeps them pure and trivially testable with a fake token, per the
project convention (see ``app/agent/prefilter.py``'s ``check()``).

URL reconstruction (``reconstruct_signing_url``) is proxy-aware: Twilio
signs the EXACT url it was told to POST to, which is very often not what
``request.url`` reports once the request has passed through a reverse
proxy/load balancer (Fly.io terminates TLS at its edge, so the ASGI server
sees a plain-HTTP request from Fly's internal network unless forwarded
headers are honored). See that function's own docstring for the two
reconstruction modes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from collections.abc import Mapping

from fastapi import Request

_FORWARDED_PROTO_HEADER = "x-forwarded-proto"
_FORWARDED_HOST_HEADER = "x-forwarded-host"


def compute_signature(url: str, params: Mapping[str, str], auth_token: str) -> str:
    """Compute Twilio's expected ``X-Twilio-Signature`` for *url* + *params*.

    ``params`` should be every POST form parameter Twilio sent (as
    string values) — the full set, not a filtered subset; omitting or
    adding a parameter changes the signature and verification will fail.
    Parameters are sorted alphabetically by name and concatenated with no
    delimiter between name/value or between pairs, appended to *url*,
    then HMAC-SHA1'd with *auth_token* and base64-encoded — this is
    Twilio's documented algorithm, reproduced verbatim.

    Pure function: no I/O, no settings import, no exceptions raised for
    normal inputs.
    """
    data = url
    for key in sorted(params):
        data += key + params[key]
    digest = hmac.new(auth_token.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).digest()
    return base64.b64encode(digest).decode("ascii")


def verify_signature(
    url: str,
    params: Mapping[str, str],
    signature: str | None,
    auth_token: str,
) -> bool:
    """Constant-time verification of *signature* against the expected value.

    Returns ``False`` (never raises) when:
    - *signature* is missing/empty (no ``X-Twilio-Signature`` header), or
    - *auth_token* is empty (fail closed — never treat "no token
      configured" as "skip verification"), or
    - the computed signature does not match.

    Uses ``hmac.compare_digest`` for the final comparison — never a plain
    ``==``, which would leak timing information about how many leading
    bytes matched.
    """
    if not signature or not auth_token:
        return False
    expected = compute_signature(url, params, auth_token)
    return hmac.compare_digest(expected, signature)


def reconstruct_signing_url(request: Request, *, public_base_url: str | None) -> str:
    """Reconstruct the exact URL Twilio used to compute its signature.

    Two modes:

    - **``public_base_url`` set** (recommended once Fly is configured with
      it — the safer, deployment-config-driven mode): the signed url is
      ``public_base_url.rstrip("/") + request.url.path`` (+ ``?query`` if
      present). This does not depend on trusting any request header at
      all — the operator configures ``public_base_url`` to match exactly
      what Twilio's console has on file as the webhook URL, so this mode
      is correct regardless of how many hops sit in front of the app.

    - **``public_base_url`` unset** (local dev / until that config lands):
      falls back to ``request.url``, honoring ``X-Forwarded-Proto`` /
      ``X-Forwarded-Host`` when present. This is safe ONLY because Fly.io
      terminates TLS at its own edge and is the single, trusted proxy hop
      in front of this app today — there is no untrusted intermediary
      that could forge these headers before they reach us. If a future
      deployment topology ever adds an additional, less-trusted proxy
      layer in front of Fly, this fallback must be revisited (prefer
      setting ``public_base_url`` explicitly rather than trusting
      headers from an untrusted hop).
    """
    query = f"?{request.url.query}" if request.url.query else ""

    if public_base_url:
        base = public_base_url.rstrip("/")
        return f"{base}{request.url.path}{query}"

    proto = request.headers.get(_FORWARDED_PROTO_HEADER) or request.url.scheme
    host = (
        request.headers.get(_FORWARDED_HOST_HEADER)
        or request.headers.get("host")
        or request.url.netloc
    )
    return f"{proto}://{host}{request.url.path}{query}"
