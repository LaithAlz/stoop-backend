"""Shared trust-ladder (#60) helpers — ``trust_metrics`` reads/writes used by
more than one call site: the graph's auto-send eligibility check
(``app/agent/nodes/auto_send.py``), the sender's own graduation write
(``app/agent/draft_sender.py``), and the landlord-facing revoke endpoint
(``app/routers/trust.py``).

Session-only module — never on the admin-session allowlist
------------------------------------------------------------------------
Every function here takes an ALREADY-OPEN ``AsyncSession``; this module
never opens its own admin-engine session (``app.db.session``'s bypass
-RLS helper). It therefore does NOT need an entry in
``tests/test_migrations_0005.py``'s ``_ADMIN_SESSION_ALLOWLIST`` — mirrors
``app/audit.py``'s own "session-only helper" convention exactly.

``'routine'`` hardcoded everywhere — belt-and-braces (#60 safety review
requirement)
------------------------------------------------------------------------
``'routine'`` is the ONLY severity ``trust_metrics.autonomy_unlocked`` may
ever be ``true`` for (schema-v1.md's own column comment: "only ever true
for routine in v1"; CLAUDE.md rule 3: "Auto-send exists only via the trust
ladder, only for `routine`"). Every SQL statement below spells the literal
``'routine'`` directly in the query text, never as a bound parameter
derived from caller input — a caller mistake (e.g. accidentally passing an
``'urgent'``/``'emergency'`` severity through) can therefore never smuggle
a non-routine row past this module's own gate; enforcement lives in SQL,
not just in whichever code path happened to call in here.

Append-only ``audit_log`` (never-break rule #2)
------------------------------------------------------------------------
The revoke helpers below only ever INSERT into ``audit_log`` — never
UPDATE/DELETE, matching every other writer in this codebase.
"""

from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

GRADUATION_SEVERITY = "routine"
"""The one severity value trust_metrics.autonomy_unlocked may ever be true
for in v1 — see module docstring. Exported so callers never need to spell
the literal themselves (and so a future v2 change has exactly one place to
edit, alongside the SQL literals below, which must be updated in lockstep
by hand — they are deliberately NOT interpolated from this constant, so
that a grep for the literal ``'routine'`` finds every enforcement point at
once)."""

# ---------------------------------------------------------------------------
# Eligibility read — the auto-send gate.
# ---------------------------------------------------------------------------

_SELECT_ROUTINE_AUTONOMY_SQL = text(
    "SELECT autonomy_unlocked, revoked_at FROM trust_metrics "
    "WHERE property_id = :property_id AND severity = 'routine'"
)


async def is_routine_autonomy_unlocked(session: AsyncSession, *, property_id: UUID) -> bool:
    """``True`` only when *property_id*'s ``'routine'`` ``trust_metrics``
    row exists, is unlocked, AND has not been revoked. A missing row
    (never graduated) reads as ``False`` — never fabricated. Callers that
    need this to gate a real send (``app/agent/nodes/auto_send.py``) must
    treat ANY exception raised here as ``False`` too (fail-closed to the
    normal landlord-approval interrupt) — that is the CALLER's contract,
    not enforced inside this function, so it stays a plain, honest read.
    """
    row = (
        (await session.execute(_SELECT_ROUTINE_AUTONOMY_SQL, {"property_id": str(property_id)}))
        .mappings()
        .one_or_none()
    )
    if row is None:
        return False
    return bool(row["autonomy_unlocked"]) and row["revoked_at"] is None


# ---------------------------------------------------------------------------
# Revocation — the landlord-facing hook (#60 AC-4) and the (currently
# unwired, see app/agent/nodes/auto_send.py's own report note) global
# misclassification hook (#60 AC-2's "any severity misclassification
# revokes globally").
# ---------------------------------------------------------------------------

# Re-graduation semantics (documented once, here, since both revoke
# functions share it): revoking ALSO resets consecutive_clean to 0 on the
# rows it touches. Without this, a property that had already accumulated
# >= threshold consecutive clean sends BEFORE being revoked would
# re-graduate on the very NEXT clean send after revoke (autonomy_unlocked
# was the only thing standing in the graduation UPDATE's WHERE clause) --
# i.e. "revoke" would be nearly a no-op for a landlord who reacts a moment
# too late. Resetting the counter means re-earning autonomy after a revoke
# genuinely requires N NEW consecutive clean sends, matching what a
# landlord reading "autonomy revoked" would expect it to mean.
_REVOKE_PROPERTY_ROUTINE_SQL = text(
    "UPDATE trust_metrics SET autonomy_unlocked = false, revoked_at = now(), "
    "consecutive_clean = 0, updated_at = now() "
    "WHERE landlord_id = :landlord_id AND property_id = :property_id "
    "AND severity = 'routine' AND autonomy_unlocked = true "
    "RETURNING id"
)

