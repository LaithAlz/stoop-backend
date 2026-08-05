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
  SERVER.items = [routine("p1", "BODY P"), routine("p2", "BODY Q"), routine("p3", "BODY R")];
  render();
  await sleep(300);
  log("=== NORMAL approve: does the newly-pinned 'sent' status linger? ===");
  log("cards:", document.querySelectorAll("article").length);
  const approves = Array.from(document.querySelectorAll("article")).map(a => Array.from(a.querySelectorAll("button")).find(b => (b.textContent ?? "").includes("Approve & send")) as HTMLButtonElement);
  approves[0].click();       // approve the FIRST of three
  await sleep(300);
  const ids = () => Array.from(document.querySelectorAll("article")).map(a => (a.textContent ?? "").match(/BODY [PQR]/)?.[0] ?? "?");
  log("right after approve, order:", JSON.stringify(ids()), "(item 6: the approved card must stay FIRST)");
  await qc.invalidateQueries(); await sleep(300);
  log("after a refetch that drops the approved row, order:", JSON.stringify(ids()));
  await sleep(5200);
  log("");
  log("--- countdown expired ---");
  log("cards now:", document.querySelectorAll("article").length, JSON.stringify(ids()));
  log("any 'Sent.' left on screen:", (document.body.textContent ?? "").includes("Sent."));
  await sleep(1500);
  log("1.5s later, cards:", document.querySelectorAll("article").length, JSON.stringify(ids()));
  log("DONE");
  flush();
}
void main();
