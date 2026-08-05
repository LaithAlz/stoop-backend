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
type Card = Record<string, unknown>;
const routine = (id: string, body: string): Card => ({
  case_id: "case-" + id, draft_id: id, severity: "routine", title: null,
  tenant_name: "Maria Ortiz", property_label: "12 Ossington", unit: "2",
  tenant_message: "Kitchen tap drips.", received_at: new Date(Date.now() - 3600_000).toISOString(),
  has_media: false, media_note: null, draft_body: body, why: "Routine.",
  reasoning: [], refusal_flags: [], recipient: "tenant", notification_id: null,
});
const SERVER = { items: [] as Card[], editStatus: 500, rejects: 0 };
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", date: new Date().toUTCString() } });
globalThis.fetch = (async (input: any, init: any = {}) => {
  const url = String(input); const method = init.method ?? "GET";
  if (url.endsWith("/v1/queue")) return json(200, { items: SERVER.items, counts: { total: SERVER.items.length, awaiting_tenant: 0 } });
  const e = url.match(/\/v1\/drafts\/([^/]+)\/edit-and-send$/);
  if (e && method === "POST") return json(SERVER.editStatus, { error: { code: "server_error", message: "x", request_id: "r" } });
  const r = url.match(/\/v1\/drafts\/([^/]+)\/reject$/);
  if (r && method === "POST") { SERVER.rejects++; SERVER.items = SERVER.items.filter((i) => i.draft_id !== r[1]); return json(200, { draft_id: r[1], status: "rejected" }); }
  const a = url.match(/\/v1\/drafts\/([^/]+)\/approve$/);
  if (a && method === "POST") { SERVER.items = SERVER.items.filter((i) => i.draft_id !== a[1]); return json(200, { draft_id: a[1], status: "approved", undo_until: new Date(Date.now() + 5000).toISOString(), scheduled_send_at: new Date(Date.now() + 5000).toISOString() }); }
  return json(200, {});
}) as any;
const Home = (HomeRoute as any).options.component as () => any;
let qc: QueryClient = createQueryClient(); let root: Root;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const btns = () => Array.from(document.querySelectorAll("article button")) as HTMLButtonElement[];
const byText = (t: string) => btns().find((e) => (e.textContent ?? "").includes(t));
const state = () => JSON.stringify(btns().map(b => (b.textContent ?? "").trim().split("\n")[0].slice(0,20) + (b.disabled ? "[DIS]" : "[en]")));
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
  const CLOCK = { off: 0 };
  const realNow = Date.now.bind(Date);
  Date.now = () => realNow() + CLOCK.off;
  log("=== after the 120s ceiling releases the flag, what is left ON THE CARD? ===");
  byText("Edit")!.click(); await sleep(60);
  const ta = document.querySelector("textarea") as HTMLTextAreaElement;
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
  setter.call(ta, "EDITED BODY THE LANDLORD ACTUALLY WANTED"); ta.dispatchEvent(new Event("input", { bubbles: true }));
  await sleep(50);
  byText("Send edited version")!.click();
  await sleep(400);
  byText("Cancel")!.click(); await sleep(120);
  log("flagged: controls:", state());
  log("flagged: card marking:", JSON.stringify((document.querySelector("article")?.textContent ?? "").replace(/\s+/g," ").slice(0, 260)));
  NOTICES.length = 0;
  CLOCK.off = 121_000;
  await sleep(1600);
  log("");
  log("--- ceiling has fired ---");
  log("toast:", JSON.stringify(NOTICES));
  log("released: controls:", state());
  log("released: card marking:", JSON.stringify((document.querySelector("article")?.textContent ?? "").replace(/\s+/g," ").slice(0, 260)));
  log("card still warns about the unconfirmed send:", (document.querySelector("article")?.textContent ?? "").includes("Checking whether"));
  log("card body shown is the ORIGINAL, pre-edit text:", (document.querySelector("article")?.textContent ?? "").includes("BODY P"));
  log("DONE");
  flush();
}
void main();