_REVOKE_ALL_FOR_LANDLORD_SQL = text(
    "UPDATE trust_metrics SET autonomy_unlocked = false, revoked_at = now(), "
    "consecutive_clean = 0, updated_at = now() "
    "WHERE landlord_id = :landlord_id AND autonomy_unlocked = true "
    "RETURNING id"
)

_INSERT_TRUST_REVOKED_AUDIT_SQL = text(
    "INSERT INTO audit_log (landlord_id, case_id, actor, action, payload) "
    "VALUES (:landlord_id, NULL, :actor, 'trust_revoked', CAST(:payload AS jsonb))"
)


async def revoke_property_autonomy(
    session: AsyncSession,
    *,
    landlord_id: UUID,
    property_id: UUID,
    actor: str,
    reason: str,
) -> int:
    """Revoke *property_id*'s ``'routine'`` autonomy (the only severity
    that can ever be unlocked, schema-v1.md) for *landlord_id*.

    Idempotent: a row that is already revoked (or was never unlocked at
    all) matches zero rows in the ``UPDATE``'s own ``WHERE`` clause, so a
    repeat call is a harmless no-op WRITE-wise. A ``trust_revoked``
    ``audit_log`` row is still appended on EVERY call regardless (the
    landlord's action is real and worth recording even when there was
    nothing left to revoke) — see ``app/routers/trust.py``'s own docstring
    for the endpoint-level idempotency contract this backs.

    Returns the number of rows actually flipped (0 or 1 — ``UNIQUE
    (property_id, severity)`` bounds this to at most one).
    """
    rows = (
        (
            await session.execute(
                _REVOKE_PROPERTY_ROUTINE_SQL,
                {"landlord_id": str(landlord_id), "property_id": str(property_id)},
            )
        )
        .mappings()
        .all()
    )
    await session.execute(
        _INSERT_TRUST_REVOKED_AUDIT_SQL,
        {
            "landlord_id": str(landlord_id),
            "actor": actor,
            "payload": json.dumps(
                {
                    "scope": "property",
                    "property_id": str(property_id),
                    "severity": GRADUATION_SEVERITY,
                    "reason": reason,
                    "affected_count": len(rows),
                }
            ),
        },
    )
    return len(rows)


async def revoke_all_autonomy(
    session: AsyncSession,
    *,
    landlord_id: UUID,
    actor: str,
    reason: str,
) -> int:
    """Revoke EVERY currently-unlocked ``(property, severity)`` row for
    *landlord_id* — the "any severity misclassification revokes globally"
    hook (#60 AC). No misclassification SIGNAL exists anywhere in this
    codebase yet to call this automatically (verified at implementation
    time — see this issue's own report); this function is wired + tested
    here so a future #66-70 eval/feedback signal has a single,
    already-reviewed function to call rather than inventing its own revoke
    logic. It is ALSO the function the landlord-facing ``scope="global"``
    branch of ``POST /v1/properties/{id}/trust/revoke`` calls
    (``actor='landlord'``).

    Idempotent in the same sense as :func:`revoke_property_autonomy` above
    (see that function's own docstring) — always audited, only actually
    mutates rows that are currently unlocked.
    """
    rows = (
        (await session.execute(_REVOKE_ALL_FOR_LANDLORD_SQL, {"landlord_id": str(landlord_id)}))
        .mappings()
        .all()
    )
    await session.execute(
        _INSERT_TRUST_REVOKED_AUDIT_SQL,
        {
            "landlord_id": str(landlord_id),
            "actor": actor,
            "payload": json.dumps(
                {"scope": "global", "reason": reason, "affected_count": len(rows)}
            ),
        },
    )
    return len(rows)


__all__: list[str] = [
    "GRADUATION_SEVERITY",
    "is_routine_autonomy_unlocked",
    "revoke_all_autonomy",
    "revoke_property_autonomy",
]
