import { Component, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClientProvider, QueryClient } from "@tanstack/react-query";
import { createQueryClient } from "@/api/queryClient";
import { Route as ThreadRoute } from "@/routes/app.conversations.$id";
import { NOTICES } from "sonner";
import { getUnverifiedSendSnapshot, resetUnverifiedSendStore } from "@/features/queue/unverifiedSendStore";

const out: string[] = [];
const log = (...a: unknown[]) => out.push(a.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(" "));
const flush = () => { document.getElementById("out")!.textContent = out.join("\n"); };

type Card = Record<string, unknown>;
const routine = (id: string, body: string): Card => ({
  case_id: "case-x", draft_id: id, severity: "routine", title: null,
  tenant_name: "Maria Ortiz", property_label: "12 Ossington", unit: "2",
  tenant_message: "Kitchen tap drips.", received_at: new Date(Date.now() - 3600_000).toISOString(),
  has_media: false, media_note: null, draft_body: body, why: "Routine.",
  reasoning: [], refusal_flags: [], recipient: "tenant", notification_id: null,
});

const SERVER = {
  items: [] as Card[], queueStatus: 200,
  caseDraft: { id: "draft-1", body: "ORIGINAL", status: "pending" } as any,
  editStatus: 200, editCode: "server_error", fetches: [] as string[],
};
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", date: new Date().toUTCString() } });
globalThis.fetch = (async (input: any, init: any = {}) => {
  const url = String(input);
  const method = init.method ?? "GET";
  SERVER.fetches.push(`${method} ${url.replace("http://api.test", "")}`);
  if (url.endsWith("/v1/queue")) {
    if (SERVER.queueStatus !== 200) return json(SERVER.queueStatus, { error: { code: "server_error", message: "x", request_id: "r" } });
    return json(200, { items: SERVER.items, counts: { total: SERVER.items.length, awaiting_tenant: 0 } });
  }
  if (url.includes("/v1/cases/")) {
    return json(200, {
      id: "case-x", status: "awaiting_approval", severity: "routine", title: "Tap drips",
      tenant: { id: "t1", name: "Maria Ortiz", unit: "2" },
      property: { id: "p1", label: "12 Ossington", address_line1: "12 Ossington Ave" },
      created_at: new Date(Date.now() - 7200_000).toISOString(), last_activity_at: new Date().toISOString(),
      timeline: [
        { kind: "message", id: "m1", direction: "inbound", party: "tenant", body: "Kitchen tap drips.", media: [], at: new Date(Date.now() - 3600_000).toISOString() },
        ...(SERVER.caseDraft ? [{ kind: "draft", ...SERVER.caseDraft, at: new Date(Date.now() - 60_000).toISOString(), recipient: "tenant" }] : []),
      ],
    });
  }
  const d = url.match(/\/v1\/drafts\/([^/]+)\/approve$/);
  if (d && method === "DELETE") {
    // the undo APPLIES server-side (draft back to pending) and the response is lost
    SERVER.caseDraft = { id: d[1], body: SERVER.caseDraft.body, status: "pending" };
    SERVER.items = [routine(d[1], SERVER.caseDraft.body)];
    // ROUND 5: see entry7.tsx's note. Throwing synchronously here made
    // the error always beat the 5s countdown, which is the one ordering
    // where round 4's dropped-stamp bug could not appear. The delay is
    // what makes this entry reproduce the realistic case.
    await new Promise((r) => setTimeout(r, UNDO_FAILURE_DELAY_MS));
    throw new TypeError("Failed to fetch");
  }
  const m = url.match(/\/v1\/drafts\/([^/]+)\/(approve|edit-and-send)$/);
  if (m && method === "POST") {
    const [, id, kind] = m;
    const status = kind === "approve" ? 200 : SERVER.editStatus;
    if (status !== 200) return json(status, { error: { code: kind === "approve" ? "server_error" : SERVER.editCode, message: "x", request_id: "r" } });
    SERVER.items = SERVER.items.filter((i) => i.draft_id !== id);
    if (SERVER.caseDraft?.id === id) SERVER.caseDraft = { ...SERVER.caseDraft, status: "approved" };
    return json(200, { draft_id: id, status: "approved", undo_until: new Date(Date.now() + 5000).toISOString(), scheduled_send_at: new Date(Date.now() + 5000).toISOString() });
  }
  return json(200, {});
}) as any;

const UNDO_FAILURE_DELAY_MS = 6000;

const Thread = (ThreadRoute as any).options.component as () => any;
let qc: QueryClient = createQueryClient();
let root: Root;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const byText = (t: string, tag = "button") =>
  Array.from(document.querySelectorAll(tag)).find((e) => (e.textContent ?? "").includes(t)) as HTMLElement | undefined;

class Boundary extends Component<{ children: ReactNode }, { err: string | null }> {
  state = { err: null as string | null };
  static getDerivedStateFromError(e: any) { return { err: String(e?.stack ?? e) }; }
  render() { return this.state.err ? <pre>BOUNDARY: {this.state.err}</pre> : this.props.children; }
}
function render() {
  root.render(<QueryClientProvider client={qc}><Boundary key={Math.random()}><Thread /></Boundary></QueryClientProvider>);
}

async function main() {
  root = createRoot(document.getElementById("app")!);
  log("=== THREAD: undo applies server-side, response lost ===");
  resetUnverifiedSendStore();
  SERVER.items = [routine("draft-1", "ORIGINAL Z")];
  SERVER.caseDraft = { id: "draft-1", body: "ORIGINAL Z", status: "pending" };
  SERVER.queueStatus = 200;
  render();
  await sleep(400);
  for (let w = 0; w < 3000 && !byText("Approve & send"); w += 30) await sleep(30);
  byText("Approve & send")!.click();
  const undoBtns = () => Array.from(document.querySelectorAll("button[aria-describedby]")) as HTMLButtonElement[];
  for (let w = 0; w < 3000 && undoBtns().length !== 1; w += 20) await sleep(20);
  log("undo ticket up:", undoBtns().length === 1);
  NOTICES.length = 0;
  undoBtns()[0].click();
  await sleep(600);
  log("notice:", JSON.stringify(NOTICES));
  log("server draft status after the DELETE applied:", SERVER.caseDraft.status, "(pending == nothing was sent)");
  log("ticket still up:", undoBtns().length === 1);
  await sleep(6000);
  const footer = document.querySelectorAll("button");
  log("");
  log("--- after the ambiguous failure has actually surfaced ---");
  // F3 (round 6): the earlier NOTICES sample fires before the failure
  // does, so it always printed [] and the ambiguous-undo toast was
  // asserted NOWHERE in the harness. That is why F1 (the toast being
  // dead code that told the landlord nothing happened) survived five
  // review rounds. Re-sampled here, after the delay.
  log("  notices after the failure: " + JSON.stringify(NOTICES));
  log("buttons on screen:", JSON.stringify(Array.from(footer).map(b => (b.textContent ?? "").trim().split("\n")[0].slice(0, 22) + ((b as HTMLButtonElement).disabled ? "[DIS]" : ""))));
  log("page says 'Sent.':", (document.body.textContent ?? "").includes("Sent."));
  log("Approve reachable again:", Boolean(byText("Approve & send")));
  await sleep(1500);
  log("after another refetch -> Approve reachable:", Boolean(byText("Approve & send")));
  log("DONE");
  flush();
}
void main();
