import { createFileRoute, Link } from "@tanstack/react-router";
import {
  cloneElement,
  isValidElement,
  useEffect,
  useRef,
  useState,
  type ReactElement,
} from "react";
import {
  ArrowLeft,
  ArrowRight,
  Copy,
  Loader2,
  Phone,
  Plus,
  ShieldCheck,
  Trash2,
} from "lucide-react";
import { toast } from "sonner";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { SeverityPlaque } from "@/components/clarity/SeverityPlaque";
import { MarginNote } from "@/components/clarity/MarginNote";
import { trackEvent } from "@/lib/analytics";
import { cn } from "@/lib/utils";
import {
  DEFAULT_ONBOARDING_STATE,
  MOCK_PROVISIONED_NUMBER,
  buildDisclosureMessage,
  buildTestTextReply,
  guestsOptions,
  heatingSeasonEndOptions,
  heatingSeasonStartOptions,
  petsOptions,
  smokingOptions,
  testTextInbound,
  toneOptions,
  vulnerableOptions,
  type BackupContact,
  type HouseRules,
  type OnboardingAccount,
  type OnboardingProperty,
  type OnboardingState,
  type OnboardingStep,
  type OnboardingTenant,
  type Tone,
  type VulnerableOccupant,
} from "@/lib/mock-onboarding";

/**
 * Self-serve onboarding (issue #113), rebuilt on the Clarity design
 * system — mock-first, exactly like the other Clarity screens
 * (app.index.tsx, app.conversations.*): no real auth/API calls, no
 * Supabase, no Twilio. Every dynamic bit of copy (option lists, the
 * tenant-notice template, the test-text exchange) lives in
 * lib/mock-onboarding.ts, not hardcoded in this file.
 *
 * Step order: welcome → account → your first property → tenants →
 * how Stoop texts (voice sample + tone + house rules) → backup
 * contact & emergency basics → done (provision → tenant notice →
 * test round-trip). This covers every field issue #113's AC calls for
 * (house rules, heating season, backup contact, voice-profile capture,
 * tenant disclosure + confirm-sent checkbox, first-message test) with
 * one deliberate reshuffle from the AC's literal "property details"
 * grouping: heating season moved next to house rules (same
 * `properties` table neighbourhood in schema-v1.md as `quiet_hours`,
 * and the same "rules Stoop follows" narrative as tone/voice), and
 * backup contact moved to its own step so it can sit next to the
 * emergency-escalation explanation (`SeverityPlaque` + the plain-English
 * "who we call next" copy) instead of being one more field on the
 * property-basics form.
 *
 * Dropped from the old (Heritage) onboarding: lease basics (rent/deposit/
 * lease end — no `properties` column for any of these in schema-v1.md)
 * and vendor capture. The vendor drop is scope, not a schema gap: the
 * `vendors` table is real and fully schema-backed (schema-v1.md's
 * `vendors` table — name/trade/phone/notes/working_hours/active); it's
 * just that issue #113's AC never lists a vendor-capture step, so this
 * wizard leaves it to Properties → Add vendor (already built,
 * app.properties_.$id_.settings.tsx) rather than inventing a step the AC
 * doesn't ask for. (The one genuinely ungrounded field in the old
 * Heritage vendor step was `vendorAfterHours` — no matching schema-v1.md
 * column — which is why that field specifically never made it into the
 * settings screen's mock `Vendor` type either.)
 *
 * Voice-sample minimum (founder-vetoable ruling, orchestrator synthesis
 * of issue #113's AC — "3–5 pasted real replies" — and this wizard's own
 * skip pattern): ≥3 non-empty samples if the step is engaged at all
 * (Continue blocks below that with a plain-English error), but the step
 * stays fully skippable via "Skip for now" — voice/drafting is Full Plan
 * territory per the welcome step's own free-vs-paid framing, so deferring
 * it entirely (skip) has to stay as valid a path as clearing the AC's bar.
 *
 * A11y: each step focuses its own `<h1>` on mount (a fresh mount per
 * step, since the active step unmounts/remounts rather than persisting
 * hidden siblings) — the #190 focus-management pattern applied to a
 * multi-step flow instead of a route change. Every `Field` with an error
 * wires `aria-invalid` + `aria-describedby` onto its control (matching
 * design-system.tsx's established error pattern, ~line 305).
 */

export const Route = createFileRoute("/onboarding")({
  head: () => ({
    meta: [
      { title: "Get set up — Stoop." },
      { name: "robots", content: "noindex" },
      {
        name: "description",
        content:
          "Set up your first property on Stoop. Five short steps, about five minutes — the Emergency Line is free, forever.",
      },
    ],
  }),
  component: OnboardingPage,
});

const STORAGE_KEY = "stoop.onboarding.clarity.v1";

function useOnboardingState() {
  const [state, setState] = useState<OnboardingState>(DEFAULT_ONBOARDING_STATE);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) setState({ ...DEFAULT_ONBOARDING_STATE, ...JSON.parse(raw) });
    } catch {
      /* ignore — mock persistence only, never load-bearing */
    }
    setHydrated(true);
  }, []);

  useEffect(() => {
    if (!hydrated) return;
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch {
      /* ignore */
    }
  }, [state, hydrated]);

  return [state, setState] as const;
}

function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/* ---------------- Page ---------------- */

