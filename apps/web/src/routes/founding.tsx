import { createFileRoute } from "@tanstack/react-router";
import { createServerFn } from "@tanstack/react-start";
import { useEffect, useState } from "react";
import { z } from "zod";

/* ──────────────────────────────────────────────────────────────────
 * Founding-cohort waitlist page (issue #114) — minimal version.
 * One idea, one action: email capture. Style-scoped under .fnd.
 * ────────────────────────────────────────────────────────────────── */

const waitlistSchema = z.object({
  email: z.string().trim().email().max(254),
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
    const { email, isPm } = parsed.data;
    await db
      .prepare(
        `INSERT INTO waitlist (name, email, is_pm, source)
         VALUES ('', ?, ?, 'founding-page')
         ON CONFLICT(email) DO NOTHING`,
      )
      .bind(email.toLowerCase(), isPm ? 1 : 0)
      .run();
    return { ok: true };
  },
);

export const Route = createFileRoute("/founding")({
  head: () => ({
    meta: [
      { title: "Stoop. — Your tenants text. You sleep." },
      {
        name: "description",
        content:
          "Stoop reads every tenant message, handles what it can, and only rings your phone for a true emergency. Founding Ontario landlords: $5/door, locked for life.",
      },
    ],
    links: [
      { rel: "preconnect", href: "https://fonts.googleapis.com" },
      { rel: "preconnect", href: "https://fonts.gstatic.com", crossOrigin: "anonymous" },
      {
        rel: "stylesheet",
        href: "https://fonts.googleapis.com/css2?family=Schibsted+Grotesk:ital,wght@0,400;0,700;0,900;1,400&family=Spline+Sans+Mono:wght@400;500&display=swap",
      },
    ],
  }),
  component: FoundingPage,
});

/* ── single quiet example (no console, no buttons) ───────────────── */

function Example() {
  const [stage, setStage] = useState(0);

  useEffect(() => {
    const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const delays = reduce ? [0, 0, 0] : [400, 1400, 2400];
    const timers = delays.map((ms, i) => setTimeout(() => setStage(i + 1), ms));
    return () => timers.forEach(clearTimeout);
  }, []);

  return (
    <figure className="fnd-example" aria-label="Example: how Stoop handles a 2 AM tenant text">
      <div className={`fnd-bubble tenant ${stage >= 1 ? "show" : ""}`}>
        <div className="who">TENANT · 2:12 AM</div>
        the heat stopped working and it's getting really cold… anything you can do tonight??
      </div>
      <div className={`fnd-mid ${stage >= 2 ? "show" : ""}`}>
        <span className="fnd-chip">URGENT — NOT AN EMERGENCY</span>
        <small>your phone stays silent</small>
      </div>
      <div className={`fnd-bubble draft ${stage >= 3 ? "show" : ""}`}>
        <div className="who">STOOP'S DRAFT · WAITING FOR YOUR OK AT 7 AM</div>
        Hi Maria — so sorry. Try the breaker in the hall closet and text me in 10 min. Still cold?
        My HVAC guy will be there at 7:30.
      </div>
    </figure>
  );
}

/* ── email capture ───────────────────────────────────────────────── */

