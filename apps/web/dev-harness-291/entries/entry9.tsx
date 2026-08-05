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
const SERVER = { items: [] as Card[], deleteDelayMs: 0, deleteMode: "ok" as "ok" | "drop", deletes: 0 };
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", date: new Date().toUTCString() } });
const wait = (ms: number) => new Promise((r) => setTimeout(r, ms));
globalThis.fetch = (async (input: any, init: any = {}) => {
  const url = String(input);
  const method = init.method ?? "GET";
  if (url.endsWith("/v1/queue"))
    return json(200, { items: SERVER.items, counts: { total: SERVER.items.length, awaiting_tenant: 0 } });
  const m = url.match(/\/v1\/drafts\/([^/]+)\/approve$/);
  if (m && method === "POST") {
    SERVER.items = SERVER.items.filter((i) => i.draft_id !== m[1]);
    return json(200, { draft_id: m[1], status: "approved", undo_until: new Date(Date.now() + 5000).toISOString(), scheduled_send_at: new Date(Date.now() + 5000).toISOString() });
  }
  if (m && method === "DELETE") {
    SERVER.deletes++;
    if (SERVER.deleteDelayMs) await wait(SERVER.deleteDelayMs);
    if (SERVER.deleteMode === "drop") throw new TypeError("Failed to fetch");
    return new Response(null, { status: 204, headers: { date: new Date().toUTCString() } });
  }
  return json(200, {});
}) as any;

const Home = (HomeRoute as any).options.component as () => any;
let qc: QueryClient = createQueryClient();
let root: Root;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const byText = (t: string, tag = "button") =>
  Array.from(document.querySelectorAll(tag)).find((e) => (e.textContent ?? "").includes(t)) as HTMLElement | undefined;
const undoBtns = () => Array.from(document.querySelectorAll("button[aria-describedby]")) as HTMLButtonElement[];
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
  await sleep(250);
  log("=== double-tap Undo during a SLOW in-flight DELETE ===");
  byText("Approve & send")!.click();
  for (let w = 0; w < 3000 && undoBtns().length !== 1; w += 20) await sleep(20);
  SERVER.deleteDelayMs = 2500;
  SERVER.deleteMode = "drop";
  SERVER.deletes = 0;
  const b = undoBtns()[0];
  b.click(); b.click(); b.click();           // same tick
  await sleep(50); undoBtns()[0]?.click();   // next tick, prop update landed
  await sleep(800); undoBtns()[0]?.click();  // mid-flight
  await sleep(800); undoBtns()[0]?.click();  // still mid-flight
  log("DELETEs while in flight (must be 1):", SERVER.deletes);
  log("aria-busy on the button mid-flight:", undoBtns()[0]?.getAttribute("aria-busy"));
  await sleep(1400); // the DELETE now fails ambiguously
  log("after the ambiguous failure -> DELETEs:", SERVER.deletes);
  log("after the ambiguous failure -> ticket still mounted:", undoBtns().length === 1);
  log("after the ambiguous failure -> aria-busy:", undoBtns()[0]?.getAttribute("aria-busy"));
  NOTICES.length = 0;
  SERVER.deleteDelayMs = 0; SERVER.deleteMode = "ok";
  undoBtns()[0]?.click();
  await sleep(400);
  log("retry after the ambiguous failure -> DELETEs (must be 2):", SERVER.deletes);
  log("retry -> ticket gone (undo succeeded):", undoBtns().length === 0);
  log("retry -> notice:", JSON.stringify(NOTICES));
  log("DONE");
  flush();
}
void main();
