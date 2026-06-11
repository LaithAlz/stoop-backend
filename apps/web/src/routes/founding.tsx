import { createFileRoute } from "@tanstack/react-router";
import { createServerFn } from "@tanstack/react-start";
import { useEffect, useRef, useState } from "react";
import { z } from "zod";

/* ──────────────────────────────────────────────────────────────────
 * Founding-cohort waitlist page (issue #114).
 * Brownstone design, scoped under .fnd so it can't clash with the
 * Heritage-utility tokens; the full design-system port is #112.
 * ────────────────────────────────────────────────────────────────── */

const waitlistSchema = z.object({
  name: z.string().trim().min(1).max(120),
  email: z.string().trim().email().max(254),
  city: z.string().trim().max(120).optional().default(""),
  doors: z.coerce.number().int().min(1).max(10000).optional(),
  isPm: z.boolean().optional().default(false),
  website: z.string().max(0).optional().default(""), // honeypot — must stay empty
});

type D1Like = {
  prepare(q: string): {
    bind(...v: unknown[]): { run(): Promise<unknown> };
  };
};

async function getWaitlistDb(): Promise<D1Like | null> {
  try {
    const mod = (await import("cloudflare:workers")) as {
      env?: Record<string, unknown>;
    };
    return (mod.env?.WAITLIST_DB as D1Like | undefined) ?? null;
  } catch {
    return null;
  }
}

const joinWaitlist = createServerFn({ method: "POST" }).handler(
  async (ctx): Promise<{ ok: boolean; reason?: string }> => {
    const parsed = waitlistSchema.safeParse(ctx.data);
    if (!parsed.success) return { ok: false, reason: "invalid" };
    if (parsed.data.website !== "") return { ok: true }; // honeypot: pretend success
    const db = await getWaitlistDb();
    if (!db) return { ok: false, reason: "not-configured" };
    const { name, email, city, doors, isPm } = parsed.data;
    await db
      .prepare(
        `INSERT INTO waitlist (name, email, city, doors, is_pm, source)
         VALUES (?, ?, ?, ?, ?, 'founding-page')
         ON CONFLICT(email) DO NOTHING`,
      )
      .bind(name, email.toLowerCase(), city, doors ?? null, isPm ? 1 : 0)
      .run();
    return { ok: true };
  },
);

export const Route = createFileRoute("/founding")({
  head: () => ({
    meta: [
      { title: "Stoop. — 10 founding landlord spots, $5/door for life" },
      {
        name: "description",
        content:
          "Tenants text one number. Stoop triages every message, drafts replies in your voice, and only a true emergency rings your phone. Ontario founding cohort: $5/door, locked for life.",
      },
    ],
    links: [
      { rel: "preconnect", href: "https://fonts.googleapis.com" },
      { rel: "preconnect", href: "https://fonts.gstatic.com", crossOrigin: "anonymous" },
      {
        rel: "stylesheet",
        href: "https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:ital,wght@0,400;0,500;0,700;0,900;1,400&family=Spline+Sans+Mono:wght@400;500;700&display=swap",
      },
    ],
  }),
  component: FoundingPage,
});

/* ── interactive triage demo ─────────────────────────────────────── */

type ScenarioKey = "urgent" | "emergency" | "routine";

const SCENARIOS: Record<
  ScenarioKey,
  {
    label: string;
    time: string;
    tenant: { who: string; text: string };
    think: string;
    chip: { cls: string; label: string };
    note: string;
    draft: { who: string; text: string };
    foot: { cls: string; text: string };
  }
