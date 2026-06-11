import { createFileRoute, Link } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import {
  ArrowLeft,
  ArrowRight,
  Apple,
  Mail,
  Chrome,
  Check,
  Loader2,
  Copy,
  Phone,
  MessageSquareText,
  ShieldCheck,
} from "lucide-react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Wordmark } from "@/components/stoop/Wordmark";
import { cn } from "@/lib/utils";

export const Route = createFileRoute("/onboarding")({
  head: () => ({
    meta: [
      { title: "Get set up — Stoop." },
      { name: "robots", content: "noindex" },
      {
        name: "description",
        content: "Set up your first property on Stoop. Five short screens, under five minutes.",
      },
    ],
  }),
  component: OnboardingPage,
});

/* ---------------- Phone frame wrapper ---------------- */

function PhoneFrame({ children }: { children: React.ReactNode }) {
  return (
    <div className="min-h-screen bg-surface px-4 py-6 md:py-12">
      <div className="mx-auto w-full max-w-[375px]">
        <div className="overflow-hidden rounded-[2.25rem] border border-border bg-canvas shadow-2xl md:rounded-[2.5rem]">
          <div className="flex min-h-[760px] flex-col">{children}</div>
        </div>
        <p className="mt-3 text-center text-[11px] font-medium text-ink-muted md:hidden">
          Stoop. is a mobile app
        </p>
      </div>
    </div>
  );
}

/* ---------------- State + persistence ---------------- */

type Screen = "signup" | "step-1" | "step-2" | "step-3" | "step-4" | "step-5";

interface FormState {
  address: string;
  nickname: string;
  units: number;
  propertyType: string;
  rent: string;
  rentDay: string;
  deposit: string;
  leaseEnd: string;
  monthToMonth: boolean;
  pets: string;
  smoking: string;
  parking: string;
  quietStart: string;
  quietEnd: string;
  guests: string;
  vendorType: string;
  vendorName: string;
  vendorPhone: string;
  vendorAfterHours: boolean;
}

const DEFAULT_STATE: FormState = {
  address: "",
  nickname: "",
  units: 1,
  propertyType: "Basement apt",
  rent: "",
  rentDay: "1",
  deposit: "",
  leaseEnd: "",
  monthToMonth: false,
  pets: "Cats only",
  smoking: "No",
  parking: "",
  quietStart: "22:00",
  quietEnd: "07:00",
  guests: "Overnight only with notice",
  vendorType: "Plumber",
  vendorName: "",
  vendorPhone: "",
  vendorAfterHours: false,
};

const STORAGE_KEY = "stoop.onboarding.v1";

