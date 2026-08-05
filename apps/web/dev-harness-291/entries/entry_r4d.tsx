import { Component, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClientProvider, QueryClient } from "@tanstack/react-query";
import { createQueryClient } from "@/api/queryClient";
import { Route as ThreadRoute } from "@/routes/app.conversations.$id";
import { NOTICES } from "sonner";
import { resetUnverifiedSendStore } from "@/features/queue/unverifiedSendStore";
import {
  UNVERIFIED_SEND_NOTICE,
  UNVERIFIED_GIVE_UP_CARD_NOTICE,
} from "@/components/clarity/EditDraftPanel";

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
  caseDraft: { id: "draft-1", body: "BODY P", status: "pending" } as any,
  editStatus: 500,
};
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", date: new Date().toUTCString() } });
globalThis.fetch = (async (input: any, init: any = {}) => {
  const url = String(input); const method = init.method ?? "GET";
  if (url.endsWith("/v1/queue")) return json(200, { items: SERVER.items, counts: { total: SERVER.items.length, awaiting_tenant: 0 } });
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
  const e = url.match(/\/v1\/drafts\/([^/]+)\/edit-and-send$/);
  if (e && method === "POST") return json(SERVER.editStatus, { error: { code: "server_error", message: "x", request_id: "r" } });
  return json(200, {});
}) as any;
const Thread = (ThreadRoute as any).options.component as () => any;
let qc: QueryClient = createQueryClient(); let root: Root;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const byText = (t: string) => Array.from(document.querySelectorAll("button")).find(b => (b.textContent ?? "").includes(t)) as HTMLButtonElement | undefined;
const bodyText = () => (document.body.textContent ?? "").replace(/\s+/g, " ");
const has = (s: string) => bodyText().includes(s.replace(/\s+/g, " "));
class Boundary extends Component<{ children: ReactNode }, { err: string | null }> {
  state = { err: null as string | null };
  static getDerivedStateFromError(e: any) { return { err: String(e?.stack ?? e) }; }
  render() { return this.state.err ? <pre>BOUNDARY: {this.state.err}</pre> : this.props.children; }
}
function render() { root.render(<QueryClientProvider client={qc}><Boundary><Thread /></Boundary></QueryClientProvider>); }

async function main() {
  root = createRoot(document.getElementById("app")!);
  resetUnverifiedSendStore();
  SERVER.items = [routine("draft-1", "BODY P")];
  SERVER.caseDraft = { id: "draft-1", body: "BODY P", status: "pending" };
  SERVER.editStatus = 500;
  render();
  await sleep(400);
  const CLOCK = { off: 0 };
  const realNow = Date.now.bind(Date);
  Date.now = () => realNow() + CLOCK.off;

  log("=== THREAD R4-D: 120s give-up ceiling, EDITOR LEFT OPEN (mirrors R4-B on Home) ===");
  byText("Edit")!.click();
  await sleep(80);
  const ta = document.querySelector("textarea") as HTMLTextAreaElement;
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
  setter.call(ta, "MARIA, THE PLUMBER COMES TUESDAY 9AM");
  ta.dispatchEvent(new Event("input", { bubbles: true }));
  await sleep(50);
  byText("Send edited version")!.click();
  await sleep(400);
  log("after the ambiguous 500:");
  log("  editor open:", Boolean(document.querySelector("textarea")),
      "| Send disabled:", (byText("Send edited version") as HTMLButtonElement | undefined)?.disabled);
  log("  UNVERIFIED_SEND_NOTICE on screen:", has(UNVERIFIED_SEND_NOTICE));

  NOTICES.length = 0;
  CLOCK.off = 121_000;
  await sleep(1800);
  log("");
  log("--- ceiling fired, landlord is away, editor still open ---");
  log("  toast fired:", JSON.stringify(NOTICES));
  log("  editor still open:", Boolean(document.querySelector("textarea")));
  log("  typed text intact:", JSON.stringify((document.querySelector("textarea") as HTMLTextAreaElement)?.value));
  const sendNow = byText("Send edited version") as HTMLButtonElement | undefined;
  log("  Send re-enabled:", sendNow ? !sendNow.disabled : "n/a");
  log("  STICKY give-up notice on screen:", has(UNVERIFIED_GIVE_UP_CARD_NOTICE));
  log("  any 'couldn' / 'confirm' text anywhere on screen:",
      JSON.stringify(bodyText().match(/[^.]*confirm[^.]*\./gi) ?? []));

  log("DONE");
  flush();
}
void main();
