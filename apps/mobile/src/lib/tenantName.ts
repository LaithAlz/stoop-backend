/** First-name-only display, matching the mockup's "Maria", never a full
 *  legal name in chrome. Falls back to the full string if it's already a
 *  single token (the queue contract's own example is first-name-only). */
export function firstName(fullName: string | null | undefined): string {
  if (!fullName) return "Your tenant";
  const trimmed = fullName.trim();
  if (!trimmed) return "Your tenant";
  return trimmed.split(/\s+/)[0];
}