function useFormState() {
  const [state, setState] = useState<FormState>(DEFAULT_STATE);
  const [hydrated, setHydrated] = useState(false);

  useEffect(() => {
    try {
      const raw = localStorage.getItem(STORAGE_KEY);
      if (raw) setState({ ...DEFAULT_STATE, ...JSON.parse(raw) });
    } catch {
      /* ignore */
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

/* ---------------- Page ---------------- */

function OnboardingPage() {
  const [screen, setScreen] = useState<Screen>("signup");
  const [form, setForm] = useFormState();
  const update = <K extends keyof FormState>(k: K, v: FormState[K]) =>
    setForm((s) => ({ ...s, [k]: v }));

  return (
    <PhoneFrame>
      {screen === "signup" && <SignUpScreen onContinue={() => setScreen("step-1")} />}
      {screen === "step-1" && (
        <Step1
          form={form}
          update={update}
          onBack={() => setScreen("signup")}
          onNext={() => setScreen("step-2")}
        />
      )}
      {screen === "step-2" && (
        <Step2
          form={form}
          update={update}
          onBack={() => setScreen("step-1")}
          onNext={() => setScreen("step-3")}
        />
      )}
      {screen === "step-3" && (
        <Step3
          form={form}
          update={update}
          onBack={() => setScreen("step-2")}
          onNext={() => setScreen("step-4")}
        />
      )}
      {screen === "step-4" && (
        <Step4
          form={form}
          update={update}
          onBack={() => setScreen("step-3")}
          onNext={() => setScreen("step-5")}
        />
      )}
      {screen === "step-5" && <Step5 form={form} onBack={() => setScreen("step-4")} />}
    </PhoneFrame>
  );
}

/* ---------------- Sign up ---------------- */

function SignUpScreen({ onContinue }: { onContinue: () => void }) {
  return (
    <div className="flex flex-1 flex-col px-6 py-8">
      <div className="flex items-center justify-between">
        <Wordmark size="sm" />
        <Link to="/" className="text-xs font-semibold text-ink-muted hover:text-brand">
          Exit
        </Link>
      </div>

      <div className="mt-12">
        <h1 className="font-display text-4xl font-bold leading-tight tracking-tight">
          Let's get your first property on board.
        </h1>
        <p className="mt-3 text-sm leading-relaxed text-ink-muted">
          Five short screens. Under five minutes. You'll have a working tenant number at the end.
        </p>
      </div>

      <div className="mt-10 space-y-3">
        <button
          onClick={onContinue}
          className="flex h-14 w-full items-center justify-center gap-2 rounded-2xl bg-ink text-base font-bold text-canvas hover:bg-ink/90"
        >
          <Apple className="size-5" aria-hidden="true" />
          Sign in with Apple
        </button>
        <button
          onClick={onContinue}
          className="flex h-14 w-full items-center justify-center gap-2 rounded-2xl border border-border bg-card text-base font-bold text-ink hover:bg-brand-muted"
        >
          <Chrome className="size-5" aria-hidden="true" />
          Sign in with Google
        </button>
        <button
          onClick={onContinue}
          className="flex h-14 w-full items-center justify-center gap-2 rounded-2xl border border-border bg-card text-base font-bold text-ink hover:bg-brand-muted"
        >
          <Mail className="size-5" aria-hidden="true" />
          Email and password
        </button>
      </div>

      <p className="mt-6 text-center text-xs text-ink-muted">
        Already have an account?{" "}
        <a href="#signin" className="font-semibold text-brand underline-offset-4 hover:underline">
          Sign in
        </a>
      </p>

      <div className="mt-auto pt-10">
        <div className="flex items-start gap-2 rounded-2xl border border-border bg-brand-muted/60 p-4">
          <ShieldCheck className="mt-0.5 size-4 shrink-0 text-brand" aria-hidden="true" />
          <p className="text-xs leading-relaxed text-ink">
            We never message your tenants without your approval. You stay in control.
          </p>
        </div>
      </div>
    </div>
  );
}

/* ---------------- Step chrome ---------------- */

interface StepChromeProps {
  step: number;
  title: string;
  subtitle?: string;
  onBack: () => void;
  onNext: () => void;
  nextLabel?: string;
  nextDisabled?: boolean;
  children: React.ReactNode;
  skip?: () => void;
}

function StepChrome({
  step,
  title,
  subtitle,
  onBack,
  onNext,
  nextLabel = "Continue",
  nextDisabled,
  children,
  skip,
}: StepChromeProps) {
  return (
    <div className="flex flex-1 flex-col">
      <div className="flex items-center justify-between px-5 py-4">
        <button
          onClick={onBack}
          aria-label="Go back"
          className="inline-flex size-10 items-center justify-center rounded-full hover:bg-brand-muted"
        >
          <ArrowLeft className="size-5" aria-hidden="true" />
        </button>
        <div className="flex items-center gap-1.5">
          {[1, 2, 3, 4, 5].map((n) => (
            <span
              key={n}
              className={cn(
                "h-2 rounded-full transition-all",
                n === step ? "w-6 bg-brand" : n < step ? "w-2 bg-brand" : "w-2 bg-border",
              )}
              aria-current={n === step ? "step" : undefined}
            />
          ))}
        </div>
        {skip ? (
          <button onClick={skip} className="text-xs font-semibold text-ink-muted hover:text-brand">
            Skip
          </button>
        ) : (
          <span className="w-10" />
        )}
      </div>

      <div className="flex-1 overflow-y-auto px-6 pb-4">
        <p className="text-[11px] font-bold uppercase tracking-[0.18em] text-brand">
          Step {step} of 5
        </p>
        <h1 className="mt-2 font-display text-[28px] font-bold leading-tight tracking-tight">
          {title}
        </h1>
        {subtitle && <p className="mt-2 text-sm text-ink-muted">{subtitle}</p>}
        <div className="mt-7 space-y-5">{children}</div>
      </div>

      <div className="border-t border-border bg-canvas px-6 py-4">
        <Button
          onClick={onNext}
          disabled={nextDisabled}
          className="h-14 w-full text-base font-bold"
        >
          {nextLabel}
          <ArrowRight className="size-4" aria-hidden="true" />
        </Button>
      </div>
    </div>
  );
}

/* ---------------- Field helpers ---------------- */

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
  return (
    <div className="space-y-1.5">
      <Label htmlFor={htmlFor} className="text-sm font-semibold text-ink">
        {label}
      </Label>
      {children}
      {error ? (
        <p className="text-xs font-medium text-emergency">{error}</p>
      ) : helper ? (
        <p className="text-xs text-ink-muted">{helper}</p>
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
              "min-h-10 rounded-full border px-4 text-sm font-semibold transition-colors",
              active
                ? "border-brand bg-brand text-brand-foreground"
                : "border-border bg-card text-ink hover:border-brand/50",
            )}
          >
            {o}
          </button>
        );
      })}
    </div>
  );
}