function OnboardingPage() {
  const [step, setStep] = useState<OnboardingStep>("welcome");
  const [state, setState] = useOnboardingState();
  const tenantCounter = useRef(1);

  useEffect(() => {
    trackEvent("onboarding_step_viewed", { props: { step } });
  }, [step]);

  const updateAccount = (patch: Partial<OnboardingAccount>) =>
    setState((s) => ({ ...s, account: { ...s.account, ...patch } }));
  const updateProperty = (patch: Partial<OnboardingProperty>) =>
    setState((s) => ({ ...s, property: { ...s.property, ...patch } }));
  const updateHouseRules = (patch: Partial<HouseRules>) =>
    setState((s) => ({ ...s, houseRules: { ...s.houseRules, ...patch } }));
  const updateBackupContact = (patch: Partial<BackupContact>) =>
    setState((s) => ({ ...s, backupContact: { ...s.backupContact, ...patch } }));
  const updateTenant = (id: string, patch: Partial<OnboardingTenant>) =>
    setState((s) => ({
      ...s,
      tenants: s.tenants.map((t) => (t.id === id ? { ...t, ...patch } : t)),
    }));
  const addTenant = () => {
    tenantCounter.current += 1;
    setState((s) => ({
      ...s,
      tenants: [
        ...s.tenants,
        {
          id: `tenant-new-${tenantCounter.current}`,
          name: "",
          phone: "",
          unit: "",
          vulnerableOccupant: "none" as VulnerableOccupant,
        },
      ],
    }));
  };
  const removeTenant = (id: string) =>
    setState((s) => ({ ...s, tenants: s.tenants.filter((t) => t.id !== id) }));
  const updateVoiceSample = (index: number, value: string) =>
    setState((s) => ({
      ...s,
      voiceSamples: s.voiceSamples.map((v, i) => (i === index ? value : v)),
    }));
  const addVoiceSample = () =>
    setState((s) =>
      s.voiceSamples.length >= 5 ? s : { ...s, voiceSamples: [...s.voiceSamples, ""] },
    );
  const removeVoiceSample = (index: number) =>
    setState((s) =>
      s.voiceSamples.length <= 1
        ? s
        : { ...s, voiceSamples: s.voiceSamples.filter((_, i) => i !== index) },
    );
  const setTone = (tone: Tone) => setState((s) => ({ ...s, tone }));
  const setDisclosureSent = (v: boolean) => setState((s) => ({ ...s, disclosureSent: v }));

  return (
    <PhoneFrame>
      {step === "welcome" && <WelcomeStep onNext={() => setStep("account")} />}

      {step === "account" && (
        <AccountStep
          state={state}
          update={updateAccount}
          onBack={() => setStep("welcome")}
          onNext={() => setStep("property")}
        />
      )}

      {step === "property" && (
        <PropertyStep
          state={state}
          update={updateProperty}
          onBack={() => setStep("account")}
          onNext={() => setStep("tenants")}
        />
      )}

      {step === "tenants" && (
        <TenantsStep
          state={state}
          onAdd={addTenant}
          onRemove={removeTenant}
          onUpdate={updateTenant}
          onBack={() => setStep("property")}
          onNext={() => setStep("voice")}
        />
      )}

      {step === "voice" && (
        <VoiceStep
          state={state}
          onUpdateSample={updateVoiceSample}
          onAddSample={addVoiceSample}
          onRemoveSample={removeVoiceSample}
          onToneChange={setTone}
          onHouseRulesChange={updateHouseRules}
          onBack={() => setStep("tenants")}
          onNext={() => setStep("backup")}
        />
      )}

      {step === "backup" && (
        <BackupStep
          state={state}
          onChange={updateBackupContact}
          onBack={() => setStep("voice")}
          onNext={() => setStep("done")}
        />
      )}

      {step === "done" && (
        <DoneStep
          state={state}
          onDisclosureSentChange={setDisclosureSent}
          onBack={() => setStep("backup")}
        />
      )}
    </PhoneFrame>
  );
}

/* ---------------- Shared field primitives ---------------- */

const inputClass =
  "h-12 w-full rounded-clarity-md border-[1.5px] border-clarity-line-strong bg-clarity-panel px-3.5 font-clarity-sans text-[15px] text-clarity-ink placeholder:text-clarity-ink-dim/60";

/**
 * Wires the error <-> control association design-system.tsx already
 * establishes (~line 305): the error `<p>` gets a stable id, and the
 * single form control this `Field` wraps gets `aria-invalid` +
 * `aria-describedby` pointing at it — via `cloneElement` so every call
 * site gets this for free instead of wiring it by hand at each one.
 * Only fires when both `htmlFor` and `error` are set, which in this file
 * is exactly the single-input fields (name/email/phone/address/etc.) —
 * the multi-input rows (quiet hours, heating season) and the ChipGroup
 * rows never pass `htmlFor`, so they're untouched.
 */
function Field({
  label,
  htmlFor,
  helper,
  error,
  children,
}: {
  label: string;
  htmlFor?: string;
  helper?: string;
  error?: string;
  children: React.ReactNode;
}) {
  const errorId = htmlFor ? `${htmlFor}-err` : undefined;
  const control =
    error && errorId && isValidElement(children)
      ? cloneElement(children as ReactElement<Record<string, unknown>>, {
          "aria-invalid": true,
          "aria-describedby": errorId,
        })
      : children;

  return (
    <div>
      {htmlFor ? (
        <label
          htmlFor={htmlFor}
          className="mb-1.5 block font-clarity-sans text-[13px] font-bold text-clarity-ink"
        >
          {label}
        </label>
      ) : (
        <span className="mb-1.5 block font-clarity-sans text-[13px] font-bold text-clarity-ink">
          {label}
        </span>
      )}
      {control}
      {error ? (
        <p
          id={errorId}
          role="alert"
          className="mt-1.5 font-clarity-sans text-xs font-semibold text-clarity-emergency"
        >
          {error}
        </p>
      ) : helper ? (
        <p className="mt-1.5 font-clarity-sans text-xs text-clarity-ink-dim">{helper}</p>
      ) : null}
    </div>
  );
}

function ChipGroup({
  options,
  value,
  onChange,
  ariaLabel,
}: {
  options: string[];
  value: string;
  onChange: (v: string) => void;
  ariaLabel: string;
}) {
  return (
    <div role="radiogroup" aria-label={ariaLabel} className="flex flex-wrap gap-2">
      {options.map((o) => {
        const active = value === o;
        return (
          <button
            key={o}
            type="button"
            role="radio"
            aria-checked={active}
            onClick={() => onChange(o)}
            className={cn(
              "min-h-11 rounded-clarity-md border-[1.5px] px-4 font-clarity-sans text-sm font-bold transition-colors",
              active
                ? "border-clarity-brand-deep bg-clarity-brand text-clarity-brand-on"
                : "border-clarity-line-strong bg-clarity-panel text-clarity-ink hover:border-clarity-brand-border",
            )}
          >
            {o}
          </button>
        );
      })}
    </div>
  );
}

