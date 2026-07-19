/**
 * The tenant-notice message for the wizard's final step — the SAME wording
 * the web onboarding ships (apps/web/src/lib/mock-onboarding.ts's
 * `buildDisclosureMessage`, itself paraphrased from docs/06-legal/
 * pilot-kit.md §1 and already through copy review), with one real
 * difference: the number interpolated here is the property's REAL
 * provisioned `twilio_number`, never a mock. Shown as a copyable/shareable
 * draft, not a binding notice (the pilot kit flags this copy for the same
 * lawyer pass as the ToS).
 */
import { formatStoopNumber } from "@/features/properties/stoopNumber";

export function buildDisclosureMessage(
  landlordFirstName: string,
  propertyLabel: string,
  stoopNumber: string,
): string {
  const who = landlordFirstName.trim() || "your landlord";
  const property = propertyLabel.trim() || "the property";
  return (
    `Hi, it's ${who}. I've set up a new number for anything about ${property} — ` +
    `repairs, questions, anything: ${formatStoopNumber(stoopNumber)}. Software helps me read ` +
    `and reply faster — I still see and approve everything, and a real emergency ` +
    `reaches me immediately, day or night. Texting works exactly like texting me.`
  );
}