/* ---------------- Steps ---------------- */

interface StepProps {
  form: FormState;
  update: <K extends keyof FormState>(k: K, v: FormState[K]) => void;
  onBack: () => void;
  onNext: () => void;
}

function Step1({ form, update, onBack, onNext }: StepProps) {
  const [submitted, setSubmitted] = useState(false);
  const errAddr = !form.address.trim() ? "Add the street address." : "";
  const errNick = !form.nickname.trim() ? "Give it a short nickname." : "";
  const valid = !errAddr && !errNick && form.units >= 1;

  return (
    <StepChrome
      step={1}
      title="What's the address?"
      subtitle="Just one property to start. You can add more later."
      onBack={onBack}
      onNext={() => {
        setSubmitted(true);
        if (valid) onNext();
      }}
      nextDisabled={submitted && !valid}
    >
      <Field
        label="Street address"
        htmlFor="addr"
        error={submitted ? errAddr : undefined}
        helper="Autocomplete coming online — type it in for now."
      >
        <Input
          id="addr"
          value={form.address}
          onChange={(e) => update("address", e.target.value)}
          placeholder="123 Main St #4, Oakville ON"
          className="h-12 text-base"
        />
      </Field>

      <Field
        label="Property nickname"
        htmlFor="nick"
        error={submitted ? errNick : undefined}
        helper="What you'll see in your queue."
      >
        <Input
          id="nick"
          value={form.nickname}
          onChange={(e) => update("nickname", e.target.value)}
          placeholder="The Walmer Basement"
          className="h-12 text-base"
        />
      </Field>

      <Field label="Units at this address" htmlFor="units">
        <Input
          id="units"
          type="number"
          min={1}
          value={form.units}
          onChange={(e) => update("units", Math.max(1, Number(e.target.value) || 1))}
          className="h-12 text-base"
        />
      </Field>

      <Field label="Property type">
        <Select value={form.propertyType} onValueChange={(v) => update("propertyType", v)}>
          <SelectTrigger className="h-12 text-base">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {["Detached", "Semi", "Townhouse", "Condo", "Basement apt", "Duplex", "Other"].map(
              (t) => (
                <SelectItem key={t} value={t}>
                  {t}
                </SelectItem>
              ),
            )}
          </SelectContent>
        </Select>
      </Field>
    </StepChrome>
  );
}

