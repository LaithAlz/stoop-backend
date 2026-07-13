"""``app/agent/draft_sender.py`` — the draft-flow half of the "one send
seam" fence (``apps/api/CLAUDE.md``: sends happen ONLY via the draft flow
or the emergency safety path — there is no third call site). This module
is the FIRST outbound-send call site this codebase has ever had (#108's
parallel branch adds the SECOND, the emergency safety path).

What it does
------------
A periodic ticker that claims ``drafts`` rows the approve/edit-and-send
finalize path (``app/agent/nodes/finalize_draft_decision.py``) marked
``'approved'`` with a ``scheduled_send_at`` that has since come due,
drains them through an INJECTABLE :class:`app.integrations.sms_sender.
SmsSender`, and writes every durable side effect of an actual send: the
outbound ``messages`` row, ``drafts.sent_message_id`` + ``status='sent'``,
``cases.status='awaiting_tenant'``, the ``trust_metrics`` clean-vs-edited
increment, and the ``audit_log`` ``'sent'`` row.

The undo window is data, not a sleep (schema-v1.md's own phrase) — this
module never sleeps waiting for a specific draft; it only ever asks
"which approved rows are due right now" and claims exactly those.

Idempotent claim — single-flight per row (skill doc Phase 3's own
obligation)
------------------------------------------------------------------------
:data:`_CLAIM_DRAFT_SQL` is the ONE atomic conditional UPDATE that decides
who gets to process a given draft: ``UPDATE drafts SET status='sending'
WHERE id=:id AND status='approved' AND scheduled_send_at <= now()
RETURNING id`` — matching a candidate SELECT to zero rows (lost the race)
is a silent, correct no-op; two overlapping ticks (or two process
instances) can never both win the SAME row. Crash-safety follows from this
alone: an ``approved`` row with a due ``scheduled_send_at`` survives a
process restart untouched (it is DB state, not in-memory schedule state)
and the next tick claims it exactly once.

Crash/failure semantics — a stuck ``'sending'`` row is the DESIGNED
failure mode, never a silent double-send
------------------------------------------------------------------------
Once a row is claimed (flipped to ``'sending'``), a crash or a raised
exception from :class:`SmsSender` before the final write-transaction
commits leaves that row stuck at ``'sending'`` forever (this issue does
not add a retry/timeout sweep for stuck rows — out of scope; flagged for a
future issue). This is DELIBERATE and matches the skill doc's own
"Expected numbers" section verbatim: "a crash between claim and
Twilio-ack is surfaced as a stuck ``sending`` row, never a silent
double-send." A stuck row is visible (queryable, an operational signal);
a duplicate outbound SMS to a tenant is not recoverable at all. Never
retried automatically here — the fenced-off alternative (a query
that resends 'sending' rows past some age) would risk exactly the
double-send this design avoids without a `twilio_sid` idempotency key from
the provider, which this issue does not have.

The edited/empty-``final_body`` guard (safety review, this round)
--------------------------------------------------------------------
An edited draft (``edited=true``) whose ``final_body`` is somehow empty
(structurally shouldn't happen — routers/drafts.py's edit-and-send handler
always sets both together — but this module never assumes that elsewhere)
is refused outright: logged loudly and Sentry-paged, the row left stuck
``'sending'`` (same stuck-row semantics as any other send failure above) —
NEVER silently falling back to ``drafts.body`` (the ORIGINAL text the
landlord explicitly edited away). Sending the original text back after a
landlord deliberately replaced it would be a silent, wrong-content send —
strictly worse than a stuck row.

Session discipline — never hold a DB connection across the network call
------------------------------------------------------------------------
Mirrors every other node in this package (e.g. ``draft_response.py``'s own
"never hold a pooled connection across a slow external call"): claim (own
short transaction) -> read recipient/case context (own short transaction)
-> call :meth:`SmsSender.send_sms` OUTSIDE any open session -> write the
final durable state (one more short transaction). A slow/hanging Twilio
call therefore never pins a pooled connection.

Wiring (#108 integration, landed)
------------------------------------------------------------------------
:func:`sender_tick` — one tick, not the standalone infinite loop below —
is called from ``app/scheduler.py``'s existing 60s ticker, alongside the
emergency escalation chain sweep and the degraded-mode retry sweep, using
:func:`app.integrations.sms_sender.get_default_sms_sender`'s real
Twilio-backed adapter. One scheduler owns all periodic work; this module
never starts its own competing lifespan task. :func:`run_sender_loop`
(the standalone, independently-tickable loop with its own interval/
stop-event) remains as a fully-built, independently testable seam — kept
for its own test coverage and as a documented alternative wiring, but is
NOT invoked from ``app/main.py`` or ``app/scheduler.py``; the scheduler
calls :func:`sender_tick` directly instead.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import sentry_sdk
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_admin_session
from app.integrations.sms_sender import SmsSender

log = structlog.get_logger(__name__)

DEFAULT_BATCH_SIZE = 25
"""Candidates read per tick — bounded so one tick can never hold the
candidate-selection connection open indefinitely under a large backlog."""

DEFAULT_INTERVAL_SECONDS = 2.0
"""Short relative to the 5s undo window on purpose — a due 'approved' row
should be picked up promptly, not up to a minute late (contrast with
#108's 60s-class escalation-chain sweep, a different domain with a much
coarser due-time granularity)."""

# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

_SELECT_DUE_DRAFT_IDS_SQL = text(
    "SELECT id FROM drafts WHERE status = 'approved' AND scheduled_send_at <= now() "
    "ORDER BY scheduled_send_at LIMIT :limit"
)

_CLAIM_DRAFT_SQL = text(
    "UPDATE drafts SET status = 'sending', updated_at = now() "
    "WHERE id = :draft_id AND status = 'approved' AND scheduled_send_at <= now() "
    "RETURNING id, case_id, recipient, body, final_body, edited, landlord_id"
)

_SELECT_CASE_FOR_SEND_SQL = text(
    "SELECT c.property_id, c.severity, c.tenant_id, c.vendor_id, p.twilio_number "
    "FROM cases c JOIN properties p ON p.id = c.property_id "
    "WHERE c.id = :case_id"
)

_SELECT_TENANT_PHONE_SQL = text("SELECT phone FROM tenants WHERE id = :tenant_id")
_SELECT_VENDOR_PHONE_SQL = text("SELECT phone FROM vendors WHERE id = :vendor_id")

_INSERT_OUTBOUND_MESSAGE_SQL = text(
    "INSERT INTO messages (landlord_id, property_id, tenant_id, vendor_id, case_id, "
    "direction, party, body, twilio_sid) "
    "VALUES (:landlord_id, :property_id, :tenant_id, :vendor_id, :case_id, 'outbound', "
    ":party, :body, :twilio_sid) "
    "RETURNING id"
)

_MARK_DRAFT_SENT_SQL = text(
    "UPDATE drafts SET status = 'sent', sent_message_id = :message_id, updated_at = now() "
    "WHERE id = :draft_id"
)

_MARK_CASE_AWAITING_TENANT_SQL = text(
    "UPDATE cases SET status = 'awaiting_tenant', last_activity_at = now(), updated_at = now() "
    "WHERE id = :case_id"
)

_UPSERT_TRUST_METRICS_SQL = text(
    "INSERT INTO trust_metrics "
    "(landlord_id, property_id, severity, clean_approvals, edited_approvals) "
    "VALUES (:landlord_id, :property_id, :severity, :clean_inc, :edited_inc) "
    "ON CONFLICT (property_id, severity) DO UPDATE SET "
    "clean_approvals = trust_metrics.clean_approvals + EXCLUDED.clean_approvals, "
    "edited_approvals = trust_metrics.edited_approvals + EXCLUDED.edited_approvals, "
    "updated_at = now()"
)

_INSERT_SENT_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'system', 'sent', CAST(:payload AS jsonb))"
)


async def _claim_draft(session: AsyncSession, draft_id: UUID) -> dict[str, Any] | None:
    row = (
        (await session.execute(_CLAIM_DRAFT_SQL, {"draft_id": str(draft_id)}))
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


async def _load_recipient_context(
    session: AsyncSession, *, case_id: UUID, recipient: str
) -> tuple[UUID, str | None, UUID | None, UUID | None, str | None, str | None]:
    """Returns ``(property_id, severity, tenant_id, vendor_id, to_e164,
    from_e164)`` — *to_e164* is ``None`` when the recipient's phone can't
    be resolved (defensive; structurally shouldn't happen given the
    schema's FK/NOT NULL shape, but this module never assumes it away).
    *from_e164* is the case's own property's ``twilio_number`` — ``None``
    until that property has one provisioned (schema-v1.md: nullable);
    see ``app/integrations/sms_sender.py``'s module docstring "Why
    ``from_e164`` is required" for why this must be the SENDING
    property's number, never any other property's or a landlord-wide
    default."""
    case_row = (
        (await session.execute(_SELECT_CASE_FOR_SEND_SQL, {"case_id": str(case_id)}))
        .mappings()
        .one()
    )
    property_id: UUID = case_row["property_id"]
    severity: str | None = case_row["severity"]
    tenant_id: UUID | None = case_row["tenant_id"]
    vendor_id: UUID | None = case_row["vendor_id"]
    from_e164: str | None = case_row["twilio_number"]

    to_e164: str | None = None
    if recipient == "tenant" and tenant_id is not None:
        trow = (
            (await session.execute(_SELECT_TENANT_PHONE_SQL, {"tenant_id": str(tenant_id)}))
            .mappings()
            .one_or_none()
        )
        to_e164 = trow["phone"] if trow is not None else None
    elif recipient == "vendor" and vendor_id is not None:
        vrow = (
            (await session.execute(_SELECT_VENDOR_PHONE_SQL, {"vendor_id": str(vendor_id)}))
            .mappings()
            .one_or_none()
        )
        to_e164 = vrow["phone"] if vrow is not None else None

    return property_id, severity, tenant_id, vendor_id, to_e164, from_e164


async def _process_claimed_draft(sender: SmsSender, claimed: dict[str, Any]) -> None:
    """Everything that happens to an ALREADY-CLAIMED (``status='sending'``)
    draft — see module docstring "Session discipline" and "Crash/failure
    semantics". Never raises: every failure path logs (never phone/body,
    rule #5) and returns, leaving the row ``'sending'`` rather than
    propagating out and killing the whole tick's batch."""
    draft_id: UUID = claimed["id"]
    case_id: UUID = claimed["case_id"]
    recipient: str = claimed["recipient"]
    edited: bool = claimed["edited"]
    landlord_id: UUID = claimed["landlord_id"]

    if edited and not claimed["final_body"]:
        # Defensive (safety review, this round): an edited draft with no
        # final_body is a structural invariant violation (routers/
        # drafts.py's edit-and-send handler always sets both together) —
        # NEVER silently fall back to `body` (the original text the
        # landlord explicitly replaced). Loud, never a silent send of the
        # wrong text.
        log.error(
            "draft_sender_edited_draft_missing_final_body",
            draft_id=str(draft_id),
            case_id=str(case_id),
        )
        sentry_sdk.capture_message(
            "draft_sender: edited draft has no final_body -- refusing to send the "
            "original, unedited text",
            level="error",
            extras={"draft_id": str(draft_id), "case_id": str(case_id)},
        )
        return
    body: str = claimed["final_body"] if edited else claimed["body"]

    async with asynccontextmanager(get_admin_session)() as session:
        (
            property_id,
            severity,
            tenant_id,
            vendor_id,
            to_e164,
            from_e164,
        ) = await _load_recipient_context(session, case_id=case_id, recipient=recipient)

    if to_e164 is None:
        log.error(
            "draft_sender_no_recipient_phone",
            draft_id=str(draft_id),
            case_id=str(case_id),
            recipient=recipient,
        )
        return

    if from_e164 is None:
        # Mirrors app/agent/emergency_chain.py's own "no_twilio_number"
        # skip reason: a property with no twilio_number provisioned yet
        # must never send from a DIFFERENT property's number or a
        # fabricated placeholder — see app/integrations/sms_sender.py's
        # module docstring "Why from_e164 is required".
        log.error(
            "draft_sender_no_property_twilio_number",
            draft_id=str(draft_id),
            case_id=str(case_id),
        )
        return

    try:
        twilio_sid = await sender.send_sms(to_e164=to_e164, from_e164=from_e164, body=body)
    except Exception as exc:
        log.error(
            "draft_sender_send_failed",
            draft_id=str(draft_id),
            case_id=str(case_id),
            exc_type=type(exc).__name__,
        )
        return

    message_tenant_id = str(tenant_id) if recipient == "tenant" and tenant_id else None
    message_vendor_id = str(vendor_id) if recipient == "vendor" and vendor_id else None

    async with asynccontextmanager(get_admin_session)() as session:
        message_row = (
            (
                await session.execute(
                    _INSERT_OUTBOUND_MESSAGE_SQL,
                    {
                        "landlord_id": str(landlord_id),
                        "property_id": str(property_id),
                        "tenant_id": message_tenant_id,
                        "vendor_id": message_vendor_id,
                        "case_id": str(case_id),
                        "party": recipient,
                        "body": body,
                        "twilio_sid": twilio_sid,
                    },
                )
            )
            .mappings()
            .one()
        )
        message_id = message_row["id"]

        await session.execute(
            _MARK_DRAFT_SENT_SQL, {"draft_id": str(draft_id), "message_id": str(message_id)}
        )
        await session.execute(_MARK_CASE_AWAITING_TENANT_SQL, {"case_id": str(case_id)})

        if severity is not None:
            clean_inc, edited_inc = (0, 1) if edited else (1, 0)
            await session.execute(
                _UPSERT_TRUST_METRICS_SQL,
                {
                    "landlord_id": str(landlord_id),
                    "property_id": str(property_id),
                    "severity": severity,
                    "clean_inc": clean_inc,
                    "edited_inc": edited_inc,
                },
            )
        else:  # pragma: no cover — defensive; draft_response never leaves severity unset
            log.error("draft_sender_missing_severity_for_trust_metrics", case_id=str(case_id))

        await session.execute(
            _INSERT_SENT_AUDIT_SQL,
            {
                "landlord_id": str(landlord_id),
                "case_id": str(case_id),
                "payload": json.dumps(
                    {"draft_id": str(draft_id), "message_id": str(message_id), "edited": edited}
                ),
            },
        )

    log.info("draft_sender_sent", draft_id=str(draft_id), case_id=str(case_id), edited=edited)


async def sender_tick(*, sender: SmsSender, batch_size: int = DEFAULT_BATCH_SIZE) -> int:
    """One tick: claims and processes every ``'approved'`` draft whose
    ``scheduled_send_at`` is due, up to *batch_size*. Returns the number of
    drafts this tick actually WON the claim race for (regardless of
    whether each one's send ultimately succeeded — see
    :func:`_process_claimed_draft`'s own per-draft failure handling).

    Safe to call repeatedly / concurrently: claiming is the single atomic
    conditional UPDATE described in the module docstring, so two
    overlapping ticks (same process or two process instances) can never
    both claim the same row.
    """
    async with asynccontextmanager(get_admin_session)() as session:
        candidate_rows = (
            (await session.execute(_SELECT_DUE_DRAFT_IDS_SQL, {"limit": batch_size}))
            .mappings()
            .all()
        )
    candidate_ids: list[UUID] = [row["id"] for row in candidate_rows]

    claimed_count = 0
    for draft_id in candidate_ids:
        async with asynccontextmanager(get_admin_session)() as claim_session:
            claimed = await _claim_draft(claim_session, draft_id)
        if claimed is None:
            # Lost the claim race (another tick/process instance already
            # claimed it) — a silent, correct no-op, never an error.
            continue
        claimed_count += 1
        await _process_claimed_draft(sender, claimed)
    return claimed_count


async def run_sender_loop(
    *,
    sender: SmsSender | None,
    interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
    stop_event: asyncio.Event | None = None,
) -> None:
    """The in-process periodic task (skill doc Phase 3, option (a)). See
    module docstring "Wiring (#108 integration, landed)" — NOT invoked
    from ``app/main.py`` or ``app/scheduler.py`` today; the scheduler calls
    :func:`sender_tick` directly instead. Kept as a fully-built,
    independently testable/usable alternative wiring.

    Ticks :func:`sender_tick` every *interval_seconds* until *stop_event*
    is set (runs forever if no *stop_event* is supplied — a caller that
    wants to stop this loop must supply one and ``set()`` it, e.g. on
    application shutdown).

    Deployment gating (matches #109's own pattern — see
    ``app/integrations/sms_sender.py``'s module docstring): if *sender* is
    ``None``, this loop refuses to run at all — logs ONE loud error and
    returns immediately, rather than looping forever doing nothing or
    (worse) ever reaching a call site that could invoke
    :class:`app.integrations.sms_sender.NotImplementedSmsSender`. No
    production caller passes ``None`` today (``get_default_sms_sender``
    always returns a real binding), but this contract stays enforced for
    any future caller that does.
    """
    if sender is None:
        log.error(
            "draft_sender_worker_disabled",
            detail=(
                "no SmsSender configured -- approved drafts will wait until a real "
                "binding is wired in (app/integrations/sms_sender.py's module docstring)"
            ),
        )
        return

    log.info("draft_sender_worker_started", interval_seconds=interval_seconds)
    while stop_event is None or not stop_event.is_set():
        try:
            await sender_tick(sender=sender)
        except Exception as exc:  # never let one bad tick kill the loop
            log.error("draft_sender_tick_failed", exc_type=type(exc).__name__)

        if stop_event is None:
            await asyncio.sleep(interval_seconds)
            continue
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            pass


__all__: list[str] = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_INTERVAL_SECONDS",
    "run_sender_loop",
    "sender_tick",
]
