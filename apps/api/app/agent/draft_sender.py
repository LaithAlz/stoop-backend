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
increment (plus, #60: the ``consecutive_clean`` streak counter and the
graduation write once a ``'routine'`` streak reaches
``settings.trust_graduation_threshold`` — see "Trust ladder graduation
(#60)" below), and the ``audit_log`` ``'sent'`` row.

Trust ladder graduation (#60)
------------------------------
Every send through this ticker (regardless of whether a landlord approved
it or the trust ladder auto-sent it — this module makes no distinction,
it drains ANY due ``'approved'`` row) upserts the SAME ``trust_metrics``
row this module already maintained: a clean (unedited) send increments
``consecutive_clean``; an edited send resets it to 0. Immediately after
that upsert, for a clean ``'routine'`` send ONLY, a second, atomic UPDATE
(:data:`_GRADUATE_ROUTINE_TRUST_SQL`) checks whether the streak has
reached :attr:`app.config.Settings.trust_graduation_threshold`
(FOUNDER-PROVISIONAL — see that setting's own docstring) and, if so, flips
``autonomy_unlocked = true`` + ``unlocked_at = now()`` (clearing any prior
``revoked_at``) and appends a ``trust_unlocked`` ``audit_log`` row
(``actor='system'``). ``'urgent'``/``'emergency'`` severities accumulate
the SAME counters (an inert streak that can never graduate — the
graduation query's own ``severity = 'routine'`` predicate is a hardcoded
SQL literal, not a bound parameter, per #60's PR #202 senior-review note:
"treat the emergency/urgent rows as inert counters").

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
calls :func:`sender_tick` directly instead. Because that call shares the
SAME ticker task as the other three sweeps, :func:`sender_tick` bounds its
own worst-case duration against a wall-clock deadline (see
:data:`DEFAULT_TICK_DEADLINE_SECONDS`) so a large send backlog can never
push the next tick — and thus the next emergency re-escalation sweep —
meaningfully late.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

import sentry_sdk
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_admin_session
from app.integrations.sms_sender import SmsSender
from app.trust import GRADUATION_SEVERITY

log = structlog.get_logger(__name__)

DEFAULT_BATCH_SIZE = 25
"""Candidates read per tick — bounded so one tick can never hold the
candidate-selection connection open indefinitely under a large backlog."""

DEFAULT_INTERVAL_SECONDS = 2.0
"""Short relative to the 5s undo window on purpose -- a due 'approved' row
should be picked up promptly, not up to a minute late. This rationale
applies ONLY to :func:`run_sender_loop`, the unused standalone seam (see
module docstring "Wiring") -- production dispatch does not use this
interval at all; it rides ``app/scheduler.py``'s existing 60s tick
instead, so a due row is picked up somewhere between ~0s and ~60s later in
production, never governed by this constant."""

DEFAULT_TICK_DEADLINE_SECONDS = 25.0
"""Wall-clock budget for one :func:`sender_tick` call (safety review,
MEDIUM finding: ``sender_tick`` shares the single scheduler ticker task
with the emergency chain sweep, the SMS drain sweep, and the degraded-mode
sweep, all of which must still run promptly every tick -- see
``app/scheduler.py``'s own module docstring "Bounding sender_tick's own
worst-case duration"). Up to :data:`DEFAULT_BATCH_SIZE` (25) candidates
each risking a 10s Twilio timeout (``app/integrations/twilio_send.py``'s
``AsyncTwilioHttpClient``) could otherwise push a single tick to ~250s,
delaying the NEXT tick (and thus the next emergency re-escalation sweep)
by the same amount. Once exceeded, :func:`sender_tick` stops CLAIMING new
drafts for the rest of that tick -- a draft already claimed and mid-send
always finishes (never abandoned mid-flight); any leftover due candidates
simply remain ``'approved'`` and due, picked up whole by the very next
tick. Nothing is lost -- matches this module's own "the undo window is
data, not a sleep" invariant."""

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
    "(landlord_id, property_id, severity, clean_approvals, edited_approvals, consecutive_clean) "
    "VALUES (:landlord_id, :property_id, :severity, :clean_inc, :edited_inc, "
    "CASE WHEN :edited THEN 0 ELSE 1 END) "
    "ON CONFLICT (property_id, severity) DO UPDATE SET "
    "clean_approvals = trust_metrics.clean_approvals + EXCLUDED.clean_approvals, "
    "edited_approvals = trust_metrics.edited_approvals + EXCLUDED.edited_approvals, "
    "consecutive_clean = CASE WHEN :edited THEN 0 ELSE trust_metrics.consecutive_clean + 1 END, "
    "updated_at = now() "
    "RETURNING consecutive_clean"
)

# #60 graduation — 'routine' is a LITERAL here, never a bound parameter
# (belt-and-braces: CLAUDE.md rule 3, "only for routine" — schema-v1.md's
# own trust_metrics.autonomy_unlocked comment, "only ever true for routine
# in v1"). `autonomy_unlocked = false` in the WHERE clause makes this fire
# AT MOST ONCE per graduation event — a row already unlocked never
# re-matches, so this never re-inserts a duplicate `trust_unlocked` audit
# row on every subsequent clean send. `revoked_at = NULL` clears any prior
# revocation (#60's own re-graduation semantics — app/trust.py's module
# docstring "Re-graduation semantics").
_GRADUATE_ROUTINE_TRUST_SQL = text(
    "UPDATE trust_metrics SET autonomy_unlocked = true, unlocked_at = now(), "
    "revoked_at = NULL, updated_at = now() "
    "WHERE property_id = :property_id AND severity = 'routine' "
    "AND consecutive_clean >= :threshold AND autonomy_unlocked = false "
    "RETURNING id"
)

_INSERT_TRUST_UNLOCKED_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'system', 'trust_unlocked', CAST(:payload AS jsonb))"
)

_INSERT_SENT_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'system', 'sent', CAST(:payload AS jsonb))"
)