function CaptureForm() {
  const [status, setStatus] = useState<"idle" | "sending" | "done" | "error">("idle");

  async function onSubmit(e: React.FormEvent<HTMLFormElement>) {
    e.preventDefault();
    const fd = new FormData(e.currentTarget);
    setStatus("sending");
    try {
      const res = await joinWaitlist({
        data: {
          email: String(fd.get("email") ?? ""),
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
      <p className="fnd-success" role="status">
        <b>You're on the list.</b> You'll hear from Laith directly — no newsletter, no spam.
      </p>
    );
  }

  return (
    <form className="fnd-form" onSubmit={onSubmit}>
      <div className="fnd-inline">
        <label className="fnd-sr" htmlFor="fnd-email">
          Email
        </label>
        <input
          id="fnd-email"
          name="email"
          type="email"
          required
          placeholder="you@email.com"
          autoComplete="email"
          maxLength={254}
        />
        <button type="submit" disabled={status === "sending"}>
          {status === "sending" ? "Saving…" : "Get early access"}
        </button>
      </div>
      <label className="fnd-check">
        <input type="checkbox" name="isPm" />
        I'm a property manager (20+ doors)
      </label>
      <input
        type="text"
        name="website"
        tabIndex={-1}
        autoComplete="off"
        aria-hidden="true"
        className="fnd-hp"
      />
      {status === "error" && (
        <p className="fnd-err" role="alert">
          That didn't go through —{" "}
          <a href="mailto:allaithalzoubi2@gmail.com?subject=Stoop%20early%20access">
            email us instead
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
      <main className="fnd-wrap">
        <span className="fnd-logo">
          Stoop<span className="dot">.</span>
        </span>

        <h1>
          Your tenants text.
          <br />
          <em>You sleep.</em>
        </h1>

        <p className="fnd-sub">
          Stoop reads every tenant message, drafts the reply in your voice, and only rings your
          phone for a true emergency.
        </p>

        <CaptureForm />

        <p className="fnd-micro">
          First 10 Ontario landlords: <b>$5/door, locked for life.</b> Emergency triage free
          forever.
        </p>

        <Example />

        <footer className="fnd-footer">STOOP. · TORONTO · BUILT FOR ONTARIO LANDLORDS</footer>
      </main>
    </div>
  );
}

/* ── scoped styles ───────────────────────────────────────────────── */

const CSS = `
.fnd {
  --void:#131009; --panel:#1b1710; --panel2:#221d14; --line:#322a1d; --lineb:#54462f;
  --text:#f2ead9; --dim:#b3a68e; --faint:#91836b;
  --accent:#ffb454; --accentb:#a06a24; --accentbg:rgba(255,180,84,.09); --onaccent:#221603;
  --mint:#7fdcab;
  --grot:"Schibsted Grotesk",ui-sans-serif,system-ui,sans-serif;
  --mono:"Spline Sans Mono",ui-monospace,monospace;
  --ease:cubic-bezier(.16,1,.3,1);
  background:var(--void); color:var(--text); font-family:var(--grot);
  min-height:100vh;
}
.fnd *{box-sizing:border-box;margin:0;padding:0}
.fnd :focus-visible{outline:3px solid var(--accent);outline-offset:3px;border-radius:6px}
.fnd-wrap{max-width:640px;margin:0 auto;padding:48px clamp(16px,4vw,28px) 60px;
  display:flex;flex-direction:column;align-items:center;text-align:center;gap:22px;
  background:radial-gradient(700px 420px at 50% -10%,rgba(255,180,84,.08),transparent 60%)}
.fnd-logo{font-weight:900;font-size:24px;letter-spacing:-.02em}
.fnd-logo .dot{color:var(--accent)}
.fnd h1{font-size:clamp(40px,8vw,64px);font-weight:900;letter-spacing:-.035em;line-height:1.04;margin-top:26px}
.fnd h1 em{font-style:italic;font-weight:400;color:var(--accent);font-family:Georgia,serif}
.fnd-sub{font-size:17px;line-height:1.65;color:var(--dim);max-width:42ch}
.fnd-form{width:100%;max-width:460px;display:flex;flex-direction:column;gap:12px;margin-top:6px}
.fnd-inline{display:flex;gap:10px}
.fnd-inline input{flex:1;background:var(--panel2);border:1px solid var(--lineb);border-radius:999px;
  color:var(--text);font-family:var(--grot);font-size:15.5px;padding:14px 20px;min-height:52px;min-width:0}
.fnd-inline input:focus{border-color:var(--accent);outline:none}
.fnd-inline button{font-family:var(--grot);font-weight:800;font-size:15.5px;padding:14px 24px;min-height:52px;
  border-radius:999px;border:1px solid var(--accent);background:var(--accent);color:var(--onaccent);
  cursor:pointer;white-space:nowrap;transition:transform .2s var(--ease),box-shadow .3s}
.fnd-inline button:hover{transform:translateY(-2px);box-shadow:0 8px 30px rgba(255,180,84,.25)}
.fnd-inline button:disabled{opacity:.6;transform:none}
.fnd-check{display:flex;align-items:center;justify-content:center;gap:8px;font-size:13px;color:var(--faint);cursor:pointer}
.fnd-check input{width:16px;height:16px;accent-color:#ffb454}
.fnd-hp{position:absolute;left:-9999px;width:1px;height:1px;opacity:0}
.fnd-sr{position:absolute;left:-9999px}
.fnd-err{font-size:13.5px;color:#ff6d5e}
.fnd-err a{color:var(--accent)}
.fnd-success{border:1px solid rgba(127,220,171,.35);background:rgba(127,220,171,.06);
  border-radius:999px;padding:15px 26px;font-size:15px;color:var(--dim)}
.fnd-success b{color:var(--text)}
.fnd-micro{font-family:var(--mono);font-size:12px;letter-spacing:.04em;color:var(--faint);max-width:48ch;line-height:1.8}
.fnd-micro b{color:var(--accent);font-weight:500}
/* example */
.fnd-example{width:100%;max-width:460px;display:flex;flex-direction:column;gap:12px;margin-top:30px;text-align:left}
.fnd-bubble{border-radius:16px;padding:13px 16px;font-size:14.5px;line-height:1.6;max-width:88%;
  opacity:0;transform:translateY(10px);transition:opacity .6s var(--ease),transform .6s var(--ease)}
.fnd-bubble.show{opacity:1;transform:none}
.fnd-bubble .who{font-family:var(--mono);font-size:9.5px;letter-spacing:.14em;color:var(--faint);margin-bottom:6px}
.fnd-bubble.tenant{background:var(--panel2);border:1px solid var(--line);border-bottom-left-radius:5px;align-self:flex-start}
.fnd-bubble.draft{border:1px solid var(--accentb);background:var(--accentbg);border-bottom-right-radius:5px;align-self:flex-end}
.fnd-bubble.draft .who{color:var(--accent)}
.fnd-mid{display:flex;align-items:center;gap:10px;justify-content:center;
  opacity:0;transition:opacity .6s var(--ease)}
.fnd-mid.show{opacity:1}
.fnd-mid small{font-family:var(--mono);font-size:10px;letter-spacing:.08em;color:var(--faint)}
.fnd-chip{font-family:var(--mono);font-size:10px;font-weight:700;letter-spacing:.12em;padding:5px 12px;
  border-radius:999px;border:1px solid var(--accentb);color:var(--accent);background:var(--accentbg)}
.fnd-footer{margin-top:44px;font-family:var(--mono);font-size:10.5px;letter-spacing:.12em;color:var(--faint)}
@media (prefers-reduced-motion:reduce){
  .fnd *{transition-duration:.01ms!important;animation-duration:.01ms!important}
}
@media (max-width:480px){
  .fnd-inline{flex-direction:column}
}
`;
