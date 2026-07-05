"""Case lifecycle — pure state-machine functions + the `sweep_cases()`
DB entrypoint, per `docs/02-product/conversation-model.md` (#110).

Everything above the "DB entrypoint" section is a PURE function: no I/O, no
DB, no clock reads other than the ``now`` parameter the caller supplies —
trivially unit-testable, mirroring ``app/agent/prefilter.py``'s "pure
functions only" design. ``app/agent/nodes/identify_case.py`` is the only
caller that turns these decisions into actual writes.

The lifecycle (verbatim shape from conversation-model.md)
-----------------------------------------------------------
::

    OPEN -> AWAITING_APPROVAL -> AWAITING_TENANT -> RESOLVED -> REOPENED
      |            |                    |               ^
      |            +-- approve/send ----+               |
      +-- landlord resolves directly ----------------------+
                                    auto-stale (14d) --> RESOLVED(auto_stale)

Status/resolved_reason values below are the LOWERCASE strings
schema-v1.md's CHECK constraints define on ``cases.status`` /
``cases.resolved_reason`` — never invented, never re-cased.

The routing seam (deliberately NOT an LLM call in this issue)
-----------------------------------------------------------------
Per the issue's scope: these are DETERMINISTIC nodes, zero LLM calls.
conversation-model.md's message-routing step ("the model assigns the
message to existing case(s), new case(s), or chitchat") is inherently a
content-understanding problem — detecting "this continues the heat case" or
"tenant says the leak is fixed now" from raw text requires exactly the kind
of semantic judgment #31/#32 (``classify_intent`` and, per this issue's own
tool already defined in ``app/agent/tools.py``, ``identify_case``'s
LLM-assisted form) will eventually supply.

``route_inbound_message`` accepts an OPTIONAL ``signals`` parameter shaped
for that future: a list of :class:`RoutingSignal`, one per distinct issue a
future node found in the message, each carrying ``is_new_issue``,
``matched_case_id`` (which MAY reference a currently-open OR a
resolved case — see the reopen-window handling below),
``is_chitchat``, and ``tenant_confirms_resolved``. When ``signals`` is
``None`` (today's reality — no such node exists yet), this module falls
back to conversation-model.md's own explicitly-provided deterministic
default: the ambiguity rule. Zero open cases -> new case. Exactly one open
case -> attach (unambiguous). More than one -> attach to the most recently
active case AND note the ambiguity in ``reasoning_log`` so the landlord can
re-route with one tap (conversation-model.md, "Message routing"). Chitchat
and tenant-confirmed-resolution detection are NOT attempted deterministically
today (there is no reliable regex for "thanks, all good now" that wouldn't
risk silently closing a case that in fact still needs attention) — they are
only ever produced via the ``signals`` seam.

The tenant-confirmed "propose resolution, auto-apply in 48h unless
contradicted" phase — now schema-backed (v1.5, migration 0008)
-----------------------------------------------------------------------------
conversation-model.md: "tenant confirms fixed (agent detects '...' and
*proposes* resolution -- landlord-visible, auto-applies after 48h if not
contradicted)". This requires a DURABLE marker of "a resolution was
proposed, not yet applied, applying at time T" that survives process
restarts. An earlier revision of this module flagged this as a genuine
schema-vocabulary gap (neither ``cases.status`` nor ``audit_log.action`` had
a place to put it) and stopped short of inventing one. The founder's
decision (doc-first, per the repo's own "the undo window is data, not a
sleep" precedent — see ``drafts.scheduled_send_at``): a new nullable column,
``cases.pending_resolved_at timestamptz`` (schema-v1.md's v1.5 amendments,
migration 0008). ``NULL`` means no proposal pending (the common case).

**Design choice — this column stores the APPLY-AT time, not the proposal
time.** :func:`propose_resolution` sets ``pending_resolved_at = now() +
RESOLUTION_PROPOSAL_WINDOW`` directly, rather than storing the proposal
instant and re-deriving the deadline everywhere it's read. See schema-v1.md's
v1.5 amendments block for the full rationale (self-describing column name,
a trivial ``pending_resolved_at <= now()`` sweep predicate with no
arithmetic to duplicate, and a future window change needs no migration).
:class:`CaseSnapshot`'s field of the same name mirrors this exactly.

**Audit vocabulary is UNCHANGED on purpose.** The proposal itself, and any
contradiction, are visible via the column plus ``reasoning_log`` only — no
``audit_log`` entry is written for either (there is still no "proposed"/
"contradicted" action, and none is needed: an entry is written only when a
resolution actually APPLIES, exactly like every other resolution path).

**Precedence over the 14-day auto-stale sweep.** A case with
``pending_resolved_at IS NOT NULL`` is NEVER auto-staled, no matter how old
its ``last_activity_at`` is — see :func:`apply_time_transitions`. The
pending, tenant-confirmed signal is more specific and more recent than mere
inactivity; auto-staling out from under it would silently discard it. Such a
case resolves via exactly one of: the 48h deadline arrives (sweep, below),
or a new message on the case contradicts it first (handled by
``identify_case`` calling :func:`contradict_resolution`, which clears the
column back to ``NULL`` — the case simply stays in its current open-family
status, unaffected otherwise).

The scheduler seam
-------------------
Nothing calls ``sweep_cases()`` today -- no cron/scheduler infrastructure
exists yet, exactly like ``app/agent/emergency.py``'s escalation-chain seam.
A future scheduled job (Fly machines cron, or the durable queue
architecture.md §11 introduces on trigger) should invoke it on a regular
cadence (``emergency-prefilter.md`` suggests a 60s cadence for the
escalation chain; auto-stale/auto-apply have day/hour-scale windows, so a
much coarser cadence -- e.g. hourly -- is sufficient here and cheap).
``sweep_cases()`` performs BOTH legs against the real database now: the 48h
tenant-confirmed auto-apply (schema-backed since migration 0008) and the
14-day auto-stale sweep.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

import structlog
from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_admin_session

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Schema-verbatim vocabulary (schema-v1.md CHECK constraints — never invent)
# ---------------------------------------------------------------------------

STATUS_OPEN = "open"
STATUS_AWAITING_APPROVAL = "awaiting_approval"
STATUS_AWAITING_TENANT = "awaiting_tenant"
STATUS_RESOLVED = "resolved"
STATUS_REOPENED = "reopened"

OPEN_STATUSES: frozenset[str] = frozenset(
    {STATUS_OPEN, STATUS_AWAITING_APPROVAL, STATUS_AWAITING_TENANT, STATUS_REOPENED}
)
"""Every status conversation-model.md treats as "still active work" —
matches ``OpenCaseSummary``'s docstring. This is the candidate set for BOTH
sweep legs at the query level and for the tenant-confirmed 48h leg's
eligibility; the auto-stale leg narrows further, see
``AUTO_STALE_ELIGIBLE_STATUSES``."""

AUTO_STALE_ELIGIBLE_STATUSES: frozenset[str] = frozenset(
    {STATUS_OPEN, STATUS_AWAITING_TENANT, STATUS_REOPENED}
)
"""``OPEN_STATUSES`` MINUS ``awaiting_approval`` (conversation-model.md
amendment, #110 review): a case sitting in ``awaiting_approval`` has a
DRAFT the landlord has not yet acted on — auto-staling it out from under
them would silently erase their own backlog, not just an inactive
conversation. ``awaiting_approval`` cases are simply never swept for
staleness; they wait for the landlord (or a new message) indefinitely."""

RESOLVED_REASON_LANDLORD = "landlord"
RESOLVED_REASON_TENANT_CONFIRMED = "tenant_confirmed"
RESOLVED_REASON_AUTO_STALE = "auto_stale"

AUDIT_ACTION_CASE_OPENED = "case_opened"
AUDIT_ACTION_CASE_REOPENED = "case_reopened"
AUDIT_ACTION_CASE_RESOLVED = "case_resolved"

# ---------------------------------------------------------------------------
# Timing constants (conversation-model.md's founder-approved defaults)
# ---------------------------------------------------------------------------

REOPEN_WINDOW = timedelta(days=30)
"""Boundary convention (a judgment call — the doc text is ambiguous exactly
AT 30 days): a case resolved EXACTLY 30 days ago still reopens (inclusive);
30 days + any amount reopens as a NEW case with ``related_case_id`` instead.
Tested explicitly at 29d/30d/31d (see tests/test_case_lifecycle.py)."""

AUTO_STALE_INACTIVITY = timedelta(days=14)
"""Auto-stale after 14 days of inactivity (``last_activity_at``) — same
inclusive-boundary convention as ``REOPEN_WINDOW``: exactly 14 days IS
stale."""

RESOLUTION_PROPOSAL_WINDOW = timedelta(hours=48)
"""Tenant-confirmed resolution auto-applies 48h after being proposed,
unless contradicted first. ``propose_resolution`` bakes this window in at
proposal time (``pending_resolved_at = now() + RESOLUTION_PROPOSAL_WINDOW``)
— see module docstring for why (schema-v1.md's v1.5 amendments, migration
0008). Changing this constant changes the window for every FUTURE proposal
with no migration required."""


# ---------------------------------------------------------------------------
# Pure data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseSnapshot:
    """A read-only, DB-row-shaped view of the lifecycle-relevant columns of
    one ``cases`` row — decoupled from any ORM/DB row type so the functions
    below stay pure and testable without a database.

    ``pending_resolved_at`` mirrors ``cases.pending_resolved_at`` (schema-
    v1.md v1.5, migration 0008) EXACTLY: the APPLY-AT time (proposal instant
    + 48h), not the proposal instant itself — see module docstring "Design
    choice". ``None`` means no resolution is currently proposed for this
    case (the common case).
    """

    case_id: UUID
    status: str
    resolved_reason: str | None
    resolved_at: datetime | None
    last_activity_at: datetime
    pending_resolved_at: datetime | None = None


@dataclass(frozen=True)
class ReopenDecision:
    """Result of :func:`decide_reopen_or_new` — whether a message matching
    an already-``resolved`` case reopens that SAME case (same id, same
    audit trail — conversation-model.md's "LTB-friendly" invariant) or
    starts a brand-new case linked via ``related_case_id``."""

    reopen: bool
    reasoning_log: str


@dataclass(frozen=True)
class Transition:
    """A generic lifecycle transition: the new column values to persist,
    the single ``audit_log.action`` (existing vocabulary only) it maps to,
    and one human-readable ``reasoning_log`` line."""

    new_status: str
    resolved_reason: str | None
    resolved_at: datetime | None
    audit_action: str
    reasoning_log: str


@dataclass(frozen=True)
class ProposeResolutionResult:
    """Result of :func:`propose_resolution`. ``identify_case`` persists
    ``pending_resolved_at`` verbatim onto the matched ``cases`` row (schema-
    v1.md v1.5, migration 0008) — no ``audit_log`` entry (see module
    docstring "Audit vocabulary is UNCHANGED on purpose")."""

    pending_resolved_at: datetime
    reasoning_log: str


@dataclass(frozen=True)
class ContradictResolutionResult:
    cleared: bool
    reasoning_log: str


@dataclass(frozen=True)
class SweepAction:
    """One case's outcome from :func:`apply_time_transitions`."""

    case_id: UUID
    transition: Transition


@dataclass(frozen=True)
class OpenCase:
    """Pure input shape for :func:`route_inbound_message` — the minimal
    projection of one of the tenant's open cases routing needs. Decoupled
    from ``app.agent.schemas.OpenCaseSummary`` (the Pydantic/state-facing
    model) so this module has zero Pydantic/DB dependency."""

    case_id: UUID
    last_activity_at: datetime


@dataclass(frozen=True)
class RoutingSignal:
    """Optional per-issue signal a future LLM-informed node (#31/#32)
    populates before ``identify_case`` runs — see module docstring "The
    routing seam". Never produced by this codebase today."""

    is_new_issue: bool
    matched_case_id: UUID | None = None
    is_chitchat: bool = False
    tenant_confirms_resolved: bool = False
    summary: str | None = None


@dataclass(frozen=True)
class RoutingResult:
    """One decision per distinct issue found in the inbound message.

    ``action`` is one of: ``"new_case"``, ``"attach_existing"``,
    ``"chitchat"``. ``target_case_id`` is set only for ``"attach_existing"``
    (the existing case — open OR resolved; the caller resolves reopen-vs-
    new via :func:`decide_reopen_or_new` when it turns out to be resolved).
    """

    action: str
    target_case_id: UUID | None
    reasoning_log: list[str] = field(default_factory=list)
    tenant_confirms_resolved: bool = False


# ---------------------------------------------------------------------------
# Routing (message -> case(s))
# ---------------------------------------------------------------------------


def route_inbound_message(
    *,
    open_cases: list[OpenCase],
    signals: list[RoutingSignal] | None = None,
    tenant_label: str | None = None,
) -> list[RoutingResult]:
    """Decide which case(s) an inbound message belongs to.

    When ``signals`` is provided (non-empty — the future LLM-informed
    path), returns one :class:`RoutingResult` per signal, in order — this is
    how a multi-issue message ("also the bathroom fan...") splits into
    per-issue case actions (conversation-model.md, "Multi-issue").

    When ``signals`` is ``None`` or empty (today's deterministic default),
    applies conversation-model.md's ambiguity rule using ONLY
    ``open_cases``, and always returns exactly one :class:`RoutingResult`
    (today's deterministic node never splits a message into multiple
    cases — see module docstring "The routing seam"):

    - No open cases -> ``"new_case"``.
    - Exactly one open case -> ``"attach_existing"`` (unambiguous).
    - More than one open case -> ``"attach_existing"`` to the MOST
      RECENTLY ACTIVE case, with an ambiguity note in ``reasoning_log``.

    ``tenant_label`` is an OPTIONAL, plain display name (e.g. a tenant's
    first name) used only to make ``reasoning_log`` read naturally
    ("Maria's open conversation" vs. "the open conversation") — passing it
    keeps this function pure (no I/O, still just a string in/string out);
    the caller (``identify_case``) is the one that actually looked it up.
    Every ``reasoning_log`` line here is warm, plain-English, landlord-
    facing copy (CLAUDE.md rule #8 / the approval card) — no ids, no
    ``node_name:`` prefixes, no field=value reprs. Raw identifiers belong
    in the caller's structlog calls instead.
    """
    if signals:
        return [_route_signal(signal) for signal in signals]

    if not open_cases:
        return [
            RoutingResult(
                action="new_case",
                target_case_id=None,
                reasoning_log=[
                    "This looks like a new issue, so I started a new conversation for it."
                ],
            )
        ]

    if len(open_cases) == 1:
        only = open_cases[0]
        whose = f"{tenant_label}'s" if tenant_label else "the"
        return [
            RoutingResult(
                action="attach_existing",
                target_case_id=only.case_id,
                reasoning_log=[
                    f"This looks like it continues {whose} open conversation, so I added it there."
                ],
            )
        ]

    most_recent = max(open_cases, key=lambda c: c.last_activity_at)
    who = f"{tenant_label} has" if tenant_label else "There are"
    return [
        RoutingResult(
            action="attach_existing",
            target_case_id=most_recent.case_id,
            reasoning_log=[
                f"{who} more than one open conversation right now, and I wasn't sure which one "
                "this belongs to, so I added it to the most recently active one. Let me know if "
                "I should move it."
            ],
        )
    ]


def _route_signal(signal: RoutingSignal) -> RoutingResult:
    if signal.is_chitchat:
        return RoutingResult(
            action="chitchat",
            target_case_id=None,
            reasoning_log=[
                "This looks like a quick reply rather than a new issue, so I didn't open a "
                "conversation for it."
            ],
        )

    if signal.matched_case_id is not None:
        if signal.summary:
            note = (
                f"This looks like it continues an existing conversation ({signal.summary}), "
                "so I added it there."
            )
        else:
            note = "This looks like it continues an existing conversation, so I added it there."
        return RoutingResult(
            action="attach_existing",
            target_case_id=signal.matched_case_id,
            reasoning_log=[note],
            tenant_confirms_resolved=signal.tenant_confirms_resolved,
        )

    if signal.summary:
        note = f"This looks like a new issue — {signal.summary}."
    else:
        note = "This looks like a new issue, so I started a new conversation for it."
    return RoutingResult(action="new_case", target_case_id=None, reasoning_log=[note])


# ---------------------------------------------------------------------------
# Reopen window
# ---------------------------------------------------------------------------


def decide_reopen_or_new(case: CaseSnapshot, now: datetime) -> ReopenDecision:
    """Reopen-window decision for a message matching an already-``resolved``
    case (conversation-model.md, "Reopen").

    Precondition: ``case.status == STATUS_RESOLVED``. Callers must not
    invoke this for a case in any other status (there is nothing to
    "reopen" otherwise) — asserted defensively below rather than silently
    misbehaving.
    """
    if case.status != STATUS_RESOLVED:
        raise ValueError(
            f"decide_reopen_or_new called on a non-resolved case (status={case.status!r}); "
            "only a resolved case can be reopened or linked."
        )
    if case.resolved_at is None:
        raise ValueError("a resolved case must have resolved_at set")

    age = now - case.resolved_at
    if age <= REOPEN_WINDOW:
        return ReopenDecision(
            reopen=True,
            reasoning_log=(
                "The tenant brought this up again within a month of it being marked resolved, "
                "so I reopened the same conversation instead of starting a new one."
            ),
        )
    return ReopenDecision(
        reopen=False,
        reasoning_log=(
            "It's been over a month since this was marked resolved, so I started a new "
            "conversation for it and linked it to the earlier one."
        ),
    )


# ---------------------------------------------------------------------------
# Resolution transitions
# ---------------------------------------------------------------------------


def resolve_by_landlord(now: datetime) -> Transition:
    """The landlord resolves a case directly (conversation-model.md's
    "landlord resolves directly" arrow — no proposal/auto-apply window)."""
    return Transition(
        new_status=STATUS_RESOLVED,
        resolved_reason=RESOLVED_REASON_LANDLORD,
        resolved_at=now,
        audit_action=AUDIT_ACTION_CASE_RESOLVED,
        reasoning_log="You marked this resolved.",
    )


def propose_resolution(now: datetime) -> ProposeResolutionResult:
    """Tenant confirms the issue is fixed — propose (not yet apply) a
    resolution. Bakes the 48h window in NOW (see module docstring "Design
    choice"): the returned ``pending_resolved_at`` is the APPLY-AT time
    (``now + RESOLUTION_PROPOSAL_WINDOW``), not the proposal instant."""
    deadline = now + RESOLUTION_PROPOSAL_WINDOW
    hours = int(RESOLUTION_PROPOSAL_WINDOW.total_seconds() // 3600)
    return ProposeResolutionResult(
        pending_resolved_at=deadline,
        reasoning_log=(
            f"The tenant said this is fixed, so I'll mark it resolved automatically in "
            f"{hours} hours unless something changes before then."
        ),
    )


def contradict_resolution(case: CaseSnapshot) -> ContradictResolutionResult:
    """A new message arrives on a case with a pending proposed resolution —
    cancel the proposal (deterministic, content-independent: ANY new
    message while a proposal is pending is treated as a contradiction,
    never risking a wrongful auto-close; see module docstring "The routing
    seam" for why detecting agreement-vs-disagreement is not attempted
    deterministically). The caller (``identify_case``) is responsible for
    actually clearing ``cases.pending_resolved_at`` back to ``NULL`` when
    ``cleared`` is True — this function only decides whether to."""
    if case.pending_resolved_at is None:
        return ContradictResolutionResult(
            cleared=False, reasoning_log="There was nothing pending to cancel."
        )
    return ContradictResolutionResult(
        cleared=True,
        reasoning_log=(
            "A new message came in before the resolution would have applied automatically, "
            "so I've held off — this is still open."
        ),
    )


# ---------------------------------------------------------------------------
# Time-driven sweep (pure)
# ---------------------------------------------------------------------------


def apply_time_transitions(cases: list[CaseSnapshot], now: datetime) -> list[SweepAction]:
    """Scan ``cases`` and return every time-driven transition that should
    fire at ``now``: the 48h tenant-confirmed auto-apply, and the 14-day
    auto-stale sweep. Only cases already in an "open family" status are
    eligible; a case already ``resolved`` (however it got there) is left
    alone.

    PRECEDENCE (module docstring, "Precedence over the 14-day auto-stale
    sweep"): a case with ``pending_resolved_at IS NOT NULL`` is NEVER
    auto-staled, regardless of ``last_activity_at`` age — it either
    auto-applies (deadline reached) or is left untouched (deadline not yet
    reached; it never "falls through" to the auto-stale check below). Only
    a case with NO pending resolution at all is considered for auto-stale.

    AUTO-STALE SCOPE: ``awaiting_approval`` cases are EXCLUDED from the
    auto-stale leg entirely (see ``AUTO_STALE_ELIGIBLE_STATUSES``) — a
    landlord's own unactioned draft must never self-resolve, that would
    silently hide their backlog rather than surface it.

    NOTE — this function is the DECISION phase only; the actual write
    (``sweep_cases``, below) re-checks the SAME conditions against the
    live row via a self-guarding ``UPDATE ... WHERE`` before trusting a
    ``SweepAction`` computed here, because the snapshot passed in can go
    stale between when it was read and when the write happens (a
    concurrent contradiction or new message) — see ``sweep_cases``'s
    docstring "TOCTOU".
    """
    actions: list[SweepAction] = []
    for case in cases:
        if case.status not in OPEN_STATUSES:
            continue

        if case.pending_resolved_at is not None:
            if now >= case.pending_resolved_at:
                hours = int(RESOLUTION_PROPOSAL_WINDOW.total_seconds() // 3600)
                actions.append(
                    SweepAction(
                        case_id=case.case_id,
                        transition=Transition(
                            new_status=STATUS_RESOLVED,
                            resolved_reason=RESOLVED_REASON_TENANT_CONFIRMED,
                            resolved_at=now,
                            audit_action=AUDIT_ACTION_CASE_RESOLVED,
                            reasoning_log=(
                                f"It's been {hours} hours since the tenant said this was fixed, "
                                "with no further messages, so I'm marking it resolved."
                            ),
                        ),
                    )
                )
            # Deadline not yet reached, or just applied above: either way,
            # this case never falls through to the auto-stale check.
            continue

        if case.status not in AUTO_STALE_ELIGIBLE_STATUSES:
            # awaiting_approval: the landlord's own backlog, never auto-resolved.
            continue

        if now - case.last_activity_at >= AUTO_STALE_INACTIVITY:
            weeks = AUTO_STALE_INACTIVITY.days // 7
            actions.append(
                SweepAction(
                    case_id=case.case_id,
                    transition=Transition(
                        new_status=STATUS_RESOLVED,
                        resolved_reason=RESOLVED_REASON_AUTO_STALE,
                        resolved_at=now,
                        audit_action=AUDIT_ACTION_CASE_RESOLVED,
                        reasoning_log=(
                            f"There's been no activity here for {weeks} weeks, so I'm marking it "
                            "resolved as stale. Let me know if it still needs attention."
                        ),
                    ),
                )
            )

    return actions


# ---------------------------------------------------------------------------
# DB entrypoint — sweep_cases()
#
# TOCTOU (#110 safety review, BLOCKING, proven live: "tenant said it is
# back, but case = resolved/tenant_confirmed"). The SELECT below and the
# decision phase (apply_time_transitions) both work off a SNAPSHOT that can
# go stale before the write happens — a concurrent contradiction (clears
# pending_resolved_at) or a fresh inbound message (bumps last_activity_at)
# landing in that window must not be silently overwritten by an UPDATE that
# only guards on `id`. Fixed with a SELF-GUARDING UPDATE per leg: the WHERE
# clause re-asserts, against the row's CURRENT state (not the stale
# snapshot), the SAME condition apply_time_transitions used to decide this
# case was eligible. Every side effect (the audit entry, the auto-stale
# notification) is gated on that UPDATE having matched EXACTLY one row
# (``rowcount == 1``) — a lost race (rowcount == 0) is a deliberate, silent
# no-op: no audit trail is invented for a transition that didn't actually
# happen, and the case is left exactly as the concurrent write left it.
# ---------------------------------------------------------------------------

# Built from the fixed, internal ``OPEN_STATUSES``/``AUTO_STALE_ELIGIBLE_
# STATUSES`` constants (never external/request-supplied data) — inline
# literal IN-lists rather than bound array parameters, sidestepping
# asyncpg's need for an explicit `::text[]` cast on `ANY(:param)` with a
# plain `text()` query.
_OPEN_STATUS_LITERAL_LIST = ", ".join(f"'{status}'" for status in sorted(OPEN_STATUSES))
_AUTO_STALE_STATUS_LITERAL_LIST = ", ".join(
    f"'{status}'" for status in sorted(AUTO_STALE_ELIGIBLE_STATUSES)
)

_SELECT_SWEEP_CANDIDATES_SQL = text(
    "SELECT id, status, resolved_reason, resolved_at, last_activity_at, "  # noqa: S608
    "pending_resolved_at "
    f"FROM cases WHERE status IN ({_OPEN_STATUS_LITERAL_LIST})"
    # ^ IN-list built from the internal OPEN_STATUSES constant only, never
    #   external/request-supplied data — not a real injection vector.
)

# tenant_confirmed leg — self-guarding: re-asserts a resolution is STILL
# pending and STILL due at UPDATE time. A concurrent contradiction (which
# sets pending_resolved_at back to NULL) makes this WHERE match zero rows.
_UPDATE_TENANT_CONFIRMED_SQL = text(
    "UPDATE cases SET status = :status, resolved_reason = :resolved_reason, "
    "resolved_at = :resolved_at, pending_resolved_at = NULL, "
    "last_activity_at = :now, updated_at = :now "
    "WHERE id = :case_id AND pending_resolved_at IS NOT NULL AND pending_resolved_at <= :now"
)

# auto_stale leg — self-guarding: re-asserts NO pending resolution, an
# eligible status (never awaiting_approval — see AUTO_STALE_ELIGIBLE_
# STATUSES), and last_activity_at still at/before the threshold, all at
# UPDATE time. A concurrent new message (which bumps last_activity_at, or
# moves status out of the eligible set) makes this WHERE match zero rows.
_UPDATE_AUTO_STALE_SQL = text(
    "UPDATE cases SET status = :status, resolved_reason = :resolved_reason, "  # noqa: S608
    "resolved_at = :resolved_at, pending_resolved_at = NULL, "
    "last_activity_at = :now, updated_at = :now "
    "WHERE id = :case_id AND pending_resolved_at IS NULL "
    f"AND status IN ({_AUTO_STALE_STATUS_LITERAL_LIST}) AND last_activity_at <= :threshold"
)

_INSERT_SWEEP_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "SELECT landlord_id, :case_id, 'system', :action, CAST(:payload AS jsonb) "
    "FROM cases WHERE id = :case_id"
)

# Auto-stale visibility (#110 review, advisory): closing a case with zero
# landlord-visible signal is a silent close. ``case_id`` (not ``message_id``)
# in the payload is DELIBERATE: migration 0006's partial unique index keys
# off ``payload ->> 'message_id'``, which extracts SQL NULL for a row like
# this one — ordinary NULL semantics mean it never collides with anything,
# so the index is not what prevents a duplicate here. Exactly-once for THIS
# notification instead comes from the guarded UPDATE above: rowcount == 1
# can only ever happen ONCE per case, because a second sweep's WHERE no
# longer matches (the row is already ``status = 'resolved'``).
_INSERT_AUTO_STALE_NOTIFICATION_SQL = text(
    "INSERT INTO notifications (landlord_id, case_id, type, channel, status, payload) "
    "SELECT landlord_id, :case_id, 'needs_eyes', 'push', 'pending', CAST(:payload AS jsonb) "
    "FROM cases WHERE id = :case_id"
)


async def _apply_sweep_action(
    session: AsyncSession,
    action: SweepAction,
    *,
    effective_now: datetime,
    stale_threshold: datetime,
) -> bool:
    """Apply ONE sweep action via its leg's self-guarding UPDATE, gating the
    audit entry (and, for auto_stale, the notification) on the UPDATE having
    matched EXACTLY one row. Returns ``True`` if it actually applied,
    ``False`` if the guard lost the race — see ``sweep_cases``'s own
    docstring and this section's header comment for the TOCTOU rationale.

    Factored out of ``sweep_cases`` specifically so the race it closes can
    be exercised directly in a test: seed a due case, compute a now-STALE
    action via :func:`apply_time_transitions`, mutate the row out from
    under it (simulating a concurrent contradiction or new message), then
    call this function with the stale action and assert it safely no-ops
    (``False``, case unchanged, no audit row) — see
    ``tests/test_case_lifecycle.py``'s TOCTOU regression test.
    """
    is_tenant_confirmed = action.transition.resolved_reason == RESOLVED_REASON_TENANT_CONFIRMED
    update_sql = _UPDATE_TENANT_CONFIRMED_SQL if is_tenant_confirmed else _UPDATE_AUTO_STALE_SQL
    params: dict[str, object] = {
        "status": action.transition.new_status,
        "resolved_reason": action.transition.resolved_reason,
        "resolved_at": action.transition.resolved_at,
        "now": effective_now,
        "case_id": str(action.case_id),
    }
    if not is_tenant_confirmed:
        params["threshold"] = stale_threshold

    result = cast("CursorResult[object]", await session.execute(update_sql, params))
    if result.rowcount != 1:
        # Lost the race (#110 review, BLOCKING): a concurrent contradiction
        # or new message changed this row between the SELECT that produced
        # `action` and this UPDATE. Deliberate silent no-op — see this
        # function's own docstring and the section header comment above.
        log.info(
            "case_lifecycle_sweep_guard_miss",
            case_id=str(action.case_id),
            leg="tenant_confirmed" if is_tenant_confirmed else "auto_stale",
        )
        return False

    await session.execute(
        _INSERT_SWEEP_AUDIT_SQL,
        {
            "case_id": str(action.case_id),
            "action": action.transition.audit_action,
            "payload": json.dumps({"reason": action.transition.resolved_reason}),
        },
    )

    if not is_tenant_confirmed:
        await session.execute(
            _INSERT_AUTO_STALE_NOTIFICATION_SQL,
            {
                "case_id": str(action.case_id),
                "payload": json.dumps({"case_id": str(action.case_id), "reason": "auto_stale"}),
            },
        )

    return True


async def sweep_cases(*, now: datetime | None = None) -> list[SweepAction]:
    """DB entrypoint for the time-driven sweep. See module docstring "The
    scheduler seam" — nothing calls this today; a future cron/scheduled job
    should. Performs BOTH legs against real data (schema-v1.md v1.5,
    migration 0008 closed the gap that previously limited this to auto-stale
    only): the 48h tenant-confirmed auto-apply, and the 14-day auto-stale
    sweep — with pending-resolution precedence (module docstring) enforced
    by :func:`apply_time_transitions`, and per-leg TOCTOU safety enforced by
    :func:`_apply_sweep_action`'s self-guarding ``UPDATE ... WHERE``
    statements (see this section's own header comment).

    An auto-stale resolution ALSO creates a ``needs_eyes`` notification —
    closing a case silently, with nothing surfaced to the landlord, is
    exactly the failure mode this exists to prevent. The tenant-confirmed
    leg does NOT create one: the landlord already saw the proposal
    (``reasoning_log``) when it was made.

    Returns only the actions that ACTUALLY applied (guard matched,
    ``rowcount == 1``) — an action whose guard lost the race is silently
    dropped from the return value, exactly as it was silently dropped from
    the database write.

    Runs on the admin engine (background/scheduled job context, no request/
    landlord JWT to scope an RLS session by — same rationale as
    ``app/agent/graph_entry.py``). Allowlisted in
    ``tests/test_migrations_0005.py::_ADMIN_SESSION_ALLOWLIST``.
    """
    effective_now = now or datetime.now(UTC)
    stale_threshold = effective_now - AUTO_STALE_INACTIVITY

    applied: list[SweepAction] = []

    async with asynccontextmanager(get_admin_session)() as session:
        rows = (await session.execute(_SELECT_SWEEP_CANDIDATES_SQL)).mappings().all()
        snapshots = [
            CaseSnapshot(
                case_id=row["id"],
                status=row["status"],
                resolved_reason=row["resolved_reason"],
                resolved_at=row["resolved_at"],
                last_activity_at=row["last_activity_at"],
                pending_resolved_at=row["pending_resolved_at"],
            )
            for row in rows
        ]

        actions = apply_time_transitions(snapshots, effective_now)

        for action in actions:
            did_apply = await _apply_sweep_action(
                session, action, effective_now=effective_now, stale_threshold=stale_threshold
            )
            if did_apply:
                applied.append(action)

    log.info("case_lifecycle_sweep_complete", cases_transitioned=len(applied))
    return applied


__all__: list[str] = [
    "AUDIT_ACTION_CASE_OPENED",
    "AUDIT_ACTION_CASE_REOPENED",
    "AUDIT_ACTION_CASE_RESOLVED",
    "AUTO_STALE_ELIGIBLE_STATUSES",
    "AUTO_STALE_INACTIVITY",
    "OPEN_STATUSES",
    "REOPEN_WINDOW",
    "RESOLUTION_PROPOSAL_WINDOW",
    "RESOLVED_REASON_AUTO_STALE",
    "RESOLVED_REASON_LANDLORD",
    "RESOLVED_REASON_TENANT_CONFIRMED",
    "STATUS_AWAITING_APPROVAL",
    "STATUS_AWAITING_TENANT",
    "STATUS_OPEN",
    "STATUS_REOPENED",
    "STATUS_RESOLVED",
    "CaseSnapshot",
    "ContradictResolutionResult",
    "OpenCase",
    "ProposeResolutionResult",
    "ReopenDecision",
    "RoutingResult",
    "RoutingSignal",
    "SweepAction",
    "Transition",
    "apply_time_transitions",
    "contradict_resolution",
    "decide_reopen_or_new",
    "propose_resolution",
    "resolve_by_landlord",
    "route_inbound_message",
    "sweep_cases",
]
