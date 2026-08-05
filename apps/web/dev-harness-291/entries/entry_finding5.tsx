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
  const e = url.match(/\/v1\/drafts\/([^/]+)\/edit-and-send$/);
  if (e && method === "POST") {
    SERVER.items = SERVER.items.filter((i) => i.draft_id !== e[1]);
    return json(200, { draft_id: e[1], status: "approved", undo_until: new Date(Date.now() + 5000).toISOString(), scheduled_send_at: new Date(Date.now() + 5000).toISOString() });
  }
  return json(200, {});
}) as any;
const Home = (HomeRoute as any).options.component as () => any;
let qc: QueryClient = createQueryClient(); let root: Root;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const byText = (t: string) => Array.from(document.querySelectorAll("article button")).find(b => (b.textContent ?? "").includes(t)) as HTMLButtonElement | undefined;
const ids = () => Array.from(document.querySelectorAll("article")).map(a => (a.textContent ?? "").match(/BODY [PQR]|EDITED BODY/)?.[0] ?? "?");
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
  log("=== Finding 5: edit-and-send on a card that fell out of decisionItems while editing ===");
  log("initial order:", JSON.stringify(ids()));
  // open the editor on the SECOND card (p2, index 1)
  const cards = Array.from(document.querySelectorAll("article"));
  const editBtn = Array.from(cards[1].querySelectorAll("button")).find(b => (b.textContent ?? "").includes("Edit")) as HTMLButtonElement;
  editBtn.click();
  await sleep(80);
  log("editor open on p2, textarea present:", Boolean(document.querySelector("textarea")));
  const ta = document.querySelector("textarea") as HTMLTextAreaElement;
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
  setter.call(ta, "EDITED BODY Q"); ta.dispatchEvent(new Event("input", { bubbles: true }));
  await sleep(30);
  // simulate a background poll that drops p2 from the server read entirely
  // (A7's own scenario: "it briefly drops off the list on an unrelated
  // field update") while the editor is still open, BEFORE submit.
  SERVER.items = SERVER.items.filter((i) => i.draft_id !== "p2");
  await qc.invalidateQueries();
  await sleep(200);
  log("after the background poll drops p2, order (editor still pinned):", JSON.stringify(ids()));
  const sendBtn = Array.from(document.querySelectorAll("button")).find(b => (b.textContent ?? "").includes("Send edited version")) as HTMLButtonElement;
  sendBtn.click();
  await sleep(300);
  log("right after edit-and-send succeeds, order:", JSON.stringify(ids()), "(p2's card, now sending, must stay at index 1: between p1 and p3)");
  log("DONE");
  flush();
}
void main();
