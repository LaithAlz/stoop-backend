/**
 * Whether a case should be treated as an emergency for CHROME purposes
 * (the thread's `EmergencyBanner`/severity plaque) — deliberately NOT just
 * `severity === "emergency"`.
 *
 * H1 (safety review, #234 PR 3 fix round — verified backend-side):
 * `CaseDetail.severity` is `Severity | null`, and it is `null` both
 * TRANSIENTLY (the case row exists but `classify_severity` — the only
 * writer of `cases.severity`, schema-v1.md — hasn't run yet) and
 * PERMANENTLY when classification fails (degraded mode). Tier-0
 * (`emergency-prefilter.md`) runs before any of that and can already have
 * the phone/SMS escalation chain ringing before a case row — and
 * therefore a severity — exists at all. A client that reads
 * `severity === null` as "not an emergency" would silently DE-ESCALATE a
 * live Tier-0 fire on screen while the landlord's phone is still ringing
 * — the one thing this app must never do (CLAUDE.md rule 1: "the
 * emergency line is never paywalled, throttled, or gated" — the softer
 * cousin of that rule is "never hidden from view" either).
 *
 * The `emergency_triggered` audit row is written by that SAME Tier-0
 * prefilter (schema-v1.md's `audit_log.action` vocabulary) and survives
 * regardless of whether classification ever completes, so its presence in
 * the timeline is an authoritative, severity-independent signal.
 *
 * Round-3 residual (safety re-verify): the trigger is honored
 * UNCONDITIONALLY, not just when severity is null. The backend has no
 * legitimate emergency→lower transition (`UPDATE … WHERE severity IS
 * DISTINCT FROM 'emergency'` — the Tier-0 clamp), so a case carrying an
 * `emergency_triggered` row with a lower written severity means the
 * backend's own clamp failed (e.g. `_parse_prefilter` falling back to
 * `hard_hit=False` on a missing/malformed `messages.prefilter`). Honoring
 * the trigger in that state can only ever fail TOWARD the alarm — the
 * correct direction on this surface.
 *
 * The emergency takeover (app.conversations.$id_.emergency.tsx) uses a
 * WIDER clamp for its own "still active" state (`severity === null` is
 * always treated as still-active there, full stop) because that screen
 * has nothing softer to fall back to display — this predicate is for the
 * thread's banner/plaque, which has other chrome to fall back to and
 * would otherwise show the full alarming banner on every ordinary
 * not-yet-classified case (ordinary case volume, not just emergencies).
 */
import type { CaseDetail } from "@/api/types";

export function hasEmergencyTrigger(caseDetail: Pick<CaseDetail, "timeline">): boolean {
  return caseDetail.timeline.some(
    (entry) => entry.kind === "audit" && entry.action === "emergency_triggered",
  );
}

export function isEmergencySignal(caseDetail: Pick<CaseDetail, "severity" | "timeline">): boolean {
  return caseDetail.severity === "emergency" || hasEmergencyTrigger(caseDetail);
}
