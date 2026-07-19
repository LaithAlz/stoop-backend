/**
 * Confirmation copy for deleting a property (api-contracts.md v1.12:
 * `DELETE /v1/properties/{id}?confirm=true`). Pure strings, test-visible.
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