function Step2({ form, update, onBack, onNext }: StepProps) {
  const [submitted, setSubmitted] = useState(false);
  const errRent = !form.rent.trim() ? "Add the monthly rent." : "";
  const day = Number(form.rentDay);
  const errDay = !day || day < 1 || day > 31 ? "Pick a day from 1–31." : "";
  const valid = !errRent && !errDay;

  return (
    <StepChrome
      step={2}
      title="Lease basics."
      subtitle="The agent uses this to answer rent questions accurately."
      onBack={onBack}
      onNext={() => {
        setSubmitted(true);
        if (valid) onNext();
      }}
      nextDisabled={submitted && !valid}
    >
      <Field label="Monthly rent (CAD)" htmlFor="rent" error={submitted ? errRent : undefined}>
        <div className="relative">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-base font-semibold text-ink-muted">
            $
          </span>
          <Input
            id="rent"
            inputMode="numeric"
            value={form.rent}
            onChange={(e) => update("rent", e.target.value.replace(/[^\d.]/g, ""))}
            placeholder="1,950"
            className="h-12 pl-7 text-base"
          />
        </div>
      </Field>

      <Field
        label="Rent due day"
        htmlFor="day"
        error={submitted ? errDay : undefined}
        helper="Day of the month."
      >
        <Input
          id="day"
          type="number"
          min={1}
          max={31}
          value={form.rentDay}
          onChange={(e) => update("rentDay", e.target.value)}
          className="h-12 text-base"
        />
      </Field>

      <Field label="Security deposit (optional)" htmlFor="dep">
        <div className="relative">
          <span className="absolute left-3 top-1/2 -translate-y-1/2 text-base font-semibold text-ink-muted">
            $
          </span>
          <Input
            id="dep"
            inputMode="numeric"
            value={form.deposit}
            onChange={(e) => update("deposit", e.target.value.replace(/[^\d.]/g, ""))}
            placeholder="1,950"
            className="h-12 pl-7 text-base"
          />
        </div>
      </Field>

      <div className="rounded-2xl border border-border bg-card p-4">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-sm font-semibold text-ink">Month-to-month</p>
            <p className="text-xs text-ink-muted">No fixed end date.</p>
          </div>
          <Switch checked={form.monthToMonth} onCheckedChange={(v) => update("monthToMonth", v)} />
        </div>
        {!form.monthToMonth && (
          <div className="mt-4">
            <Label htmlFor="end" className="text-sm font-semibold text-ink">
              Lease end date (optional)
            </Label>
            <Input
              id="end"
              type="date"
              value={form.leaseEnd}
              onChange={(e) => update("leaseEnd", e.target.value)}
              className="mt-1.5 h-12 text-base"
            />
          </div>
        )}
      </div>
    </StepChrome>
  );
}

