/**
 * Display logic for a property's own Stoop number (`Property.
 * twilio_number`) — the number tenants text. Pure, unit-testable.
 *
 * The null case is real, not theoretical: properties created before
 * provisioning shipped (#53 — api-contracts.md v1.12: "`twilio_number` ...
 * previously always `null`") have no number, and there is no re-provision
 * endpoint in the contract. The copy for that state is honest about the
 * consequence (tenants can't text this property) without promising a fix
 * date — flagged in the M2 report as a follow-up the API needs to offer.
 */

/** "+14165550134" → "(416) 555-0134"; anything that isn't a NANP number
 *  (a `+` prefix other than `+1`, or not 10/11 digits) renders exactly as
 *  stored — formatting a foreign number as North American would be
 *  invented information, not presentation. */
export function formatStoopNumber(e164: string): string {
  if (e164.startsWith("+") && !e164.startsWith("+1")) return e164;
  const digits = e164.replace(/\D/g, "");
  const national = digits.length === 11 && digits.startsWith("1") ? digits.slice(1) : digits;
  if (national.length !== 10) return e164;
  return `(${national.slice(0, 3)}) ${national.slice(3, 6)}-${national.slice(6)}`;
}

export const NO_NUMBER_TITLE = "No Stoop number yet";

export const NO_NUMBER_BODY =
  "This property doesn't have its own phone number, so tenants can't text it yet. " +
  "Contact support to get one set up.";

/** The one-line explanation under the number itself — what the number IS. */
export const NUMBER_CAPTION = "This is the number your tenants text.";