> = {
  urgent: {
    label: "No heat, 2 AM",
    time: "02:12",
    tenant: {
      who: "TENANT · MARIA · UNIT 2",
      text: "hey sorry to text so late but the heat hasn't worked since 10pm and it's getting really cold… we have the baby this week. anything you can do tonight??",
    },
    think:
      "no heat + overnight + infant present · Ontario bylaw: ≥21°C required · no flood / gas / fire →",
    chip: { cls: "urgent", label: "URGENT" },
    note: "CLASSIFIED IN 22s",
    draft: {
      who: "STOOP DRAFT · YOUR VOICE",
      text: "Hi Maria — so sorry, that's not okay with the little one there. Try the breaker in the hall closet (switch 4) and text me in 10 min. Still cold? HVAC guy at 7:30 AM. Space heater's in basement storage tonight.",
    },
    foot: { cls: "wait", text: "HELD FOR YOUR APPROVAL — WAITING IN YOUR MORNING QUEUE" },
  },
  emergency: {
    label: "Water through ceiling",
    time: "00:21",
    tenant: {
      who: "TENANT · DEV · UNIT 3",
      text: "WATER IS COMING THROUGH THE CEILING LIGHT IN THE LIVING ROOM. it's getting worse. what do I do??",
    },
    think:
      "active water + electrical fixture · spread risk to unit below · matches emergency criteria →",
    chip: { cls: "emergency", label: "EMERGENCY" },
    note: "PROTOCOL FIRED IN 18s",
    draft: {
      who: "STOOP — SENT IMMEDIATELY (SAFETY)",
      text: "Dev — shut off the breaker for the living room now (panel, top-left). Move things clear and don't touch the fixture. I've called your landlord — help is moving.",
    },
    foot: { cls: "call", text: "CALLING YOUR PHONE NOW — THIS ONE CAN'T WAIT" },
  },
  routine: {
    label: "Parking question",
    time: "09:02",
    tenant: {
      who: "TENANT · SAM · UNIT 1B",
      text: "hey! my sister is visiting this weekend — is there visitor parking or should she find street parking?",
    },
    think: "no maintenance issue · answer exists in your house rules §4 · zero risk →",
    chip: { cls: "routine", label: "ROUTINE" },
    note: "TRUST LV2 · AUTO-SENT",
    draft: {
      who: "STOOP — SENT FOR YOU · RECAP LOGGED",
      text: "Hey Sam! One visitor spot behind the building (sign says P2) — first come, first served, max 48h. Street parking is fine after 6 too. Have a great weekend!",
    },
    foot: { cls: "auto", text: "HANDLED SOLO — YOU'LL SEE IT IN TONIGHT'S RECAP" },
  },
};

function TriageDemo() {
  const [scenario, setScenario] = useState<ScenarioKey>("urgent");
  const [stage, setStage] = useState(0);
  const timers = useRef<ReturnType<typeof setTimeout>[]>([]);

  useEffect(() => {
    timers.current.forEach(clearTimeout);
    timers.current = [];
    setStage(0);
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const delays = reduce ? [0, 0, 0, 0, 0] : [150, 1000, 2300, 3100, 4100];
    delays.forEach((ms, i) => {
      timers.current.push(setTimeout(() => setStage(i + 1), ms));
    });
    return () => timers.current.forEach(clearTimeout);
  }, [scenario]);

  const s = SCENARIOS[scenario];

  return (
    <div className="fnd-demo">
      <div className="fnd-demo-head">
        <span className="fnd-lights" aria-hidden="true">
          <i /> <i /> <i />
        </span>
        <span>LIVE TRIAGE · TRY A SCENARIO</span>
        <span>{s.time}</span>
      </div>
      <div className="fnd-scenarios" role="group" aria-label="Demo scenarios">
        {(Object.keys(SCENARIOS) as ScenarioKey[]).map((k) => (
          <button
            key={k}
            type="button"
            className="fnd-scn"
            aria-pressed={scenario === k}
            onClick={() => setScenario(k)}
          >
            {SCENARIOS[k].label}
          </button>
        ))}
      </div>
      <div className="fnd-stage" aria-live="polite">
        <div className={`fnd-bubble tenant ${stage >= 1 ? "show" : ""}`}>
          <div className="who">{s.tenant.who}</div>
          {s.tenant.text}
        </div>
        <div className={`fnd-think ${stage >= 2 ? "show" : ""}`}>
          <b>AGENT</b>{" "}
          <span className="scan" aria-hidden="true">
            ▮
          </span>{" "}
          {s.think}
        </div>
        <div className={`fnd-verdict ${stage >= 3 ? "show" : ""}`}>
          <span className={`fnd-chip ${s.chip.cls}`}>{s.chip.label}</span>
          <small>{s.note}</small>
        </div>
        <div className={`fnd-bubble draft ${stage >= 4 ? "show" : ""}`}>
          <div className="who">{s.draft.who}</div>
          {s.draft.text}
        </div>
        <div className={`fnd-foot ${stage >= 5 ? "show" : ""}`}>
          <span className={`fnd-hold ${s.foot.cls}`}>{s.foot.text}</span>
        </div>
      </div>
    </div>
  );
}