function Step3({ form, update, onBack, onNext }: StepProps) {
  return (
    <StepChrome
      step={3}
      title="House rules."
      subtitle="The agent answers tenant questions using these."
      onBack={onBack}
      onNext={onNext}
    >
      <Field label="Pets">
        <ChipGroup
          ariaLabel="Pets"
          value={form.pets}
          onChange={(v) => update("pets", v)}
          options={["No pets", "Cats only", "Cats & dogs", "With deposit"]}
        />
      </Field>

      <Field label="Smoking">
        <ChipGroup
          ariaLabel="Smoking"
          value={form.smoking}
          onChange={(v) => update("smoking", v)}
          options={["No", "Outside only", "Allowed"]}
        />
      </Field>

      <Field label="Parking" htmlFor="parking" helper="e.g. Spot 14, back lot.">
        <Input
          id="parking"
          value={form.parking}
          onChange={(e) => update("parking", e.target.value)}
          placeholder="Spot 14, back lot"
          className="h-12 text-base"
        />
      </Field>

      <Field label="Quiet hours">
        <div className="flex items-center gap-3">
          <Input
            type="time"
            value={form.quietStart}
            onChange={(e) => update("quietStart", e.target.value)}
            className="h-12 text-base"
            aria-label="Quiet hours start"
          />
          <span className="text-sm font-medium text-ink-muted">to</span>
          <Input
            type="time"
            value={form.quietEnd}
            onChange={(e) => update("quietEnd", e.target.value)}
            className="h-12 text-base"
            aria-label="Quiet hours end"
          />
        </div>
      </Field>

      <Field label="Guests">
        <ChipGroup
          ariaLabel="Guests"
          value={form.guests}
          onChange={(v) => update("guests", v)}
          options={["No restriction", "Overnight only with notice", "Other"]}
        />
      </Field>
    </StepChrome>
  );
}