/* ---------------- Wizard chrome (the five numbered steps) ---------------- */

const TOTAL_STEPS = 5;

interface WizardChromeProps {
  stepNumber: number;
  title: string;
  subtitle?: string;
  onBack: () => void;
  onNext: () => void;
  nextLabel?: string;
  nextDisabled?: boolean;
  skip?: () => void;
  skipLabel?: string;
  children: React.ReactNode;
}

function WizardChrome({
  stepNumber,
  title,
  subtitle,
  onBack,
  onNext,
  nextLabel = "Continue",
  nextDisabled,
  skip,
  skipLabel = "Skip for now",
  children,
}: WizardChromeProps) {
  const headingRef = useRef<HTMLHeadingElement>(null);

  // Each step is a fresh mount (the previous one unmounts when `step`
  // changes in the parent), so a plain mount-effect focuses the new
  // step's own heading every time the wizard advances or goes back —
  // the same "move focus to what changed" rule PR #190 applied to a
  // route change, here applied to an in-place step change instead.
  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  return (
    <div className="flex flex-1 flex-col bg-clarity-bg">
      <div className="flex items-center justify-between px-5 pb-2 pt-4">
        <button
          type="button"
          onClick={onBack}
          aria-label="Go back"
          className="inline-flex size-11 items-center justify-center rounded-full text-clarity-ink-dim hover:bg-clarity-brand-soft"
        >
          <ArrowLeft className="size-5" aria-hidden="true" />
        </button>
        <div className="flex items-center gap-1.5" aria-hidden="true">
          {Array.from({ length: TOTAL_STEPS }, (_, i) => i + 1).map((n) => (
            <span
              key={n}
              className={cn(
                "h-2 rounded-full",
                n === stepNumber
                  ? "w-6 bg-clarity-brand"
                  : n < stepNumber
                    ? "w-2 bg-clarity-brand"
                    : "w-2 bg-clarity-line",
              )}
            />
          ))}
        </div>
        {skip ? (
          <button
            type="button"
            onClick={skip}
            className="inline-flex min-h-11 items-center px-1 font-clarity-sans text-xs font-bold text-clarity-ink-dim hover:text-clarity-brand"
          >
            {skipLabel}
          </button>
        ) : (
          <span className="w-11" aria-hidden="true" />
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-6 pb-4">
        <p className="font-clarity-sans text-[11px] font-bold uppercase tracking-[0.18em] text-clarity-brand">
          Step {stepNumber} of {TOTAL_STEPS}
        </p>
        {/* Focused programmatically, not via Tab, so the default browser
           outline (which would otherwise appear as a stray, off-system
           rectangle) is turned off here specifically — screen readers
           announce the heading regardless of any visible styling. */}
        <h1
          ref={headingRef}
          tabIndex={-1}
          className="mt-2 font-clarity-serif text-[26px] font-semibold leading-tight tracking-tight text-clarity-ink outline-none"
        >
          {title}
        </h1>
        {subtitle && (
          <p className="mt-2 font-clarity-sans text-sm leading-relaxed text-clarity-ink-dim">
            {subtitle}
          </p>
        )}
        <div className="mt-6 space-y-5 pb-2">{children}</div>
      </div>

      <div className="border-t border-clarity-line bg-clarity-bg px-6 py-4">
        <button
          type="button"
          onClick={onNext}
          disabled={nextDisabled}
          className="flex min-h-14 w-full items-center justify-center gap-2 rounded-clarity-md border-[1.5px] border-clarity-brand-deep bg-clarity-brand font-clarity-sans text-base font-extrabold text-clarity-brand-on shadow-clarity-banner transition-transform duration-150 ease-clarity hover:-translate-y-px disabled:pointer-events-none disabled:opacity-50 motion-reduce:transition-none motion-reduce:hover:translate-y-0"
        >
          {nextLabel}
          <ArrowRight className="size-4" aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}

/* ---------------- Welcome ---------------- */

function WelcomeStep({ onNext }: { onNext: () => void }) {
  const headingRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  return (
    <div className="flex flex-1 flex-col bg-clarity-bg">
      <div className="flex items-center justify-between px-5 pt-4">
        <span className="font-clarity-serif text-xl font-bold tracking-tight text-clarity-ink">
          Stoop<span className="text-clarity-emergency">.</span>
        </span>
        <Link
          to="/"
          className="inline-flex min-h-11 items-center px-1 font-clarity-sans text-xs font-bold text-clarity-ink-dim hover:text-clarity-brand"
        >
          Exit
        </Link>
      </div>

      <div className="flex-1 overflow-y-auto px-6 pb-4">
        <h1
          ref={headingRef}
          tabIndex={-1}
          className="mt-8 font-clarity-serif text-[30px] font-semibold leading-tight tracking-tight text-clarity-ink outline-none"
        >
          Let's get Stoop answering your tenants.
        </h1>
        <p className="mt-3 font-clarity-sans text-sm leading-relaxed text-clarity-ink-dim">
          Five short steps, about five minutes. You'll end with a live number you can text yourself
          right now.
        </p>

        <div className="mt-7">
          <p className="font-clarity-sans text-[11px] font-bold uppercase tracking-[0.14em] text-clarity-ink-dim">
            Every tenant text gets sorted into one of three buckets
          </p>
          <div className="mt-2.5 flex flex-wrap gap-2">
            <SeverityPlaque severity="emergency" size="sm" />
            <SeverityPlaque severity="urgent" size="sm" />
            <SeverityPlaque severity="routine" size="sm" />
          </div>
          <p className="mt-2.5 font-clarity-sans text-[13px] leading-relaxed text-clarity-ink-dim">
            An emergency rings your phone right away. Everything else waits for you, sorted and
            drafted.
          </p>
        </div>

        <div className="mt-7 flex items-start gap-2.5 rounded-clarity-lg border border-clarity-line-strong bg-clarity-surface p-4">
          <ShieldCheck className="mt-0.5 size-4 shrink-0 text-clarity-brand" aria-hidden="true" />
          <p className="font-clarity-sans text-[13px] leading-relaxed text-clarity-ink-dim">
            The Emergency Line is free, forever — every message read, real emergencies ring your
            phone. Setting up your voice below is part of the Full Plan:{" "}
            <span className="font-bold text-clarity-ink">$5/month early-access rate</span>, locked
            in for as long as you stay.
          </p>
        </div>
      </div>

      <div className="border-t border-clarity-line bg-clarity-bg px-6 py-4">
        <button
          type="button"
          onClick={onNext}
          className="flex min-h-14 w-full items-center justify-center gap-2 rounded-clarity-md border-[1.5px] border-clarity-brand-deep bg-clarity-brand font-clarity-sans text-base font-extrabold text-clarity-brand-on shadow-clarity-banner transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0"
        >
          Get started
          <ArrowRight className="size-4" aria-hidden="true" />
        </button>
      </div>
    </div>
  );
}

/* ---------------- Step 1: Account ---------------- */

function AccountStep({
  state,
  update,
  onBack,
  onNext,
}: {
  state: OnboardingState;
  update: (patch: Partial<OnboardingAccount>) => void;
  onBack: () => void;
  onNext: () => void;
}) {
  const [submitted, setSubmitted] = useState(false);
  const { fullName, email, phone } = state.account;

  const errName = !fullName.trim() ? "Add your name." : "";
  const errEmail = !/^\S+@\S+\.\S+$/.test(email) ? "Add a valid email." : "";
  const errPhone = phone.replace(/\D/g, "").length < 10 ? "Use a 10-digit phone number." : "";
  const valid = !errName && !errEmail && !errPhone;

  return (
    <WizardChrome
      stepNumber={1}
      title="Tell us who you are."
      subtitle="So Stoop knows who's on the other end of every text."
      onBack={onBack}
      onNext={() => {
        setSubmitted(true);
        if (valid) onNext();
      }}
      nextDisabled={submitted && !valid}
    >
      <Field label="Full name" htmlFor="ob-name" error={submitted ? errName : undefined}>
        <input
          id="ob-name"
          value={fullName}
          onChange={(e) => update({ fullName: e.target.value })}
          placeholder="Sarah Chen"
          autoComplete="name"
          className={inputClass}
        />
      </Field>

      <Field label="Email" htmlFor="ob-email" error={submitted ? errEmail : undefined}>
        <input
          id="ob-email"
          type="email"
          value={email}
          onChange={(e) => update({ email: e.target.value })}
          placeholder="sarah@example.com"
          autoComplete="email"
          className={inputClass}
        />
      </Field>

      <Field
        label="Your phone number"
        htmlFor="ob-phone"
        error={submitted ? errPhone : undefined}
        helper="This is where emergency calls ring, day or night."
      >
        <input
          id="ob-phone"
          type="tel"
          inputMode="tel"
          value={phone}
          onChange={(e) => update({ phone: e.target.value })}
          placeholder="(416) 555-0134"
          autoComplete="tel"
          className={inputClass}
        />
      </Field>
    </WizardChrome>
  );
}

/* ---------------- Step 2: Property ---------------- */

function PropertyStep({
  state,
  update,
  onBack,
  onNext,
}: {
  state: OnboardingState;
  update: (patch: Partial<OnboardingProperty>) => void;
  onBack: () => void;
  onNext: () => void;
}) {
  const [submitted, setSubmitted] = useState(false);
  const { nickname, addressLine1, city, province, postalCode } = state.property;

  const errNickname = !nickname.trim() ? "Give it a short nickname." : "";
  const errAddress = !addressLine1.trim() ? "Add the street address." : "";
  const errCity = !city.trim() ? "Add the city." : "";
  const valid = !errNickname && !errAddress && !errCity;

  return (
    <WizardChrome
      stepNumber={2}
      title="Your first property."
      subtitle="Just one to start. You can add more later from Properties."
      onBack={onBack}
      onNext={() => {
        setSubmitted(true);
        if (valid) onNext();
      }}
      nextDisabled={submitted && !valid}
    >
      <Field
        label="Property nickname"
        htmlFor="ob-nickname"
        error={submitted ? errNickname : undefined}
        helper="What you'll see in your queue."
      >
        <input
          id="ob-nickname"
          value={nickname}
          onChange={(e) => update({ nickname: e.target.value })}
          placeholder="The Palmerston Duplex"
          className={inputClass}
        />
      </Field>

      <Field label="Street address" htmlFor="ob-address" error={submitted ? errAddress : undefined}>
        <input
          id="ob-address"
          value={addressLine1}
          onChange={(e) => update({ addressLine1: e.target.value })}
          placeholder="41 Palmerston Ave"
          className={inputClass}
        />
      </Field>

      <div className="grid grid-cols-[1fr_auto] gap-3">
        <Field label="City" htmlFor="ob-city" error={submitted ? errCity : undefined}>
          <input
            id="ob-city"
            value={city}
            onChange={(e) => update({ city: e.target.value })}
            placeholder="Toronto"
            className={inputClass}
          />
        </Field>
        <Field label="Province" htmlFor="ob-province">
          <input
            id="ob-province"
            value={province}
            onChange={(e) => update({ province: e.target.value.toUpperCase().slice(0, 2) })}
            className={cn(inputClass, "w-16 text-center")}
          />
        </Field>
      </div>

      <Field label="Postal code (optional)" htmlFor="ob-postal">
        <input
          id="ob-postal"
          value={postalCode}
          onChange={(e) => update({ postalCode: e.target.value })}
          placeholder="M6G 2K2"
          className={inputClass}
        />
      </Field>
    </WizardChrome>
  );
}

/* ---------------- Step 3: Tenants ---------------- */

function TenantsStep({
  state,
  onAdd,
  onRemove,
  onUpdate,
  onBack,
  onNext,
}: {
  state: OnboardingState;
  onAdd: () => void;
  onRemove: (id: string) => void;
  onUpdate: (id: string, patch: Partial<OnboardingTenant>) => void;
  onBack: () => void;
  onNext: () => void;
}) {
  const [submitted, setSubmitted] = useState(false);

  const filledRows = state.tenants.filter((t) => t.name.trim() || t.phone.trim());
  const invalidRow = filledRows.find(
    (t) => !t.name.trim() || t.phone.replace(/\D/g, "").length < 10,
  );
  const valid = !invalidRow;

  return (
    <WizardChrome
      stepNumber={3}
      title="Who's texting in?"
      subtitle="Add the tenants at this property, or skip and add them later from Properties."
      onBack={onBack}
      onNext={() => {
        setSubmitted(true);
        if (valid) onNext();
      }}
      nextDisabled={submitted && !valid}
      skip={onNext}
    >
      <div className="space-y-4">
        {state.tenants.map((t, i) => (
          <TenantRow
            key={t.id}
            tenant={t}
            index={i}
            submitted={submitted}
            onChange={(patch) => onUpdate(t.id, patch)}
            onRemove={state.tenants.length > 1 ? () => onRemove(t.id) : undefined}
          />
        ))}
      </div>

      <button
        type="button"
        onClick={onAdd}
        className="flex min-h-11 w-full items-center justify-center gap-2 rounded-clarity-md border border-dashed border-clarity-line-strong font-clarity-sans text-sm font-bold text-clarity-brand"
      >
        <Plus className="size-4" aria-hidden="true" />
        Add another tenant
      </button>

      <MarginNote kicker="Why">
        If anything ever goes wrong here, I treat it more urgently when someone vulnerable lives in
        the unit.
      </MarginNote>
    </WizardChrome>
  );
}

function TenantRow({
  tenant,
  index,
  submitted,
  onChange,
  onRemove,
}: {
  tenant: OnboardingTenant;
  index: number;
  submitted: boolean;
  onChange: (patch: Partial<OnboardingTenant>) => void;
  onRemove?: () => void;
}) {
  const hasContent = Boolean(tenant.name.trim() || tenant.phone.trim());
  const errName = hasContent && !tenant.name.trim() ? "Add a name." : "";
  const errPhone =
    hasContent && tenant.phone.replace(/\D/g, "").length < 10 ? "Use a 10-digit phone." : "";

  return (
    <div className="rounded-clarity-lg border border-clarity-line-strong bg-clarity-surface p-4">
      <div className="flex items-center justify-between">
        <p className="font-clarity-sans text-[11px] font-bold uppercase tracking-[0.12em] text-clarity-ink-dim">
          Tenant {index + 1}
        </p>
        {onRemove && (
          <button
            type="button"
            onClick={onRemove}
            aria-label={`Remove tenant ${index + 1}`}
            className="inline-flex size-11 items-center justify-center text-clarity-ink-dim hover:text-clarity-emergency"
          >
            <Trash2 className="size-4" aria-hidden="true" />
          </button>
        )}
      </div>

      <div className="mt-2 space-y-3">
        <Field
          label="Name"
          htmlFor={`tenant-name-${tenant.id}`}
          error={submitted ? errName : undefined}
        >
          <input
            id={`tenant-name-${tenant.id}`}
            value={tenant.name}
            onChange={(e) => onChange({ name: e.target.value })}
            placeholder="Elena Petrova"
            className={inputClass}
          />
        </Field>

        <div className="grid grid-cols-2 gap-3">
          <Field
            label="Phone"
            htmlFor={`tenant-phone-${tenant.id}`}
            error={submitted ? errPhone : undefined}
          >
            <input
              id={`tenant-phone-${tenant.id}`}
              type="tel"
              inputMode="tel"
              value={tenant.phone}
              onChange={(e) => onChange({ phone: e.target.value })}
              placeholder="(416) 555-0134"
              className={inputClass}
            />
          </Field>
          <Field label="Unit (optional)" htmlFor={`tenant-unit-${tenant.id}`}>
            <input
              id={`tenant-unit-${tenant.id}`}
              value={tenant.unit}
              onChange={(e) => onChange({ unit: e.target.value })}
              placeholder="Unit 2"
              className={inputClass}
            />
          </Field>
        </div>

        <Field label="Anyone vulnerable in this unit?" htmlFor={`tenant-vuln-${tenant.id}`}>
          <select
            id={`tenant-vuln-${tenant.id}`}
            value={tenant.vulnerableOccupant}
            onChange={(e) => onChange({ vulnerableOccupant: e.target.value as VulnerableOccupant })}
            className={inputClass}
          >
            {vulnerableOptions.map((o) => (
              <option key={o.value} value={o.value}>
                {o.label}
              </option>
            ))}
          </select>
        </Field>
      </div>
    </div>
  );
}

/* ---------------- Step 4: Voice + house rules ---------------- */

function VoiceStep({
  state,
  onUpdateSample,
  onAddSample,
  onRemoveSample,
  onToneChange,
  onHouseRulesChange,
  onBack,
  onNext,
}: {
  state: OnboardingState;
  onUpdateSample: (index: number, value: string) => void;
  onAddSample: () => void;
  onRemoveSample: (index: number) => void;
  onToneChange: (tone: Tone) => void;
  onHouseRulesChange: (patch: Partial<HouseRules>) => void;
  onBack: () => void;
  onNext: () => void;
}) {
  const [submitted, setSubmitted] = useState(false);
  const activeTone = toneOptions.find((t) => t.value === state.tone);

  // Founder-vetoable ruling (see the file header): the AC's "3–5 pasted
  // real replies" only binds if the landlord engages this step at all —
  // Skip still defers voice capture entirely, since drafting is Full
  // Plan territory, not something the free Emergency Line needs.
  const filledSamples = state.voiceSamples.filter((s) => s.trim()).length;
  const errVoice = filledSamples < 3 ? "Add at least 3 real replies so drafts sound like you." : "";
  const showVoiceError = submitted && Boolean(errVoice);
  const valid = !errVoice;

  return (
    <WizardChrome
      stepNumber={4}
      title="How Stoop texts your tenants."
      subtitle="Paste a few of your real replies so drafts sound like you, then set the rules Stoop should follow."
      onBack={onBack}
      onNext={() => {
        setSubmitted(true);
        if (valid) onNext();
      }}
      nextDisabled={submitted && !valid}
      skip={onNext}
    >
      <section>
        <h2 className="font-clarity-sans text-[11px] font-bold uppercase tracking-[0.14em] text-clarity-ink-dim">
          Sound like you
        </h2>

        <div className="mt-3 space-y-3">
          {state.voiceSamples.map((sample, i) => (
            <div key={i} className="flex items-start gap-2">
              <div className="flex-1">
                <label htmlFor={`voice-sample-${i}`} className="sr-only">
                  Real reply example {i + 1}
                </label>
                <textarea
                  id={`voice-sample-${i}`}
                  value={sample}
                  onChange={(e) => onUpdateSample(i, e.target.value)}
                  placeholder={`e.g. "Hey Sam, thanks for flagging — I'll get Tony out there Thursday morning."`}
                  aria-invalid={showVoiceError || undefined}
                  aria-describedby={showVoiceError ? "voice-samples-err" : undefined}
                  className="min-h-20 w-full rounded-clarity-md border-[1.5px] border-clarity-line-strong bg-clarity-panel px-3.5 py-3 font-clarity-sans text-sm leading-relaxed text-clarity-ink placeholder:text-clarity-ink-dim/60"
                />
              </div>
              {state.voiceSamples.length > 1 && (
                <button
                  type="button"
                  onClick={() => onRemoveSample(i)}
                  aria-label={`Remove example ${i + 1}`}
                  className="mt-1 inline-flex size-11 shrink-0 items-center justify-center text-clarity-ink-dim hover:text-clarity-emergency"
                >
                  <Trash2 className="size-4" aria-hidden="true" />
                </button>
              )}
            </div>
          ))}

          {state.voiceSamples.length < 5 && (
            <button
              type="button"
              onClick={onAddSample}
              className="flex min-h-11 w-full items-center justify-center gap-2 rounded-clarity-md border border-dashed border-clarity-line-strong font-clarity-sans text-sm font-bold text-clarity-brand"
            >
              <Plus className="size-4" aria-hidden="true" />
              Add another example
            </button>
          )}

          {showVoiceError && (
            <p
              id="voice-samples-err"
              role="alert"
              className="font-clarity-sans text-xs font-semibold text-clarity-emergency"
            >
              {errVoice}
            </p>
          )}
        </div>

        <MarginNote kicker="Why" className="mt-4">
          The more real replies you give me, the less editing you'll do later.
        </MarginNote>

        <div className="mt-5">
          <Field label="How should replies sound?">
            <div role="radiogroup" aria-label="Tone" className="flex flex-wrap gap-2">
              {toneOptions.map((t) => {
                const active = state.tone === t.value;
                return (
                  <button
                    key={t.value}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    onClick={() => onToneChange(t.value)}
                    className={cn(
                      "min-h-11 rounded-clarity-md border-[1.5px] px-4 font-clarity-sans text-sm font-bold transition-colors",
                      active
                        ? "border-clarity-brand-deep bg-clarity-brand text-clarity-brand-on"
                        : "border-clarity-line-strong bg-clarity-panel text-clarity-ink hover:border-clarity-brand-border",
                    )}
                  >
                    {t.label}
                  </button>
                );
              })}
            </div>
          </Field>
          {activeTone && (
            <p className="mt-2 font-clarity-serif text-xs italic leading-relaxed text-clarity-ink-dim">
              "{activeTone.example}"
            </p>
          )}
        </div>
      </section>

      <hr className="border-clarity-line" />

      <section>
        <h2 className="font-clarity-sans text-[11px] font-bold uppercase tracking-[0.14em] text-clarity-ink-dim">
          House rules
        </h2>

        <div className="mt-3 space-y-5">
          <Field label="Pets">
            <ChipGroup
              ariaLabel="Pets"
              value={state.houseRules.pets}
              onChange={(v) => onHouseRulesChange({ pets: v })}
              options={petsOptions}
            />
          </Field>

          <Field label="Smoking">
            <ChipGroup
              ariaLabel="Smoking"
              value={state.houseRules.smoking}
              onChange={(v) => onHouseRulesChange({ smoking: v })}
              options={smokingOptions}
            />
          </Field>

          <Field label="Parking" htmlFor="ob-parking" helper="e.g. Spot 14, back lot.">
            <input
              id="ob-parking"
              value={state.houseRules.parking}
              onChange={(e) => onHouseRulesChange({ parking: e.target.value })}
              placeholder="Spot 14, back lot"
              className={inputClass}
            />
          </Field>

          <Field label="Guests">
            <ChipGroup
              ariaLabel="Guests"
              value={state.houseRules.guests}
              onChange={(v) => onHouseRulesChange({ guests: v })}
              options={guestsOptions}
            />
          </Field>

          <Field label="Quiet hours">
            <div className="flex items-center gap-3">
              <div className="flex-1">
                <label htmlFor="ob-quiet-start" className="sr-only">
                  Quiet hours start
                </label>
                <input
                  id="ob-quiet-start"
                  type="time"
                  value={state.houseRules.quietStart}
                  onChange={(e) => onHouseRulesChange({ quietStart: e.target.value })}
                  className={inputClass}
                />
              </div>
              <span className="font-clarity-sans text-sm font-semibold text-clarity-ink-dim">
                to
              </span>
              <div className="flex-1">
                <label htmlFor="ob-quiet-end" className="sr-only">
                  Quiet hours end
                </label>
                <input
                  id="ob-quiet-end"
                  type="time"
                  value={state.houseRules.quietEnd}
                  onChange={(e) => onHouseRulesChange({ quietEnd: e.target.value })}
                  className={inputClass}
                />
              </div>
            </div>
          </Field>

          <Field
            label="Heating season"
            helper="When you're required to provide heat — helps me flag a no-heat report correctly."
          >
            <div className="flex items-center gap-3">
              <div className="flex-1">
                <label htmlFor="ob-heat-start" className="sr-only">
                  Heating season start
                </label>
                <select
                  id="ob-heat-start"
                  value={state.houseRules.heatingSeasonStart}
                  onChange={(e) => onHouseRulesChange({ heatingSeasonStart: e.target.value })}
                  className={inputClass}
                >
                  {heatingSeasonStartOptions.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
              <span className="font-clarity-sans text-sm font-semibold text-clarity-ink-dim">
                to
              </span>
              <div className="flex-1">
                <label htmlFor="ob-heat-end" className="sr-only">
                  Heating season end
                </label>
                <select
                  id="ob-heat-end"
                  value={state.houseRules.heatingSeasonEnd}
                  onChange={(e) => onHouseRulesChange({ heatingSeasonEnd: e.target.value })}
                  className={inputClass}
                >
                  {heatingSeasonEndOptions.map((o) => (
                    <option key={o.value} value={o.value}>
                      {o.label}
                    </option>
                  ))}
                </select>
              </div>
            </div>
          </Field>
        </div>
      </section>
    </WizardChrome>
  );
}

/* ---------------- Step 5: Backup contact & emergency basics ---------------- */

function BackupStep({
  state,
  onChange,
  onBack,
  onNext,
}: {
  state: OnboardingState;
  onChange: (patch: Partial<BackupContact>) => void;
  onBack: () => void;
  onNext: () => void;
}) {
  const backupName = state.backupContact.name.trim() || "your backup contact";

  return (
    <WizardChrome
      stepNumber={5}
      title="Who do we call if you don't pick up?"
      subtitle="Strongly encouraged, not required — a partner, super, or trusted neighbor."
      onBack={onBack}
      onNext={onNext}
      skip={onNext}
    >
      <Field label="Their name (optional)" htmlFor="ob-backup-name">
        <input
          id="ob-backup-name"
          value={state.backupContact.name}
          onChange={(e) => onChange({ name: e.target.value })}
          placeholder="Jordan (super)"
          className={inputClass}
        />
      </Field>

      <Field label="Their phone (optional)" htmlFor="ob-backup-phone">
        <input
          id="ob-backup-phone"
          type="tel"
          inputMode="tel"
          value={state.backupContact.phone}
          onChange={(e) => onChange({ phone: e.target.value })}
          placeholder="(416) 555-0177"
          className={inputClass}
        />
      </Field>

      <div className="rounded-clarity-lg border border-clarity-line-strong bg-clarity-surface p-4">
        <div className="flex items-center gap-2">
          <SeverityPlaque severity="emergency" size="sm" />
          <span className="font-clarity-sans text-[11px] font-bold uppercase tracking-[0.1em] text-clarity-ink-dim">
            Always free
          </span>
        </div>
        <p className="mt-3 font-clarity-sans text-[13px] leading-relaxed text-clarity-ink-dim">
          When a tenant sends something like this, I call your phone right away — free, no matter
          your plan. If you don't answer, I call again, then call {backupName} ten minutes later.
          Nobody has to wait alone.
        </p>
      </div>

      <MarginNote kicker="Why">
        Real emergencies can't wait for you to wake up. A backup contact means someone always
        answers.
      </MarginNote>
    </WizardChrome>
  );
}

/* ---------------- Done ---------------- */

type DonePhase = "provisioning" | "ready" | "testing" | "sent";

function DoneStep({
  state,
  onDisclosureSentChange,
  onBack,
}: {
  state: OnboardingState;
  onDisclosureSentChange: (v: boolean) => void;
  onBack: () => void;
}) {
  const [phase, setPhase] = useState<DonePhase>("provisioning");
  const headingRef = useRef<HTMLHeadingElement>(null);
  const nickname = state.property.nickname.trim() || "Your property";

  useEffect(() => {
    headingRef.current?.focus();
  }, []);

  useEffect(() => {
    const t = setTimeout(() => setPhase("ready"), prefersReducedMotion() ? 0 : 1600);
    return () => clearTimeout(t);
  }, []);

  useEffect(() => {
    if (phase !== "testing") return;
    const t = setTimeout(() => setPhase("sent"), prefersReducedMotion() ? 0 : 1200);
    return () => clearTimeout(t);
  }, [phase]);

  const disclosureMessage = buildDisclosureMessage(state.account.fullName, nickname);
  const testReply = buildTestTextReply(nickname);

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(disclosureMessage);
      toast.success("Copied. Send it to your tenants.");
    } catch {
      toast.error("Couldn't copy. Select the message text instead.");
    }
  }

  return (
    <div className="flex flex-1 flex-col bg-clarity-bg">
      <div className="flex items-center justify-between px-5 pb-2 pt-4">
        <button
          type="button"
          onClick={onBack}
          aria-label="Go back"
          className="inline-flex size-11 items-center justify-center rounded-full text-clarity-ink-dim hover:bg-clarity-brand-soft"
        >
          <ArrowLeft className="size-5" aria-hidden="true" />
        </button>
        <div className="flex items-center gap-1.5" aria-hidden="true">
          {Array.from({ length: TOTAL_STEPS }, (_, i) => i + 1).map((n) => (
            <span key={n} className="h-2 w-2 rounded-full bg-clarity-brand" />
          ))}
        </div>
        <span className="w-11" aria-hidden="true" />
      </div>

      <div className="flex-1 overflow-y-auto px-6 pb-4" aria-live="polite">
        {phase === "provisioning" && (
          <div className="flex flex-col items-center justify-center px-2 py-16 text-center">
            <div className="relative">
              <div
                className="absolute inset-0 animate-ping rounded-full bg-clarity-brand/20 motion-reduce:animate-none"
                aria-hidden="true"
              />
              <div className="relative flex size-20 items-center justify-center rounded-full bg-clarity-brand-soft">
                <Phone className="size-8 text-clarity-brand" aria-hidden="true" />
              </div>
            </div>
            <h1
              ref={headingRef}
              tabIndex={-1}
              className="mt-8 font-clarity-serif text-xl font-semibold text-clarity-ink outline-none"
            >
              Reserving your number…
            </h1>
            <p className="mt-2 inline-flex items-center gap-2 font-clarity-sans text-sm text-clarity-ink-dim">
              <Loader2
                className="size-3.5 animate-spin motion-reduce:animate-none"
                aria-hidden="true"
              />
              This takes a second.
            </p>
          </div>
        )}

        {phase !== "provisioning" && (
          <>
            <p className="font-clarity-sans text-[11px] font-bold uppercase tracking-[0.18em] text-clarity-brand">
              You're set.
            </p>
            <h1
              ref={headingRef}
              tabIndex={-1}
              className="mt-2 font-clarity-serif text-[26px] font-semibold leading-tight tracking-tight text-clarity-ink outline-none"
            >
              {nickname} has its own number.
            </h1>

            <div className="clarity-ticket relative mt-6 rounded-clarity-md border border-clarity-brand-border bg-clarity-brand-soft px-5 pb-5 pt-6 text-center">
              <p className="font-clarity-sans text-[11px] font-bold uppercase tracking-[0.14em] text-clarity-ink-dim">
                Dedicated SMS number
              </p>
              <p className="mt-2 font-clarity-serif text-[28px] font-bold tracking-tight text-clarity-ink">
                {MOCK_PROVISIONED_NUMBER}
              </p>
              <p className="mt-1 font-clarity-sans text-xs text-clarity-ink-dim">
                Live now. Owned by you.
              </p>
            </div>

            <div className="mt-6 rounded-clarity-lg border border-clarity-line-strong bg-clarity-surface p-4">
              <p className="font-clarity-sans text-[11px] font-bold uppercase tracking-[0.12em] text-clarity-ink-dim">
                Tell your tenants
              </p>
              <p className="mt-2.5 font-clarity-sans text-sm leading-relaxed text-clarity-ink">
                {disclosureMessage}
              </p>
              <button
                type="button"
                onClick={handleCopy}
                className="mt-3 inline-flex min-h-11 items-center gap-2 rounded-clarity-md border-[1.5px] border-clarity-line-strong bg-clarity-panel px-4 font-clarity-sans text-sm font-bold text-clarity-ink-dim hover:border-clarity-brand-border"
              >
                <Copy className="size-4" aria-hidden="true" />
                Copy message
              </button>

              <label className="mt-4 flex min-h-11 cursor-pointer items-start gap-2.5">
                <input
                  type="checkbox"
                  checked={state.disclosureSent}
                  onChange={(e) => onDisclosureSentChange(e.target.checked)}
                  className="mt-1 size-4 shrink-0 accent-[var(--clarity-brand)]"
                />
                <span className="font-clarity-sans text-sm text-clarity-ink">
                  I've sent this to my tenants
                </span>
              </label>
            </div>

            {phase === "ready" && (
              <button
                type="button"
                onClick={() => setPhase("testing")}
                className="mt-6 flex min-h-14 w-full items-center justify-center gap-2 rounded-clarity-md border-[1.5px] border-clarity-brand-deep bg-clarity-brand font-clarity-sans text-base font-extrabold text-clarity-brand-on shadow-clarity-banner transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0"
              >
                Send myself a test text
              </button>
            )}

            {phase === "testing" && (
              <div className="mt-6 flex flex-col items-center justify-center gap-2 py-6 text-center">
                <Loader2
                  className="size-6 animate-spin text-clarity-brand motion-reduce:animate-none"
                  aria-hidden="true"
                />
                <p className="font-clarity-sans text-sm font-semibold text-clarity-ink-dim">
                  Texting your phone…
                </p>
              </div>
            )}

            {phase === "sent" && (
              <>
                <p className="mt-6 font-clarity-sans text-[11px] font-bold uppercase tracking-[0.14em] text-clarity-ink-dim">
                  Round-trip confirmed
                </p>
                <div className="mt-3 space-y-3">
                  <div className="ml-auto max-w-[85%]">
                    <div className="rounded-clarity-lg rounded-tr-clarity-sm border border-clarity-line-strong bg-clarity-panel px-[15px] py-[13px] font-clarity-sans text-[15px] leading-relaxed text-clarity-ink">
                      {testTextInbound}
                    </div>
                    <p className="mt-1.5 text-right font-clarity-sans text-[11px] font-semibold text-clarity-ink-dim">
                      You (test text) · just now
                    </p>
                  </div>
                  <div className="max-w-[85%]">
                    <div className="rounded-clarity-lg rounded-tl-clarity-sm bg-clarity-brand px-[15px] py-[13px] font-clarity-serif text-[15px] italic leading-relaxed text-clarity-brand-on">
                      {testReply}
                    </div>
                    <p className="mt-1.5 font-clarity-sans text-[11px] font-semibold text-clarity-brand">
                      Stoop · just now
                    </p>
                  </div>
                </div>

                <Link
                  to="/app"
                  className="mt-7 flex min-h-14 w-full items-center justify-center gap-2 rounded-clarity-md border-[1.5px] border-clarity-brand-deep bg-clarity-brand font-clarity-sans text-base font-extrabold text-clarity-brand-on shadow-clarity-banner"
                >
                  Go to your dashboard
                  <ArrowRight className="size-4" aria-hidden="true" />
                </Link>

                <p className="mt-4 pb-2 text-center font-clarity-sans text-xs leading-relaxed text-clarity-ink-dim">
                  Emergency texts reach you free, no matter what.{" "}
                  <Link
                    to="/plans"
                    className="font-bold text-clarity-brand underline-offset-2 hover:underline"
                  >
                    See plans &amp; pricing
                  </Link>
                  .
                </p>
              </>
            )}
          </>
        )}
      </div>
    </div>
  );
}