/* ── waitlist form ───────────────────────────────────────────────── */

function WaitlistForm() {
  const [status, setStatus] = useState<"idle" | "sending" | "done" | "error">("idle");

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    setStatus("sending");
    try {
      const res = await joinWaitlist({
        data: {
          name: String(fd.get("name") ?? ""),
          email: String(fd.get("email") ?? ""),
          city: String(fd.get("city") ?? ""),
          doors: fd.get("doors") ? Number(fd.get("doors")) : undefined,
          isPm: fd.get("isPm") === "on",
          website: String(fd.get("website") ?? ""),
        },
      });
      setStatus(res.ok ? "done" : "error");
    } catch {
      setStatus("error");
    }
  }

  if (status === "done") {
    return (
      <div className="fnd-success" role="status">
        <strong>You're on the list.</strong>
        <p>
          We onboard founding landlords personally, in small batches. You'll hear from Laith
          directly — no newsletter, no spam.
        </p>
      </div>
    );
  }

  return (
    <form className="fnd-form" onSubmit={onSubmit}>
      <div className="fnd-row">
        <label>
          Name
          <input name="name" required autoComplete="name" maxLength={120} />
        </label>
        <label>
          Email
          <input name="email" type="email" required autoComplete="email" maxLength={254} />
        </label>
      </div>
      <div className="fnd-row">
        <label>
          City
          <input name="city" placeholder="Toronto" maxLength={120} />
        </label>
        <label>
          Doors you manage
          <input name="doors" type="number" min={1} max={10000} placeholder="2" />
        </label>
      </div>
      <label className="fnd-check">
        <input type="checkbox" name="isPm" />
        I'm a property manager (20+ doors) — put me on the Stoop Desk waitlist instead
      </label>
      <input
        type="text"
        name="website"
        tabIndex={-1}
        autoComplete="off"
        aria-hidden="true"
        className="fnd-hp"
      />
      <button type="submit" className="fnd-cta" disabled={status === "sending"}>
        {status === "sending" ? "Saving…" : "Claim a founding spot →"}
      </button>
      {status === "error" && (
        <p className="fnd-err" role="alert">
          That didn't go through — email us instead:{" "}
          <a href="mailto:allaithalzoubi2@gmail.com?subject=Stoop%20founding%20landlord">
            get in touch directly
          </a>
          .
        </p>
      )}
    </form>
  );
}

/* ── page ────────────────────────────────────────────────────────── */

function FoundingPage() {
  return (
    <div className="fnd">
      <style>{CSS}</style>
      <div className="fnd-wrap">
        <nav className="fnd-nav" aria-label="Primary">
          <span className="fnd-logo">
            Stoop<span className="dot">.</span>
          </span>
          <a className="fnd-nav-cta" href="#claim">
            Claim a founding spot
          </a>
        </nav>

        <header className="fnd-hero">
          <div>
            <span className="fnd-pill">
              <span className="dot" aria-hidden="true" />
              FOUNDING COHORT · 10 SPOTS · ONTARIO
            </span>
            <h1>
              Tenant texts,
              <br />
              handled <em>before</em>
              <br />
              they wake you.
            </h1>
            <p className="fnd-sub">
              Your tenants text one number. Stoop reads every message, ranks how bad it really is,
              and drafts the reply <b>in your voice</b> — held for your approval.{" "}
              <b>Only a true emergency rings your phone.</b>
            </p>
            <div className="fnd-offer-line">
              Founding landlords pay <b>$5/door/month — locked for life.</b> Later cohorts will pay
              more. Emergency triage is free forever, for everyone.
            </div>
            <a className="fnd-cta" href="#claim">
              Claim a founding spot →
            </a>
          </div>
          <TriageDemo />
        </header>

        <section className="fnd-claim" id="claim">
          <div className="fnd-cards">
            <div className="fnd-card">
              <h3>Safety Net</h3>
              <div className="price">
                $0<small>/forever</small>
              </div>
              <ul>
                <li>Full triage + severity classification</li>
                <li>Emergency calls + escalation chain</li>
                <li>Instant tenant safety texts</li>
              </ul>
            </div>
            <div className="fnd-card featured">
              <span className="ribbon">FOUNDING RATE</span>
              <h3>Founding landlord</h3>
              <div className="price">
                $5<small>/door/mo</small>
              </div>
              <ul>
                <li>Everything in Safety Net</li>
                <li>Drafts in your voice, approve to send</li>
                <li>Earned auto-send for routine replies</li>
                <li>Audit trail + LTB-ready export</li>
                <li>
                  <b>Rate locked for life</b>
                </li>
              </ul>
            </div>
          </div>
          <div className="fnd-form-col">
            <h2>10 spots. White-glove onboarding. Cancel anytime.</h2>
            <p>
              We set you up personally, your tenants keep texting like they always have, and you
              stop being the 3 AM phone line.
            </p>
            <WaitlistForm />
          </div>
        </section>

        <footer className="fnd-footer">
          <span>STOOP. — TENANT MAINTENANCE, HANDLED · TORONTO</span>
          <span>BUILT FOR ONTARIO LANDLORDS</span>
        </footer>
      </div>
    </div>
  );
}