function Step4({ form, update, onBack, onNext }: StepProps) {
  const [submitted, setSubmitted] = useState(false);
  const errName = !form.vendorName.trim() ? "Vendor name needed." : "";
  const phoneDigits = form.vendorPhone.replace(/\D/g, "");
  const errPhone = form.vendorPhone && phoneDigits.length < 10 ? "Use a 10-digit phone." : "";
  const valid = !errName && !errPhone && form.vendorPhone.trim();

  return (
    <StepChrome
      step={4}
      title="Got a plumber?"
      subtitle="The agent suggests them on leaks, clogs, and burst pipes."
      onBack={onBack}
      onNext={() => {
        setSubmitted(true);
        if (valid) onNext();
      }}
      nextDisabled={submitted && !valid}
      skip={onNext}
    >
      <Field label="Vendor type">
        <Select value={form.vendorType} onValueChange={(v) => update("vendorType", v)}>
          <SelectTrigger className="h-12 text-base">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {["Plumber", "Electrician", "HVAC", "Locksmith", "Handyman", "Other"].map((t) => (
              <SelectItem key={t} value={t}>
                {t}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </Field>

      <Field label="Vendor name" htmlFor="vname" error={submitted ? errName : undefined}>
        <Input
          id="vname"
          value={form.vendorName}
          onChange={(e) => update("vendorName", e.target.value)}
          placeholder="Mike's Plumbing"
          className="h-12 text-base"
        />
      </Field>

      <Field
        label="Vendor phone"
        htmlFor="vphone"
        error={
          submitted ? errPhone || (!form.vendorPhone.trim() ? "Phone needed." : "") : undefined
        }
      >
        <Input
          id="vphone"
          inputMode="tel"
          value={form.vendorPhone}
          onChange={(e) => update("vendorPhone", e.target.value)}
          placeholder="(905) 555-0142"
          className="h-12 text-base"
        />
      </Field>

      <div className="flex items-center justify-between rounded-2xl border border-border bg-card p-4">
        <div className="pr-4">
          <p className="text-sm font-semibold text-ink">After-hours available</p>
          <p className="text-xs text-ink-muted">Reachable nights and weekends.</p>
        </div>
        <Switch
          checked={form.vendorAfterHours}
          onCheckedChange={(v) => update("vendorAfterHours", v)}
        />
      </div>

      <p className="text-center text-xs text-ink-muted">
        You can add electricians, HVAC, locksmiths, and more after setup.
      </p>
    </StepChrome>
  );
}

function Step5({ form, onBack }: { form: FormState; onBack: () => void }) {
  const [provisioning, setProvisioning] = useState(true);
  const number = useMemo(() => "(437) 555-0181", []);

  useEffect(() => {
    const t = setTimeout(() => setProvisioning(false), 2000);
    return () => clearTimeout(t);
  }, []);

  const message = `Hey — for maintenance issues or questions, text me at ${number}. I'll get back to you quickly.`;
  const nickname = form.nickname || "your property";

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(message);
      toast.success("Copied. Send it to your tenant.");
    } catch {
      toast.error("Couldn't copy. Long-press the message instead.");
    }
  };

  return (
    <div className="flex flex-1 flex-col">
      <div className="flex items-center justify-between px-5 py-4">
        <button
          onClick={onBack}
          aria-label="Go back"
          className="inline-flex size-10 items-center justify-center rounded-full hover:bg-brand-muted"
        >
          <ArrowLeft className="size-5" aria-hidden="true" />
        </button>
        <div className="flex items-center gap-1.5">
          {[1, 2, 3, 4, 5].map((n) => (
            <span
              key={n}
              className={cn("h-2 rounded-full", n === 5 ? "w-6 bg-brand" : "w-2 bg-brand")}
            />
          ))}
        </div>
        <span className="w-10" />
      </div>

      <div className="flex-1 overflow-y-auto px-6 pb-4">
        {provisioning ? (
          <div className="flex flex-col items-center justify-center py-20 text-center">
            <div className="relative">
              <div className="absolute inset-0 animate-ping rounded-full bg-brand/20" />
              <div className="relative flex size-20 items-center justify-center rounded-full bg-brand-muted">
                <Phone className="size-8 text-brand" aria-hidden="true" />
              </div>
            </div>
            <p className="mt-8 font-display text-xl font-bold tracking-tight">
              Reserving your number…
            </p>
            <p className="mt-2 inline-flex items-center gap-2 text-sm text-ink-muted">
              <Loader2 className="size-3.5 animate-spin" aria-hidden="true" />
              This takes a second.
            </p>
          </div>
        ) : (
          <>
            <p className="text-[11px] font-bold uppercase tracking-[0.18em] text-brand">
              You're set.
            </p>
            <h1 className="mt-2 font-display text-[28px] font-bold leading-tight tracking-tight">
              {nickname} has its own number.
            </h1>

            <div className="mt-6 rounded-3xl border border-brand/30 bg-brand-muted/60 p-6 text-center">
              <p className="text-[11px] font-bold uppercase tracking-widest text-ink-muted">
                Dedicated SMS
              </p>
              <p className="mt-2 font-display text-3xl font-bold tracking-tight text-ink">
                {number}
              </p>
              <p className="mt-1 text-xs text-ink-muted">Live now. Owned by you.</p>
            </div>

            <div className="mt-6 rounded-2xl border border-border bg-card p-4">
              <div className="flex items-center gap-2 text-xs font-bold uppercase tracking-widest text-ink-muted">
                <MessageSquareText className="size-3.5" aria-hidden="true" />
                Share with your tenant
              </div>
              <p className="mt-3 text-sm leading-relaxed text-ink">{message}</p>
            </div>

            <div className="mt-6 space-y-2">
              <Button onClick={copy} className="h-14 w-full text-base font-bold">
                <Copy className="size-4" aria-hidden="true" />
                Share with my tenant
              </Button>
              <button
                onClick={() => toast.success("Test text sent — open your phone.")}
                className="flex h-12 w-full items-center justify-center rounded-2xl border border-border bg-card text-sm font-bold text-ink hover:bg-brand-muted"
              >
                Send myself a test text
              </button>
            </div>

            <div className="mt-6 flex items-start gap-2 rounded-2xl border border-border bg-surface/60 p-4">
              <ShieldCheck className="mt-0.5 size-4 shrink-0 text-brand" aria-hidden="true" />
              <p className="text-xs leading-relaxed text-ink-muted">
                You're in <span className="font-semibold text-ink">Shadow Mode</span>. The agent
                will draft every reply and wait for your approval — until you choose to graduate it.
              </p>
            </div>
          </>
        )}
      </div>
    </div>
  );
}
