import { Component, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClientProvider, QueryClient } from "@tanstack/react-query";
import { createQueryClient } from "@/api/queryClient";
import { Route as HomeRoute } from "@/routes/app.index";
import { NOTICES } from "sonner";

const out: string[] = [];
const log = (...a: unknown[]) => out.push(a.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(" "));
const flush = () => { document.getElementById("out")!.textContent = out.join("\n"); };
type Card = Record<string, unknown>;
const routine = (id: string, body: string): Card => ({
  case_id: "case-" + id, draft_id: id, severity: "routine", title: null,
  tenant_name: "Maria Ortiz", property_label: "12 Ossington", unit: "2",
  tenant_message: "Kitchen tap drips.", received_at: new Date(Date.now() - 3600_000).toISOString(),
  has_media: false, media_note: null, draft_body: body, why: "Routine.",
  reasoning: [], refusal_flags: [], recipient: "tenant", notification_id: null,
});
const SERVER = { items: [] as Card[] };
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", date: new Date().toUTCString() } });
globalThis.fetch = (async (input: any, init: any = {}) => {
  const url = String(input); const method = init.method ?? "GET";
  if (url.endsWith("/v1/queue")) return json(200, { items: SERVER.items, counts: { total: SERVER.items.length, awaiting_tenant: 0 } });
  const m = url.match(/\/v1\/drafts\/([^/]+)\/approve$/);
  if (m && method === "POST") {
    SERVER.items = SERVER.items.filter((i) => i.draft_id !== m[1]);
    return json(200, { draft_id: m[1], status: "approved", undo_until: new Date(Date.now() + 5000).toISOString(), scheduled_send_at: new Date(Date.now() + 5000).toISOString() });
  }
  return json(200, {});
}) as any;
const Home = (HomeRoute as any).options.component as () => any;
let qc: QueryClient = createQueryClient(); let root: Root;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const raf = () => new Promise<number>((r) => requestAnimationFrame((t) => r(t)));
class Boundary extends Component<{ children: ReactNode }, { err: string | null }> {
  state = { err: null as string | null };
  static getDerivedStateFromError(e: any) { return { err: String(e?.stack ?? e) }; }
  render() { return this.state.err ? <pre>BOUNDARY: {this.state.err}</pre> : this.props.children; }
}
function render() { root.render(<QueryClientProvider client={qc}><Boundary><Home /></Boundary></QueryClientProvider>); }

async function main() {
  root = createRoot(document.getElementById("app")!);
  SERVER.items = [routine("p1", "BODY P")];
  render();
  await sleep(300);
  log("=== does 'Sent.' ever survive an actual PAINTED frame (rAF), post-BLOCKER1 fix ===");
  const ap = Array.from(document.querySelectorAll("article button")).find(b => (b.textContent ?? "").includes("Approve & send")) as HTMLButtonElement;
  ap.click();
  await sleep(200);
  await qc.invalidateQueries();
  await sleep(200);
  // now poll via rAF (one check per real painted frame) from just before
  // the 5s countdown expires through a couple seconds past it
  const frames: { t: number; sent: boolean }[] = [];
  const t0 = performance.now();
  while (performance.now() - t0 < 2000) {
    await raf();
    const sent = (document.body.textContent ?? "").includes("Sent.");
    frames.push({ t: Math.round(performance.now() - t0), sent });
  }
  const sentFrames = frames.filter((f) => f.sent);
  log("total painted frames observed:", frames.length);
  log("frames where 'Sent.' was actually on screen at paint time:", sentFrames.length);
  log("first/last such frame (ms into the window):", sentFrames.length ? [sentFrames[0].t, sentFrames[sentFrames.length - 1].t] : "none");
  log("DONE");
  flush();
}
void main();
