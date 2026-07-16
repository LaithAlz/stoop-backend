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

Cost metering (#111, schema-v1.md v1.12)
------------------------------------------
The ``'sent'`` audit payload also carries ``segments``/``sms_cost_cents``,
computed by the pure ``app/integrations/sms_segments.py`` helper from the
SAME ``body`` this tick just sent (never from the Twilio response --
Twilio's REST API doesn't return a segment count). ``app/cost_reporting.py``
is the read side: cost-per-case/door/month, each answerable by one query
over ``audit_log``.

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

Supersession belt-and-braces for auto-sent drafts (#60 safety review
MEDIUM-1)
------------------------------------------------------------------------
``app/agent/nodes/draft_response.py``'s own stale-then-insert logic is the
PRIMARY fix: it cancels a still-unsent ``auto_send=true`` draft the moment
a newer tenant message triggers a fresh one, under the per-case advisory
lock. This module carries a SECOND, independent layer for the same
invariant, in case that primary path is ever bypassed: :data:`_CLAIM_
DRAFT_SQL` itself refuses to claim (and therefore send) an ``auto_send =
true`` draft if a newer inbound message has landed on its case since it
was drafted (never a landlord-approved draft — that predicate only ever
looks at ``auto_send = true`` rows); :func:`_claim_draft` then cancels
that refused draft (:data:`_CANCEL_SUPERSEDED_AUTO_SEND_DRAFT_SQL`) and
records a ``send_cancelled`` audit row, so it never sits stuck
``'approved'`` re-appearing as a "due" candidate forever.

The undo window is data, not a sleep (schema-v1.md's own phrase) — this
module never sleeps waiting for a specific draft; it only ever asks
"which approved rows are due right now" and claims exactly those.

Resolved-case guard belt-and-braces (#206)
------------------------------------------------------------------------
``app/routers/cases.py``'s ``POST /v1/cases/{id}/resolve`` is the PRIMARY
fix for "a draft must never send after its case is resolved": it cancels
every still-``'pending'``/``'approved'`` draft on the case, in the SAME
transaction as the case's own ``UPDATE``, the moment a landlord resolves
directly. This module carries a SECOND, independent layer for the same
invariant, in case that primary path is ever bypassed (the exact same
"belt-and-braces" shape as the supersession guard above): :data:`_CLAIM_
DRAFT_SQL` itself also refuses to claim ANY ``'approved'`` draft whose
case has since become ``status = 'resolved'`` — never a landlord-approved
row exception this time; a resolved case's draft is refused regardless of
``auto_send``. Unlike the supersession guard, a draft refused ONLY for
this reason is not actively cancelled here — it is simply never claimed,
falling through :func:`_claim_draft` as a silent no-op (the row stays
``'approved'`` forever, re-appearing as a due candidate on every future
tick but never winning the claim). This is deliberate: the primary fix
(``cases.py``) already cancels the common case immediately, so this guard
almost never fires in practice; it exists purely to make "never sends
after resolve" true even against a code path (present or future) this
module's authors didn't anticipate — e.g. ``app/agent/case_lifecycle.py``'s
own ``sweep_cases()`` tenant-confirmed leg, which (unlike its auto-stale
leg) is NOT excluded from ``awaiting_approval`` and could in principle
auto-resolve a case with a still-approved draft sitting on it. A stuck-
forever-``'approved'``-but-never-sent row is a strictly SAFER failure mode
than a send after resolve, and is a pre-existing, out-of-scope gap in
``sweep_cases()`` itself (not fixed here — only insured against).

Batch-starvation guard (#206 follow-up, safety review MEDIUM-2, the
starvation half only)
------------------------------------------------------------------------
:data:`_SELECT_DUE_DRAFT_IDS_SQL` also excludes any draft whose case is
already ``resolved`` (the SAME :data:`_CASE_RESOLVED_EXISTS_SQL`
correlation the claim guard above uses) — without this, a resolved-case
"zombie" draft (never claimable, per the guard above, but still
``'approved'`` and due) would keep reappearing at the front of every
tick's candidate window (ordered oldest-``scheduled_send_at``-first,
``LIMIT`` :data:`DEFAULT_BATCH_SIZE`) FOREVER, permanently occupying a
batch slot a legitimate due draft could otherwise use — a real
availability concern once :func:`app.agent.case_lifecycle.sweep_cases` is
ever wired to a scheduler (today nothing calls it — see that module's own
"The scheduler seam" — so this starvation is currently only a latent risk,
not yet observable in production). The COMPANION half of that finding —
actually cancelling/paging on a resolved-case zombie instead of merely
excluding it from candidate selection — is DELIBERATELY DEFERRED, a hard
blocker on #212 wiring ``sweep_cases()`` to a real cadence (there is no
value in adding observability for a state this codebase cannot yet
produce end-to-end).

The in-flight claim vs. resolve race — the case-status flip is
self-guarding (#206 follow-up, safety review MEDIUM-1, reproduced
empirically)
------------------------------------------------------------------------
A draft already claimed (``'sending'``) at the instant a landlord resolves
its case is, correctly, left alone by ``app/routers/cases.py``'s resolve
endpoint (see that endpoint's own docstring, "The 'sending' race") — the
send is genuinely mid-flight and this module never holds a transaction
open across the Twilio call, so there is no atomic way to stop it.
Reproduced sequence this fix responds to: landlord approves -> sender
claims (``'sending'``) -> landlord resolves the case (200; the cancel
step correctly finds no ``'pending'``/``'approved'`` row to touch) -> the
send completes. :data:`_MARK_CASE_AWAITING_TENANT_SQL` used to
UNCONDITIONALLY flip ``cases.status`` back to ``'awaiting_tenant'`` at
that point — dragging an explicitly ``'resolved'`` case back out of
resolution while ``resolved_at``/``resolved_reason`` stayed populated (an
inconsistent row, and a silent override of the landlord's own action).
Fixed with a self-guarding ``WHERE ... AND status != 'resolved'`` on that
ONE statement (see its own comment) — every OTHER durable side effect of
the completed send (the outbound ``messages`` row, ``drafts.
sent_message_id``/``status='sent'``, ``trust_metrics``, the ``'sent'``
audit row) still lands unconditionally; the send itself is never rolled
back, only the case-status side effect is guarded.

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
from app.integrations.sms_segments import count_segments, estimate_sms_cost_cents
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

# #206 belt-and-braces — see module docstring "Resolved-case guard
# belt-and-braces (#206)". Applies to EVERY draft (not gated on `auto_send`,
# unlike the newer-inbound guard below): a landlord-approved draft is no
# more safe to send after its case is resolved than an auto-sent one.
# Defined ABOVE `_SELECT_DUE_DRAFT_IDS_SQL` (rather than only above the
# claim SQL, as originally added) so the same text can be reused by the
# candidate SELECT too — see module docstring "Batch-starvation guard
# (#206 follow-up, safety review MEDIUM-2, the starvation half only)".
_CASE_RESOLVED_EXISTS_SQL = (
    "EXISTS (SELECT 1 FROM cases c WHERE c.id = drafts.case_id AND c.status = 'resolved')"
)

# MEDIUM-2 (starvation half): excludes resolved-case "zombies" from the
# candidate window itself, not just the claim -- otherwise a resolved-case
# draft (never claimable, per _CLAIM_DRAFT_SQL's own guard below, but
# still 'approved' and due) would sort to the FRONT of every tick (oldest
# `scheduled_send_at` first) and permanently occupy one of `LIMIT :limit`
# slots, forever, once app/agent/case_lifecycle.py's sweep_cases() is ever
# wired to a real cadence (nothing calls it today -- see that module's own
# "The scheduler seam"). The cancel-or-page companion half of this finding
# is deliberately deferred to #212 (a hard blocker on wiring sweep_cases)
# -- see module docstring for why.
_SELECT_DUE_DRAFT_IDS_SQL = text(
    "SELECT id FROM drafts WHERE status = 'approved' AND scheduled_send_at <= now() "  # noqa: S608
    f"AND NOT {_CASE_RESOLVED_EXISTS_SQL} "
    "ORDER BY scheduled_send_at LIMIT :limit"
)

# #60 safety review MEDIUM-1 (belt-and-braces) — an `auto_send=true` draft
# is a NEVER-human-reviewed row (unlike a landlord-approved one), so the
# claim itself must not dispatch it if a NEWER tenant inbound message has
# arrived for its case since it was drafted. `app/agent/nodes/
# draft_response.py`'s own `_cancel_superseded_auto_send_drafts` is the
# PRIMARY fix (cancels immediately when the newer message triggers a fresh
# draft, under the per-case advisory lock) — this predicate is the SECOND,
# independent layer in case that primary path is ever bypassed. The
# `EXISTS` sub-select correlates "does a newer inbound message belong to
# this draft's case" the SAME way every other cross-table message/case
# correlation in this codebase does (`app/routers/queue.py`'s own LATERAL
# subquery, `app/routers/cases.py`'s `_SELECT_MESSAGES_SQL`):
# `messages.case_id` is always NULL in production (the webhook, the sole
# writer, inserts before case identity is known), so a direct
# `m.case_id = drafts.case_id` match alone would never fire there — the
# `message_cases` join is REQUIRED, not optional, to actually catch this
# in production. `drafts.case_id`/`drafts.created_at` are referenced
# directly (no alias needed — Postgres allows an UPDATE's own WHERE/
# sub-selects to correlate against the target table by name).
_NEWER_INBOUND_EXISTS_SQL = (
    "EXISTS ("
    "  SELECT 1 FROM messages m "
    "  WHERE m.direction = 'inbound' AND m.created_at > drafts.created_at "
    "    AND (m.case_id = drafts.case_id OR EXISTS ("
    "      SELECT 1 FROM message_cases mc "
    "      WHERE mc.message_id = m.id AND mc.case_id = drafts.case_id"
    "    ))"
    ")"
)

_CLAIM_DRAFT_SQL = text(
    "UPDATE drafts SET status = 'sending', updated_at = now() "  # noqa: S608 -- static const interpolated below, no user input
    "WHERE id = :draft_id AND status = 'approved' AND scheduled_send_at <= now() "
    f"AND (auto_send = false OR NOT {_NEWER_INBOUND_EXISTS_SQL}) "
    f"AND NOT {_CASE_RESOLVED_EXISTS_SQL} "
    "RETURNING id, case_id, recipient, body, final_body, edited, landlord_id"
)

# The claim above deliberately refuses a superseded auto_send draft (never
# a landlord-approved one, gated by `auto_send = true` here too) — this is
# the companion write that actually cancels it, so it doesn't sit stuck
# 'approved' forever re-appearing as a "due" candidate on every future
# tick. Same atomic-`UPDATE`-decides-everything shape as the claim itself.
_CANCEL_SUPERSEDED_AUTO_SEND_DRAFT_SQL = text(
    "UPDATE drafts SET status = 'cancelled', updated_at = now() "  # noqa: S608 -- static const interpolated below, no user input
    "WHERE id = :draft_id AND status = 'approved' AND auto_send = true "
    f"AND {_NEWER_INBOUND_EXISTS_SQL} "
    "RETURNING id, case_id, landlord_id"
)

_INSERT_AUTO_SEND_SUPERSEDED_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, :case_id, 'agent', 'send_cancelled', CAST(:payload AS jsonb))"
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

# Self-guarding (safety review MEDIUM-1, reproduced empirically, #206
# follow-up): a case a landlord resolved WHILE this exact draft was already
# claimed ('sending') must not be dragged back out of 'resolved' once the
# send completes -- `app/routers/cases.py`'s resolve endpoint correctly
# leaves a 'sending' row alone (it's genuinely mid-flight), but this
# UPDATE used to unconditionally flip `cases.status` regardless of what
# happened to the case in the meantime, silently overriding the
# landlord's explicit resolve action while `resolved_at`/`resolved_reason`
# stayed populated -- an inconsistent, wrong end state. `status !=
# 'resolved'` re-asserts, at UPDATE time, that this case has not since
# been resolved; a lost guard here is a deliberate, silent no-op (matching
# every other self-guarding UPDATE in this codebase) -- the send itself
# already happened and is NOT rolled back: the outbound `messages` row,
# `drafts.sent_message_id`/`status='sent'`, `trust_metrics`, and the
# `'sent'` audit row below all still land unconditionally. Only this ONE
# case-status side effect is guarded.
_MARK_CASE_AWAITING_TENANT_SQL = text(
    "UPDATE cases SET status = 'awaiting_tenant', last_activity_at = now(), updated_at = now() "
    "WHERE id = :case_id AND status != 'resolved'"
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
    """Claim *draft_id* for sending, or ``None`` if it can't be claimed
    right now — FOUR distinct reasons collapse into that same ``None``:
    lost the claim race (another tick/process already claimed it), not
    actually due yet, a superseded ``auto_send=true`` draft the claim's own
    guard refused (#60 safety review MEDIUM-1), or (#206) its case has
    since become ``resolved`` (see module docstring "Resolved-case guard
    belt-and-braces (#206)"). Only the THIRD case does anything further
    here: :data:`_CANCEL_SUPERSEDED_AUTO_SEND_DRAFT_SQL` cancels it (never
    a landlord-approved row — see that query's own docstring) and records
    a ``send_cancelled`` audit row, so it never sits stuck ``'approved'``
    reappearing as "due" forever. The FOURTH case (resolved-case refusal)
    is NOT actively cancelled here on purpose — the primary fix
    (``app/routers/cases.py``'s resolve endpoint) already cancels the
    common case immediately; this guard is a pure safety net, so a draft
    caught ONLY by it is left ``'approved'`` (never sent, never
    cancelled) — see that section of the module docstring for why this is
    an acceptable, strictly-safer-than-sending failure mode. The first two
    cases fall through as a silent, correct no-op exactly like before this
    fix.
    """
    row = (
        (await session.execute(_CLAIM_DRAFT_SQL, {"draft_id": str(draft_id)}))
        .mappings()
        .one_or_none()
    )
    if row is not None:
        return dict(row)

    cancelled_row = (
        (await session.execute(_CANCEL_SUPERSEDED_AUTO_SEND_DRAFT_SQL, {"draft_id": str(draft_id)}))
        .mappings()
        .one_or_none()
    )
    if cancelled_row is not None:
        await session.execute(
            _INSERT_AUTO_SEND_SUPERSEDED_AUDIT_SQL,
            {
                "landlord_id": str(cancelled_row["landlord_id"]),
                "case_id": str(cancelled_row["case_id"]),
                "payload": json.dumps(
                    {"draft_id": str(draft_id), "reason": "superseded_by_newer_message"}
                ),
            },
        )
        log.info(
            "draft_sender_auto_send_cancelled_superseded",
            draft_id=str(draft_id),
            case_id=str(cancelled_row["case_id"]),
        )
    return None


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

    # #111 cost metering -- computed from the SAME body just sent, never
    # from the Twilio response (Twilio's API doesn't return segment count).
    # Pure/no-I/O (app/integrations/sms_segments.py) -- never affects
    # whether/what was sent, only what gets recorded about it afterward.
    # Guarded (safety review, #111): the SMS is already irreversibly out at
    # this point, so a metering failure must NEVER cost the send record --
    # the module's own never-raises invariant outranks a cost annotation.
    segments: int | None
    sms_cost_cents: float | None
    try:
        segment_info = count_segments(body)
        segments = segment_info.segments
        sms_cost_cents = estimate_sms_cost_cents(segment_info.segments)
    except Exception:
        log.error("draft_sender_segment_metering_failed", draft_id=str(draft_id))
        segments = None
        sms_cost_cents = None

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
                    {
                        "draft_id": str(draft_id),
                        "message_id": str(message_id),
                        "edited": edited,
                        # #111 cost metering (schema-v1.md v1.12): segment
                        # count + estimated Twilio cost for THIS send.
                        "segments": segments,
                        "sms_cost_cents": sms_cost_cents,
                    }
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
