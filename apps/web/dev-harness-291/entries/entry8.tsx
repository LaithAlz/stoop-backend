import { Component, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClientProvider, QueryClient } from "@tanstack/react-query";
import { createQueryClient } from "@/api/queryClient";
import { Route as HomeRoute } from "@/routes/app.index";
import { getUnverifiedSendSnapshot } from "@/features/queue/unverifiedSendStore";
import { NOTICES } from "sonner";

const out: string[] = [];
const log = (...a: unknown[]) => out.push(a.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(" "));
const flush = () => { document.getElementById("out")!.textContent = out.join("\n"); };

// --- timer accounting: does the ceiling interval leak across unmount? ---
const liveIntervals = new Set<number>();
const realSetInterval = window.setInterval.bind(window);
const realClearInterval = window.clearInterval.bind(window);
(window as any).setInterval = (fn: any, ms: number, ...rest: any[]) => {
  const id = realSetInterval(fn, ms, ...rest) as unknown as number;
  liveIntervals.add(id);
  return id;
};
(window as any).clearInterval = (id: number) => { liveIntervals.delete(id); return realClearInterval(id as any); };

// --- controllable clock ---
let CLOCK_OFFSET = 0;
const realNow = Date.now.bind(Date);
Date.now = () => realNow() + CLOCK_OFFSET;

type Card = Record<string, unknown>;
const routine = (id: string, body: string): Card => ({
  case_id: "case-" + id, draft_id: id, severity: "routine", title: null,
  tenant_name: "Maria Ortiz", property_label: "12 Ossington", unit: "2",
  tenant_message: "Kitchen tap drips.", received_at: new Date(realNow() - 3600_000).toISOString(),
  has_media: false, media_note: null, draft_body: body, why: "Routine.",
  reasoning: [], refusal_flags: [], recipient: "tenant", notification_id: null,
});

const SERVER = { items: [] as Card[], queueFails: false, editStatus: 500, fetches: [] as string[] };
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", date: new Date().toUTCString() } });
globalThis.fetch = (async (input: any, init: any = {}) => {
  const url = String(input);
  const method = init.method ?? "GET";
  SERVER.fetches.push(`${method} ${url.replace("http://api.test", "")}`);
  if (url.endsWith("/v1/queue")) {
    if (SERVER.queueFails) return json(500, { error: { code: "server_error", message: "boom", request_id: "r" } });
    return json(200, { items: SERVER.items, counts: { total: SERVER.items.length, awaiting_tenant: 0 } });
  }
  const m = url.match(/\/v1\/drafts\/([^/]+)\/edit-and-send$/);
  if (m && method === "POST") return json(SERVER.editStatus, { error: { code: "server_error", message: "boom", request_id: "r" } });
  return json(200, {});
}) as any;

const Home = (HomeRoute as any).options.component as () => any;
let qc: QueryClient = createQueryClient();
let root: Root;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const byText = (t: string, tag = "button") =>
  Array.from(document.querySelectorAll(tag)).find((e) => (e.textContent ?? "").includes(t)) as HTMLButtonElement | undefined;
const controls = () => Array.from(document.querySelectorAll("article button")).map((b) => (b.textContent ?? "").trim().split("\n")[0].slice(0, 18) + ((b as HTMLButtonElement).disabled ? " [DISABLED]" : " [enabled]"));

class Boundary extends Component<{ children: ReactNode }, { err: string | null }> {
  state = { err: null as string | null };
  static getDerivedStateFromError(e: any) { return { err: String(e?.stack ?? e) }; }
  render() { return this.state.err ? <pre>BOUNDARY: {this.state.err}</pre> : this.props.children; }
}
function render() { root.render(<QueryClientProvider client={qc}><Boundary><Home /></Boundary></QueryClientProvider>); }

async function main() {
  root = createRoot(document.getElementById("app")!);
  SERVER.items = [routine("p1", "BODY P"), routine("p2", "BODY Q")];
  render();
  await sleep(250);
  log("=== BLOCKER 2: an ambiguous edit-and-send with GET /v1/queue ALSO down ===");
  byText("Edit")!.click();
  await sleep(60);
  const ta = document.querySelector("textarea") as HTMLTextAreaElement;
  ta.value = "EDITED TEXT";
  ta.dispatchEvent(new Event("input", { bubbles: true }));
  await sleep(30);
  byText("Send")!.click();
  await sleep(400);
  log("flag raised:", JSON.stringify(Array.from(getUnverifiedSendSnapshot().keys())));
  log("notice:", JSON.stringify(NOTICES));
  NOTICES.length = 0;
  SERVER.queueFails = true; // the queue read itself is now down: the settle path can never run
  await sleep(100);
  log("controls on the flagged card:", JSON.stringify(controls().slice(0, 3)));
  log("controls on the OTHER card:", JSON.stringify(controls().slice(3)));
  log("live intervals while flagged:", liveIntervals.size);

  log("");
  log("--- jump the wall clock 121s forward (ceiling is 120s) ---");
  CLOCK_OFFSET = 121_000;
  await sleep(1400);
  log("flag after the ceiling:", JSON.stringify(Array.from(getUnverifiedSendSnapshot().keys())));
  log("give-up notice:", JSON.stringify(NOTICES));
  log("notices fired (must be exactly 1):", NOTICES.length);
  await sleep(2500);
  log("notices after 2.5 more seconds of ticks:", NOTICES.length);
  log("controls after the release:", JSON.stringify(controls().slice(0, 3)));
  log("live intervals after the flag cleared:", liveIntervals.size);

  log("");
  log("--- unmount Home while a flag is up: does the interval get cleaned up? ---");
  NOTICES.length = 0;
  CLOCK_OFFSET = 0;
  SERVER.queueFails = false;
  qc = createQueryClient();
  root.unmount();
  root = createRoot(document.getElementById("app")!);
  render();
  await sleep(300);
  byText("Edit")!.click();
  await sleep(60);
  const ta2 = document.querySelector("textarea") as HTMLTextAreaElement;
  ta2.value = "EDITED AGAIN";
  ta2.dispatchEvent(new Event("input", { bubbles: true }));
  await sleep(30);
  byText("Send")!.click();
  await sleep(400);
  SERVER.queueFails = true;
  log("flag raised again:", JSON.stringify(Array.from(getUnverifiedSendSnapshot().keys())));
  log("live intervals:", liveIntervals.size);
  root.unmount();
  await sleep(100);
  log("live intervals after unmount (expect 0):", liveIntervals.size);
  log("flag still up in the module singleton after unmount:", JSON.stringify(Array.from(getUnverifiedSendSnapshot().keys())));
  NOTICES.length = 0;
  CLOCK_OFFSET = 121_000;
  await sleep(1500);
  log("give-up notices fired while NOTHING is mounted (expect 0):", NOTICES.length);
  log("flag after the ceiling passed with nothing mounted:", JSON.stringify(Array.from(getUnverifiedSendSnapshot().keys())));
  log("");
  log("--- landlord comes back to Home 2 minutes later ---");
  root = createRoot(document.getElementById("app")!);
  render();
  await sleep(1600);
  log("give-up notices after remount:", JSON.stringify(NOTICES));
  log("flag after remount:", JSON.stringify(Array.from(getUnverifiedSendSnapshot().keys())));
  log("DONE");
  flush();
}
void main();
