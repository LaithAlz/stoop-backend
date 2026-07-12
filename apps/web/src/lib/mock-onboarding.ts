/**
 * Mock data + types for the self-serve onboarding wizard (issue #113).
 * Mock-first, same doctrine as the other Clarity screens (app.index.tsx,
 * app.conversations.*): no real auth/API calls happen here. Field names
 * mirror `schema-v1.md` / `api-contracts.md` where a real column exists —
 * see the CONTRACT GAP note below (on `Tone`) for the one place this
 * wizard's shape diverges from issue #113's AC.
 */

/**
 * `landlords.voice_profile` shape (schema-v1.md:352): `{tone: text,
 * samples: text[]}`.
 *
 * CONTRACT GAP: issue #113's AC says "Voice profile stored per
 * property," but the schema puts `voice_profile` on `landlords`, not
 * `properties` — confirmed by the agent's own context loader
 * (`apps/api/app/agent/nodes/load_context.py`, `_load_voice_profile`),
 * which reads it with `SELECT voice_profile FROM landlords WHERE id =
 * :landlord_id`, never by `property_id`. This wizard follows the real
 * schema (one voice profile per landlord account, shared across every
 * property they own) rather than silently reshaping the AC's words to
 * fit — `OnboardingState.voiceSamples`/`.tone` below become that single
 * per-landlord `voice_profile`, not a per-property field.
 */
export type Tone = "warm" | "direct" | "formal" | "casual";

export interface ToneOption {
  value: Tone;
  label: string;
  /** A short plain-English SMS example so the tone reads as a real
   * reply, not a mood word in the abstract. */
  example: string;
}

export const toneOptions: ToneOption[] = [
  {
    value: "warm",
    label: "Warm",
    example: "Hi Elena — sorry about that, I'll get someone out today.",
  },
  { value: "direct", label: "Direct", example: "Got it. Plumber's booked for 1 PM Thursday." },
  {
    value: "formal",
    label: "Formal",
    example: "Thank you for letting us know. A technician will attend today.",
  },
  { value: "casual", label: "Casual", example: "Ah no worries, I'll get Tony out there today!" },
];

/** Same option wording as the previous (Heritage) onboarding step — kept
 * verbatim rather than re-litigated, since copy-guardian already cleared
 * these words once and they haven't changed meaning. */
export const petsOptions = ["No pets", "Cats only", "Cats & dogs", "With deposit"];
export const smokingOptions = ["No", "Outside only", "Allowed"];
export const guestsOptions = ["No restriction", "Overnight only with notice", "Other"];

/** `tenants.vulnerable_occupant` CHECK values (schema-v1.md) plus the
 * unset/"none" case, which stores `null`. Raising a heat/power/water
 * failure one severity level for a vulnerable occupant is a real rubric
 * rule (severity-rubric-v1.md's "vulnerable-occupant modifier") — this
 * is why the wizard asks, not just a demographic field. */
export type VulnerableOccupant = "none" | "infant" | "elderly" | "medical_device";

export const vulnerableOptions: { value: VulnerableOccupant; label: string }[] = [
  { value: "none", label: "No one" },
  { value: "infant", label: "An infant" },
  { value: "elderly", label: "An elderly person" },
  { value: "medical_device", label: "Someone on powered medical equipment" },
];

/** `properties.heating_season` jsonb (schema-v1.md), default
 * `{"start":"09-15","end":"06-01"}` — a curated month/day picklist
 * rather than a free date picker; the rubric only needs the window, not
 * day-level precision beyond what a landlord can name from memory. */
export const heatingSeasonStartOptions = [
  { value: "09-01", label: "September 1" },
  { value: "09-15", label: "September 15" },
  { value: "10-01", label: "October 1" },
];

export const heatingSeasonEndOptions = [
  { value: "05-01", label: "May 1" },
  { value: "05-15", label: "May 15" },
  { value: "06-01", label: "June 1" },
  { value: "06-15", label: "June 15" },
];

export interface OnboardingTenant {
  /** Client-only list key — never sent anywhere, generated from a plain
   * incrementing counter in an event handler (never at render time), so
   * server and client markup can never diverge (SSR discipline). */
  id: string;
  name: string;
  phone: string;
  unit: string;
  vulnerableOccupant: VulnerableOccupant;
}

