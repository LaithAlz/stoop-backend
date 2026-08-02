/**
 * First-name-only display, matching the mockup's "Maria", never a full
 * legal name in chrome. Falls back to the full string if it's already a
 * single token. Ported verbatim from apps/mobile/src/lib/tenantName.ts
 * (campaign issue #234 PR 2) — `tenants.name` (schema-v1.md) is a plain,
 * unconstrained `text` column, so `QueueItem.tenant_name` isn't guaranteed
 * to already be first-name-only the way the api-contracts.md example
 * ("Maria") suggests.
 */
export function firstName(fullName: string | null | undefined): string {
  if (!fullName) return "Your tenant";
  const trimmed = fullName.trim();
  if (!trimmed) return "Your tenant";
  return trimmed.split(/\s+/)[0];
}
