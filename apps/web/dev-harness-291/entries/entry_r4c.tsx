import { Component, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClientProvider, QueryClient } from "@tanstack/react-query";
import { createQueryClient } from "@/api/queryClient";
import { Route as ThreadRoute } from "@/routes/app.conversations.$id";
import { NOTICES } from "sonner";
import { resetUnverifiedSendStore } from "@/features/queue/unverifiedSendStore";

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
  editStatus: 200,
};
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", date: new Date().toUTCString() } });
globalThis.fetch = (async (input: any, init: any = {}) => {
  const url = String(input); const method = init.method ?? "GET";
  if (url.endsWith("/v1/queue")) return json(200, { items: SERVER.items, counts: { total: SERVER.items.length, awaiting_tenant: 0 } });
  if (url.includes("/v1/cases/")) {
    const snapDraft = SERVER.caseDraft ? { ...SERVER.caseDraft } : null;
    const d = (SERVER as any).caseDelayMs ?? 0;
    if (d) await sleep(d);
    return json(200, {
      id: "case-x", status: "awaiting_approval", severity: "routine", title: "Tap drips",
      tenant: { id: "t1", name: "Maria Ortiz", unit: "2" },
      property: { id: "p1", label: "12 Ossington", address_line1: "12 Ossington Ave" },
      created_at: new Date(Date.now() - 7200_000).toISOString(), last_activity_at: new Date().toISOString(),
      timeline: [
        { kind: "message", id: "m1", direction: "inbound", party: "tenant", body: "Kitchen tap drips.", media: [], at: new Date(Date.now() - 3600_000).toISOString() },
        ...(snapDraft ? [{ kind: "draft", ...snapDraft, at: new Date(Date.now() - 60_000).toISOString(), recipient: "tenant" }] : []),
      ],
    });
  }
  const ap = url.match(/\/v1\/drafts\/([^/]+)\/approve$/);
  if (ap && method === "POST") {
    (SERVER as any).approves = ((SERVER as any).approves ?? 0) + 1;
    SERVER.caseDraft = { ...SERVER.caseDraft, status: "sent" };
    SERVER.items = [];
    const at = new Date(Date.now() + 5000).toISOString();
    return json(200, { status: "approved", scheduled_send_at: at, undo_until: at });
  }
  const e = url.match(/\/v1\/drafts\/([^/]+)\/edit-and-send$/);
  if (e && method === "POST") return json(SERVER.editStatus, { error: { code: "server_error", message: "x", request_id: "r" } });
  return json(200, {});
}) as any;
const Thread = (ThreadRoute as any).options.component as () => any;
let qc: QueryClient = createQueryClient(); let root: Root;

const byText = (t: string) => Array.from(document.querySelectorAll("button")).find(b => (b.textContent ?? "").includes(t)) as HTMLButtonElement | undefined;
class Boundary extends Component<{ children: ReactNode }, { err: string | null }> {
  state = { err: null as string | null };
  static getDerivedStateFromError(e: any) { return { err: String(e?.stack ?? e) }; }
  render() { return this.state.err ? <pre>BOUNDARY: {this.state.err}</pre> : this.props.children; }
}
function render() { root.render(<QueryClientProvider client={qc}><Boundary><Thread /></Boundary></QueryClientProvider>); }

async function main() {
  root = createRoot(document.getElementById("app")!);
  resetUnverifiedSendStore();
  SERVER.items = [routine("draft-1", "ORIGINAL")];
  SERVER.caseDraft = { id: "draft-1", body: "ORIGINAL", status: "pending" };
  render();
  await sleep(450);
  const footer = () => (document.querySelector("main")?.textContent ?? "").replace(/\s+/g, " ");
  const ctrls = () => JSON.stringify(Array.from(document.querySelectorAll("button")).map(b => (b.textContent ?? "").trim().split("\n")[0].slice(0,20) + (b.disabled ? "[DIS]" : "[en]")));
  log("=== R4-C: thread route, case read in flight ACROSS an approve ===");
  log("controls:", ctrls());
  (SERVER as any).caseDelayMs = 1500;
  void qc.invalidateQueries({ queryKey: ["case", "case-x"] });
  await sleep(150);
  log("approving while the case read is still in flight...");
  byText("Approve")!.click();
  await sleep(500);
  log("t+0.5s ->", ctrls());
  log("server approves:", (SERVER as any).approves, "| server draft status now:", SERVER.caseDraft.status);
  (SERVER as any).caseDelayMs = 0;
  for (let i = 0; i < 8; i++) { await sleep(1000); log("t+" + (0.5 + i + 1).toFixed(1) + "s ->", ctrls()); }
  log("");
  log("footer text:", JSON.stringify(footer().slice(-160)));
  const ap = byText("Approve");
  log("Approve live again on an already-sent reply:", Boolean(ap && !ap.disabled));
  log("DONE");
  flush();
}
void main();
