/**
 * Confirmation copy for deleting a property (api-contracts.md v1.12:
 * `DELETE /v1/properties/{id}?confirm=true`). Pure strings, ported verbatim
 * from apps/mobile/src/features/properties/deleteProperty.ts (campaign
 * issue #234 PR 4; that copy was already reviewed there) so both clients
 * say exactly the same thing about the same irreversible action.
 *
 * Honesty requirements baked in:
 * - The row delete is immediate and permanent — said plainly.
 * - The number's release is NOT instant: the contract gives it a 24-hour
 *   server-side hold before it's gone for good. We say "after a 24-hour
 *   hold" instead of pretending it vanishes now — and we do NOT present
 *   that hold as an undo (it isn't one; the property itself is already
 *   gone).
 * - A property with open cases (409 `has_open_cases`) or any saved
 *   history/tenants (409 `has_dependents`) can't be deleted — those land
 *   as their own house lines from src/api/errors.ts after the attempt.
 */
export const DELETE_PROPERTY_TITLE = "Delete this property?";

export const DELETE_PROPERTY_MESSAGE =
  "This can't be undone. Its phone number stops taking tenant texts right away, " +
  "and the number itself is fully released after a 24-hour hold — a release isn't instant.";

export const DELETE_PROPERTY_CONFIRM_LABEL = "Delete property";

/**
 * L4 (#258 follow-up): the dialog above is pure destruction framing and
 * never named what's actually blocking a delete, even though the property
 * detail page already knows two of the FK-`RESTRICT` blockers
 * (schema-v1.md: `tenants.property_id`/`cases.property_id`, both
 * `ON DELETE RESTRICT`) BEFORE the landlord ever clicks delete — the
 * confirm-copy said nothing while a caption below the button carried the
 * only hint. This does not claim completeness: `messages.property_id` and
 * `trust_metrics.property_id` are also `RESTRICT` (api-contracts.md's
 * v1.9 amendment) and aren't independently visible on this page, so a
 * property with neither a known tenant row nor an open case can still
 * 409. The function therefore only ever ASSERTS a block when one is
 * actually known (deterministic — `RESTRICT` means present rows always
 * block); it never asserts success by omission.
 *
 * `tenantCount` must be the RAW tenant-row count (every row the property
 * detail page's `useTenants` query returns, not the active-only list it
 * renders) — a soft-deleted tenant (`active = false`) still leaves its
 * row in place and still blocks the delete via the same FK.
 */
export function deletePropertyMessage(knownBlockers: {
  tenantCount: number;
  openCaseCount: number;
}): string {
  const parts: string[] = [];
  if (knownBlockers.openCaseCount > 0) {
    parts.push(
      knownBlockers.openCaseCount === 1
        ? "1 open case"
        : `${knownBlockers.openCaseCount} open cases`,
    );
  }
  if (knownBlockers.tenantCount > 0) {
    parts.push(
      knownBlockers.tenantCount === 1
        ? "1 tenant on file"
        : `${knownBlockers.tenantCount} tenants on file`,
    );
  }
  if (parts.length === 0) return DELETE_PROPERTY_MESSAGE;
  return `This property has ${parts.join(" and ")}, so the delete will be blocked until those are cleared. ${DELETE_PROPERTY_MESSAGE}`;
}