def _default_time_source() -> float:
    """The real, monotonic clock :func:`sender_tick` budgets its wall-clock
    deadline against -- ``asyncio.get_running_loop().time()`` per the
    safety review's own wording ("computed from loop.time() at tick
    start"). Injectable so tests can advance a fake clock deterministically
    instead of sleeping for real seconds — see
    ``tests/test_agent_draft_sender.py``'s own deadline tests."""
    return asyncio.get_running_loop().time()


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
        # Safety review (MEDIUM): a stuck 'sending' row must never rely on
        # a log line alone to surface -- same paging discipline as the
        # edited/empty-final_body guard above (uuids only, never a phone
        # number or message body).
        sentry_sdk.capture_message(
            "draft_sender: no twilio_number provisioned for this property -- "
            "refusing to send, draft left stuck 'sending'",
            level="error",
            extras={"draft_id": str(draft_id), "case_id": str(case_id)},
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
        # Safety review (MEDIUM): a send failure must page, not just log --
        # this is the row a landlord's approved reply can otherwise die in
        # silence in (uuids/exception type only, never a phone number or
        # message body).
        sentry_sdk.capture_message(
            "draft_sender: send_sms raised -- draft left stuck 'sending'",
            level="error",
            extras={
                "draft_id": str(draft_id),
                "case_id": str(case_id),
                "exc_type": type(exc).__name__,
            },
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
            trust_row = (
                (
                    await session.execute(
                        _UPSERT_TRUST_METRICS_SQL,
                        {
                            "landlord_id": str(landlord_id),
                            "property_id": str(property_id),
                            "severity": severity,
                            "clean_inc": clean_inc,
                            "edited_inc": edited_inc,
                            "edited": edited,
                        },
                    )
                )
                .mappings()
                .one()
            )

            # #60 graduation — only ever attempted for 'routine' clean
            # sends (an edit always resets consecutive_clean to 0 above,
            # so it could never legitimately reach the threshold on the
            # SAME transaction anyway; skipping the query entirely for
            # edited/non-routine sends is a cheap belt-and-braces on top
            # of _GRADUATE_ROUTINE_TRUST_SQL's own hardcoded predicate).
            if severity == GRADUATION_SEVERITY and not edited:
                threshold = settings.trust_graduation_threshold
                graduated_row = (
                    (
                        await session.execute(
                            _GRADUATE_ROUTINE_TRUST_SQL,
                            {"property_id": str(property_id), "threshold": threshold},
                        )
                    )
                    .mappings()
                    .one_or_none()
                )
                if graduated_row is not None:
                    await session.execute(
                        _INSERT_TRUST_UNLOCKED_AUDIT_SQL,
                        {
                            "landlord_id": str(landlord_id),
                            "case_id": str(case_id),
                            "payload": json.dumps(
                                {
                                    "property_id": str(property_id),
                                    "severity": GRADUATION_SEVERITY,
                                    "threshold": threshold,
                                    "consecutive_clean": trust_row["consecutive_clean"],
                                }
                            ),
                        },
                    )
                    log.info(
                        "trust_ladder_graduated",
                        property_id=str(property_id),
                        case_id=str(case_id),
                    )
        else:
            # #197: cases.severity is now written by classify_severity (post
            # -clamp) for every case that has ever been through the graph,
            # so a NULL here is no longer the 100%-of-sends noise it used to
            # be before that write existed -- it now means either a case
            # created before #197 shipped (no backfill migration; NULL
            # stays legal, schema-v1.md) or a genuine anomaly (e.g. a case
            # whose classify_severity run somehow never reached the case-
            # -update, or the unknown-sender fallback thread producing a
            # case some other way). Either way trust_metrics silently loses
            # a data point for the trust ladder (#60) this table exists to
            # feed -- worth a page, not just a log line, same as every other
            # anomaly branch in this module.
            log.error("draft_sender_missing_severity_for_trust_metrics", case_id=str(case_id))
            sentry_sdk.capture_message(
                "draft_sender: cases.severity is NULL on send -- trust_metrics not "
                "incremented for this send",
                level="error",
                extras={"case_id": str(case_id), "draft_id": str(draft_id)},
            )

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


async def sender_tick(
    *,
    sender: SmsSender,
    batch_size: int = DEFAULT_BATCH_SIZE,
    deadline_seconds: float = DEFAULT_TICK_DEADLINE_SECONDS,
    time_source: Callable[[], float] = _default_time_source,
) -> int:
    """One tick: claims and processes every ``'approved'`` draft whose
    ``scheduled_send_at`` is due, up to *batch_size* -- bounded by
    *deadline_seconds* of wall-clock time from this call's own start (see
    :data:`DEFAULT_TICK_DEADLINE_SECONDS`). Returns the number of drafts
    this tick actually WON the claim race for (regardless of whether each
    one's send ultimately succeeded — see :func:`_process_claimed_draft`'s
    own per-draft failure handling).

    Safe to call repeatedly / concurrently: claiming is the single atomic
    conditional UPDATE described in the module docstring, so two
    overlapping ticks (same process or two process instances) can never
    both claim the same row.

    *time_source* defaults to the real event loop clock
    (:func:`_default_time_source`) — tests inject a fake, monotonically
    -advanceable callable instead of sleeping for real seconds.
    """
    start = time_source()
    async with asynccontextmanager(get_admin_session)() as session:
        candidate_rows = (
            (await session.execute(_SELECT_DUE_DRAFT_IDS_SQL, {"limit": batch_size}))
            .mappings()
            .all()
        )
    candidate_ids: list[UUID] = [row["id"] for row in candidate_rows]

    claimed_count = 0
    for index, draft_id in enumerate(candidate_ids):
        if time_source() - start >= deadline_seconds:
            # Wall-clock budget exceeded (safety review, MEDIUM) -- stop
            # CLAIMING new drafts for the rest of this tick. Every draft
            # claimed above already finished processing (this loop awaits
            # each one in turn, never abandoning a claimed row mid-send);
            # the remaining candidates stay 'approved' and due, claimed
            # whole by the very next tick -- nothing lost, see
            # DEFAULT_TICK_DEADLINE_SECONDS's own docstring.
            log.info(
                "draft_sender_tick_deadline_reached",
                claimed_this_tick=claimed_count,
                remaining_candidates=len(candidate_ids) - index,
            )
            break
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
    "DEFAULT_TICK_DEADLINE_SECONDS",
    "run_sender_loop",
    "sender_tick",
]