/* ── scoped styles (Brownstone, trimmed) ─────────────────────────── */

const CSS = `
.fnd {
  --void:#131009; --panel:#1b1710; --panel2:#221d14; --line:#322a1d; --lineb:#54462f;
  --text:#f2ead9; --dim:#b3a68e; --faint:#91836b;
  --accent:#ffb454; --accentb:#a06a24; --accentbg:rgba(255,180,84,.09); --onaccent:#221603;
  --terra:#e0673f; --mint:#7fdcab; --blue:#9fc2ec; --red:#ff6d5e;
  --grot:"Schibsted Grotesk",ui-sans-serif,system-ui,sans-serif;
  --mono:"Spline Sans Mono",ui-monospace,monospace;
  --spring:cubic-bezier(.34,1.45,.64,1); --ease:cubic-bezier(.16,1,.3,1);
  background:var(--void); color:var(--text); font-family:var(--grot);
  min-height:100vh;
}
.fnd *{box-sizing:border-box;margin:0;padding:0}
.fnd :focus-visible{outline:3px solid var(--accent);outline-offset:3px;border-radius:6px}
.fnd-wrap{position:relative;max-width:1200px;margin:0 auto;padding:22px clamp(16px,3vw,28px) 0;
  background:radial-gradient(1000px 520px at 80% -10%,rgba(255,180,84,.08),transparent 60%)}
.fnd-nav{display:flex;align-items:center;justify-content:space-between;border:1px solid var(--line);
  border-radius:999px;padding:10px 12px 10px 24px;background:rgba(27,23,16,.88)}
.fnd-logo{font-weight:900;font-size:22px;letter-spacing:-.02em}
.fnd-logo .dot{color:var(--accent)}
.fnd-nav-cta{font-weight:800;font-size:14px;text-decoration:none;color:var(--onaccent);
  background:var(--accent);padding:12px 22px;border-radius:999px;min-height:44px;display:inline-flex;align-items:center}
.fnd-hero{display:grid;grid-template-columns:1.05fr 1fr;gap:52px;align-items:center;padding:72px 0 56px}
.fnd-pill{display:inline-flex;align-items:center;gap:9px;font-family:var(--mono);font-size:11.5px;
  letter-spacing:.14em;color:var(--accent);border:1px solid var(--accentb);border-radius:999px;padding:7px 15px;font-weight:500}
.fnd-pill .dot{width:7px;height:7px;border-radius:50%;background:var(--mint);box-shadow:0 0 9px var(--mint)}
.fnd-hero h1{font-size:clamp(38px,4.8vw,64px);font-weight:900;letter-spacing:-.035em;line-height:1.02;margin:20px 0}
.fnd-hero h1 em{font-style:italic;font-weight:400;color:var(--accent);font-family:Georgia,serif}
.fnd-sub{font-size:17px;line-height:1.7;color:var(--dim);max-width:46ch}
.fnd-sub b{color:var(--text)}
.fnd-offer-line{margin:22px 0;padding:14px 18px;border:1px dashed var(--accentb);border-radius:14px;
  font-size:15px;line-height:1.6;color:var(--dim);background:var(--accentbg)}
.fnd-offer-line b{color:var(--accent)}
.fnd-cta{display:inline-block;font-weight:800;font-size:16px;padding:16px 28px;border-radius:999px;
  background:var(--accent);color:var(--onaccent);text-decoration:none;border:1px solid var(--accent);
  cursor:pointer;transition:transform .2s var(--spring),box-shadow .3s;min-height:52px}
.fnd-cta:hover{transform:translateY(-3px);box-shadow:0 10px 34px rgba(255,180,84,.25)}
.fnd-cta:disabled{opacity:.6;transform:none}
/* demo */
.fnd-demo{background:linear-gradient(180deg,var(--panel2),var(--panel));border:1px solid var(--line);
  border-radius:22px;overflow:hidden;box-shadow:0 30px 80px rgba(0,0,0,.5)}
.fnd-demo-head{display:flex;align-items:center;justify-content:space-between;padding:14px 20px;
  border-bottom:1px solid var(--line);font-family:var(--mono);font-size:11px;letter-spacing:.16em;color:var(--faint)}
.fnd-lights{display:flex;gap:6px}
.fnd-lights i{width:9px;height:9px;border-radius:50%;background:var(--lineb);display:inline-block}
.fnd-lights i:first-child{background:var(--terra)}
.fnd-scenarios{display:flex;gap:8px;padding:14px 16px;border-bottom:1px solid var(--line);flex-wrap:wrap}
.fnd-scn{font-family:var(--mono);font-size:12px;font-weight:500;color:var(--dim);background:transparent;
  border:1px solid var(--lineb);border-radius:999px;padding:9px 16px;min-height:44px;cursor:pointer;
  transition:all .25s var(--spring)}
.fnd-scn:hover{color:var(--text);transform:translateY(-1px)}
.fnd-scn[aria-pressed="true"]{background:var(--accent);border-color:var(--accent);color:var(--onaccent);font-weight:700}
.fnd-stage{padding:20px;min-height:340px;display:flex;flex-direction:column;gap:12px}
.fnd-bubble{border-radius:14px;padding:12px 15px;font-size:14px;line-height:1.6;max-width:90%;
  opacity:0;transform:translateY(12px);transition:opacity .5s var(--ease),transform .5s var(--spring)}
.fnd-bubble.show{opacity:1;transform:none}
.fnd-bubble .who{font-family:var(--mono);font-size:10px;letter-spacing:.14em;color:var(--faint);margin-bottom:6px;font-weight:500}
.fnd-bubble.tenant{background:var(--panel2);border:1px solid var(--line);border-bottom-left-radius:4px}
.fnd-bubble.draft{margin-left:auto;border-bottom-right-radius:4px;border:1px solid var(--accentb);background:var(--accentbg)}
.fnd-bubble.draft .who{color:var(--accent)}
.fnd-think{font-family:var(--mono);font-size:11px;color:var(--dim);border:1px dashed var(--lineb);
  border-radius:10px;padding:10px 14px;line-height:1.8;background:rgba(159,194,236,.04);
  opacity:0;transform:translateY(12px);transition:opacity .5s,transform .5s var(--spring)}
.fnd-think.show{opacity:1;transform:none}
.fnd-think b{color:var(--blue);font-weight:500}
.fnd-think .scan{animation:fndblink .9s steps(1) infinite;color:var(--blue)}
.fnd-verdict{display:flex;align-items:center;gap:10px;opacity:0;transform:scale(.92);
  transition:opacity .4s,transform .45s var(--spring)}
.fnd-verdict.show{opacity:1;transform:none}
.fnd-verdict small{font-family:var(--mono);font-size:10.5px;letter-spacing:.08em;color:var(--faint)}
.fnd-chip{font-family:var(--mono);font-size:11px;font-weight:700;letter-spacing:.14em;padding:6px 14px;
  border-radius:999px;border:1px solid}
.fnd-chip.urgent{color:var(--accent);border-color:var(--accentb);background:var(--accentbg)}
.fnd-chip.emergency{color:var(--red);border-color:rgba(255,109,94,.5);background:rgba(255,109,94,.09)}
.fnd-chip.routine{color:var(--blue);border-color:rgba(159,194,236,.35);background:rgba(159,194,236,.08)}
.fnd-foot{margin-top:auto;opacity:0;transform:translateY(10px);transition:opacity .5s,transform .5s var(--spring)}
.fnd-foot.show{opacity:1;transform:none}
.fnd-hold{display:block;text-align:center;font-family:var(--mono);font-size:11px;letter-spacing:.1em;
  padding:14px;border-radius:999px;font-weight:500}
.fnd-hold.wait{color:var(--accent);border:1px dashed var(--accentb)}
.fnd-hold.call{color:var(--red);border:1px solid rgba(255,109,94,.5)}
.fnd-hold.auto{color:var(--mint);border:1px dashed rgba(127,220,171,.35)}
/* claim section */
.fnd-claim{display:grid;grid-template-columns:1fr 1fr;gap:48px;padding:48px 0 72px;align-items:start}
.fnd-cards{display:grid;gap:14px}
.fnd-card{border:1px solid var(--line);border-radius:20px;padding:26px 26px;background:linear-gradient(180deg,var(--panel2),var(--panel));position:relative;overflow:hidden}
.fnd-card.featured{border-color:var(--accentb)}
.fnd-card .ribbon{position:absolute;top:18px;right:-34px;background:var(--accent);color:var(--onaccent);
  font-family:var(--mono);font-size:9.5px;font-weight:700;letter-spacing:.12em;padding:5px 40px;transform:rotate(36deg)}
.fnd-card h3{font-weight:900;font-size:19px}
.fnd-card .price{font-size:44px;font-weight:900;letter-spacing:-.03em;margin:12px 0 14px}
.fnd-card .price small{font-size:14px;color:var(--dim);font-weight:500}
.fnd-card ul{list-style:none}
.fnd-card li{font-size:14px;color:var(--dim);padding:8px 0;border-top:1px solid var(--line)}
.fnd-card li b{color:var(--accent)}
.fnd-form-col h2{font-size:clamp(24px,2.6vw,32px);font-weight:900;letter-spacing:-.02em;line-height:1.15}
.fnd-form-col>p{color:var(--dim);font-size:15px;line-height:1.7;margin:12px 0 24px}
.fnd-form{display:flex;flex-direction:column;gap:16px}
.fnd-row{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.fnd-form label{display:flex;flex-direction:column;gap:7px;font-family:var(--mono);font-size:11px;
  letter-spacing:.14em;text-transform:uppercase;color:var(--faint);font-weight:500}
.fnd-form input[type=text],.fnd-form input[type=email],.fnd-form input[type=number],.fnd-form input:not([type]){
  background:var(--panel2);border:1px solid var(--lineb);border-radius:12px;color:var(--text);
  font-family:var(--grot);font-size:15px;padding:14px 16px;min-height:48px;width:100%}
.fnd-form input:focus{border-color:var(--accent);outline:none}
.fnd-check{flex-direction:row!important;align-items:center;gap:10px!important;text-transform:none!important;
  font-family:var(--grot)!important;font-size:13.5px!important;letter-spacing:0!important;color:var(--dim)!important;cursor:pointer}
.fnd-check input{width:18px;height:18px;accent-color:#ffb454}
.fnd-hp{position:absolute;left:-9999px;width:1px;height:1px;opacity:0}
.fnd-err{font-size:13.5px;color:var(--red)}
.fnd-err a{color:var(--accent)}
.fnd-success{border:1px solid rgba(127,220,171,.35);background:rgba(127,220,171,.06);border-radius:16px;padding:24px}
.fnd-success strong{font-size:18px;font-weight:900}
.fnd-success p{color:var(--dim);font-size:14.5px;line-height:1.7;margin-top:8px}
.fnd-footer{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;border-top:1px solid var(--line);
  padding:30px 0 42px;font-family:var(--mono);font-size:11px;letter-spacing:.1em;color:var(--faint)}
@keyframes fndblink{50%{opacity:0}}
@media (prefers-reduced-motion:reduce){
  .fnd *{transition-duration:.01ms!important;animation-duration:.01ms!important}
}
@media (max-width:920px){
  .fnd-hero,.fnd-claim{grid-template-columns:1fr;gap:36px}
  .fnd-row{grid-template-columns:1fr}
}
`;
