import { Component, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClientProvider, QueryClient } from "@tanstack/react-query";
import { createQueryClient } from "@/api/queryClient";
import { Route as HomeRoute } from "@/routes/app.index";
import { NOTICES } from "sonner";

const out: string[] = [];
const log = (...a: unknown[]) =>
  out.push(a.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(" "));
const flush = () => { document.getElementById("out")!.textContent = out.join("\n"); };
type Card = Record<string, unknown>;
const routine = (id: string, body: string): Card => ({
  case_id: "case-" + id, draft_id: id, severity: "routine", title: null,
  tenant_name: "Maria Ortiz", property_label: "12 Ossington", unit: "2",
  tenant_message: "Kitchen tap drips.", received_at: new Date(Date.now() - 3600_000).toISOString(),
  has_media: false, media_note: null, draft_body: body, why: "Routine.",
  reasoning: [], refusal_flags: [], recipient: "tenant", notification_id: null,
});

const SERVER = { items: [] as Card[], queueDelayMs: 0, approves: 0, edits: 0, editBodies: [] as string[] };
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", date: new Date().toUTCString() },
  });
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

globalThis.fetch = (async (input: any, init: any = {}) => {
  const url = String(input); const method = init.method ?? "GET";
  if (url.endsWith("/v1/queue")) {
    // snapshot the server state AT REQUEST TIME, exactly like a real read
    const snap = SERVER.items.slice();
    const delay = SERVER.queueDelayMs;
    if (delay) await sleep(delay);
    return json(200, { items: snap, counts: { total: snap.length, awaiting_tenant: 0 } });
  }
  const a = url.match(/\/v1\/drafts\/([^/]+)\/approve$/);
  if (a && method === "POST") {
    SERVER.approves += 1;
    // server-side: draft leaves the pending queue immediately
    SERVER.items = SERVER.items.filter((i) => i.draft_id !== a[1]);
    const at = new Date(Date.now() + 5000).toISOString();
    return json(200, { status: "approved", scheduled_send_at: at, undo_until: at });
  }
  const e = url.match(/\/v1\/drafts\/([^/]+)\/edit-and-send$/);
  if (e && method === "POST") {
    SERVER.edits += 1;
    SERVER.editBodies.push(JSON.parse(String(init.body)).body);
    // idempotent 200 on an already-approved draft: body NOT applied
    const at = new Date(Date.now() - 1000).toISOString();
    return json(200, { status: "approved", scheduled_send_at: at, undo_until: at });
  }
  return json(200, {});
}) as any;

const Home = (HomeRoute as any).options.component as () => any;
let qc: QueryClient = createQueryClient(); let root: Root;
const btns = () => Array.from(document.querySelectorAll("article button")) as HTMLButtonElement[];
const byText = (t: string) => btns().find((e) => (e.textContent ?? "").includes(t));
const state = () =>
  JSON.stringify(btns().map((b) => (b.textContent ?? "").trim().split("\n")[0].slice(0, 24) + (b.disabled ? "[DIS]" : "[en]")));
const cardText = () => (document.querySelector("article")?.textContent ?? "").replace(/\s+/g, " ");

class Boundary extends Component<{ children: ReactNode }, { err: string | null }> {
  state = { err: null as string | null };
  static getDerivedStateFromError(e: any) { return { err: String(e?.stack ?? e) }; }
  render() { return this.state.err ? <pre>BOUNDARY: {this.state.err}</pre> : this.props.children; }
}
function render() {
  root.render(<QueryClientProvider client={qc}><Boundary><Home /></Boundary></QueryClientProvider>);
}

async function main() {
  root = createRoot(document.getElementById("app")!);
  SERVER.items = [routine("p1", "ORIGINAL DRAFT BODY")];
  render();
  await sleep(400);
  log("=== R4-A: a queue read in flight ACROSS an approve resurrects a sent card ===");
  log("initial card controls:", state());

  // A perfectly ordinary refetch: window focus, the 20s poll, another
  // card's onSettled, or useAcknowledge's queue invalidation. It is issued
  // BEFORE the approve, so its server snapshot still lists the draft.
  SERVER.queueDelayMs = 1500;
  void qc.invalidateQueries({ queryKey: ["queue"] });
  await sleep(150);

  log("approving while that read is still in flight...");
  byText("Approve")!.click();
  await sleep(400);
  log("t+0.4s after approve  ->", state(), "|", JSON.stringify(cardText().slice(0, 60)));
  log("server saw approves:", SERVER.approves, " server items now:", JSON.stringify(SERVER.items.map((i) => i.draft_id)));

  // the stale read lands here (pre-approve snapshot, still lists p1)
  await sleep(1400);
  SERVER.queueDelayMs = 0;
  log("t+1.8s (stale pre-approve read has landed) ->", state());

  for (let i = 0; i < 10; i++) {
    await sleep(1000);
    log("t+" + (1.8 + i + 1).toFixed(1) + "s ->", state());
  }
  log("");
  log("--- undo window is over, reply HAS gone out server-side ---");
  log("card controls now:", state());
  log("card text now:", JSON.stringify(cardText().slice(0, 120)));
  const approveNow = byText("Approve");
  log("Approve live again on an already-sent reply:", Boolean(approveNow && !approveNow.disabled));
  const editNow = byText("Edit");
  log("Edit live again:", Boolean(editNow && !editNow.disabled));
  log("any on-screen warning?:", JSON.stringify(NOTICES));

  if (editNow && !editNow.disabled) {
    editNow.click();
    await sleep(80);
    const ta = document.querySelector("textarea") as HTMLTextAreaElement;
    log("editor reopened with body:", JSON.stringify(ta?.value));
    const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
    setter.call(ta, "CORRECTION THE LANDLORD BELIEVES WENT OUT");
    ta.dispatchEvent(new Event("input", { bubbles: true }));
    await sleep(50);
    (Array.from(document.querySelectorAll("button")).find((b) =>
      (b.textContent ?? "").includes("Send edited version")) as HTMLButtonElement).click();
    await sleep(500);
    log("server edit calls:", SERVER.edits, "bodies:", JSON.stringify(SERVER.editBodies));
    log("screen after that 'correction':", JSON.stringify(cardText().slice(0, 120)));
  }
  log("DONE");
  flush();
}
void main();