export interface BackupContact {
  name: string;
  phone: string;
}

export interface OnboardingAccount {
  fullName: string;
  email: string;
  /** `landlords.phone` — E.164 in the real schema; where emergency calls
   * ring, so this wizard asks for it up front rather than treating it
   * as a generic profile field. */
  phone: string;
}

export interface OnboardingProperty {
  /** `properties.label`. */
  nickname: string;
  addressLine1: string;
  city: string;
  province: string;
  postalCode: string;
}

export interface HouseRules {
  pets: string;
  smoking: string;
  parking: string;
  guests: string;
  /** `properties.quiet_hours` jsonb, default `{"start":"21:00","end":"08:00"}`. */
  quietStart: string;
  quietEnd: string;
  heatingSeasonStart: string;
  heatingSeasonEnd: string;
}

export interface OnboardingState {
  account: OnboardingAccount;
  property: OnboardingProperty;
  tenants: OnboardingTenant[];
  /** Becomes `landlords.voice_profile.samples` — per-landlord, not
   * per-property; see the CONTRACT GAP note on `Tone` above. */
  voiceSamples: string[];
  /** Becomes `landlords.voice_profile.tone` — same per-landlord note. */
  tone: Tone;
  houseRules: HouseRules;
  backupContact: BackupContact;
  disclosureSent: boolean;
}

export const DEFAULT_ONBOARDING_STATE: OnboardingState = {
  account: { fullName: "", email: "", phone: "" },
  property: { nickname: "", addressLine1: "", city: "", province: "ON", postalCode: "" },
  tenants: [{ id: "tenant-1", name: "", phone: "", unit: "", vulnerableOccupant: "none" }],
  voiceSamples: [""],
  tone: "warm",
  houseRules: {
    pets: "Cats only",
    smoking: "No",
    parking: "",
    guests: "Overnight only with notice",
    quietStart: "21:00",
    quietEnd: "08:00",
    heatingSeasonStart: "09-15",
    heatingSeasonEnd: "06-01",
  },
  backupContact: { name: "", phone: "" },
  disclosureSent: false,
};

export const ONBOARDING_STEPS = [
  "welcome",
  "account",
  "property",
  "tenants",
  "voice",
  "backup",
  "done",
] as const;

export type OnboardingStep = (typeof ONBOARDING_STEPS)[number];

/** A mock, never-really-provisioned number — distinct from every number
 * already used by the other mock screens (mock-property.ts) so nobody
 * confuses this wizard's fake property with an existing mock one. */
export const MOCK_PROVISIONED_NUMBER = "(647) 555-0199";

/**
 * The tenant-notice template from `docs/06-legal/pilot-kit.md` §1 (SMS
 * version), paraphrased down to the two facts every tenant needs — the
 * number changed and a person still reads it — without the wizard
 * inventing new legal language. `docs/06-legal/pilot-kit.md` says this
 * copy needs the same lawyer pass as the Terms of Service before it's
 * final; this wizard shows it as a copyable draft, not a binding notice.
 */
export function buildDisclosureMessage(
  landlordFirstName: string,
  propertyNickname: string,
): string {
  const who = landlordFirstName.trim() || "your landlord";
  const property = propertyNickname.trim() || "the property";
  return (
    `Hi, it's ${who}. I've set up a new number for anything about ${property} — ` +
    `repairs, questions, anything: ${MOCK_PROVISIONED_NUMBER}. Software helps me read ` +
    `and reply faster — I still see and approve everything, and a real emergency ` +
    `reaches me immediately, day or night. Texting works exactly like texting me.`
  );
}

/** The test round-trip AC ("a test tenant message round-trips before the
 * wizard says 'done'") — a fixed, plain-English exchange, not a live
 * send. `outbound` is built from the property nickname so the message
 * still reads correctly with whatever name the landlord chose. */
export const testTextInbound = "test";
export function buildTestTextReply(propertyNickname: string): string {
  const property = propertyNickname.trim() || "your property";
  return `Got it — I'm live for ${property}. I'll draft your first reply the moment a tenant texts in.`;
}
