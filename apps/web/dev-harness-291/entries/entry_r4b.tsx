import { Component, type ReactNode } from "react";
import { createRoot, type Root } from "react-dom/client";
import { QueryClientProvider, QueryClient } from "@tanstack/react-query";
import { createQueryClient } from "@/api/queryClient";
import { Route as HomeRoute } from "@/routes/app.index";
import { NOTICES } from "sonner";
import {
  UNVERIFIED_SEND_NOTICE,
  UNVERIFIED_GIVE_UP_CARD_NOTICE,
} from "@/components/clarity/EditDraftPanel";

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
const SERVER = { items: [] as Card[], editStatus: 500, edits: 0 };
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), {
    status, headers: { "content-type": "application/json", date: new Date().toUTCString() },
  });
globalThis.fetch = (async (input: any, init: any = {}) => {
  const url = String(input); const method = init.method ?? "GET";
  if (url.endsWith("/v1/queue"))
    return json(200, { items: SERVER.items, counts: { total: SERVER.items.length, awaiting_tenant: 0 } });
  const e = url.match(/\/v1\/drafts\/([^/]+)\/edit-and-send$/);
  if (e && method === "POST") {
    SERVER.edits += 1;
    return json(SERVER.editStatus, { error: { code: "server_error", message: "x", request_id: "r" } });
  }
  return json(200, {});
}) as any;
const Home = (HomeRoute as any).options.component as () => any;
let qc: QueryClient = createQueryClient(); let root: Root;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const bodyText = () => (document.body.textContent ?? "").replace(/\s+/g, " ");
const has = (s: string) => bodyText().includes(s.replace(/\s+/g, " "));
class Boundary extends Component<{ children: ReactNode }, { err: string | null }> {
  state = { err: null as string | null };
  static getDerivedStateFromError(e: any) { return { err: String(e?.stack ?? e) }; }
  render() { return this.state.err ? <pre>BOUNDARY: {this.state.err}</pre> : this.props.children; }
}
function render() {
  root.render(<QueryClientProvider client={qc}><Boundary><Home /></Boundary></QueryClientProvider>);
}
const sendBtn = () =>
  Array.from(document.querySelectorAll("button")).find((b) =>
    (b.textContent ?? "").includes("Send edited version")) as HTMLButtonElement | undefined;

async function main() {
  root = createRoot(document.getElementById("app")!);
  SERVER.items = [routine("p1", "ORIGINAL DRAFT BODY")];
  render();
  await sleep(350);
  const CLOCK = { off: 0 };
  const realNow = Date.now.bind(Date);
  Date.now = () => realNow() + CLOCK.off;

  log("=== R4-B: 120s give-up ceiling, EDITOR LEFT OPEN (round 3's new behavior) ===");
  (Array.from(document.querySelectorAll("article button")).find((b) =>
    (b.textContent ?? "").includes("Edit")) as HTMLButtonElement).click();
  await sleep(60);
  const ta = document.querySelector("textarea") as HTMLTextAreaElement;
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
  setter.call(ta, "MARIA, THE PLUMBER COMES TUESDAY 9AM");
  ta.dispatchEvent(new Event("input", { bubbles: true }));
  await sleep(40);
  sendBtn()!.click();
  await sleep(400);
  log("after the ambiguous 500:");
  log("  editor open:", Boolean(document.querySelector("textarea")),
      "| Send disabled:", sendBtn()?.disabled);
  log("  UNVERIFIED_SEND_NOTICE on screen:", has(UNVERIFIED_SEND_NOTICE));

  NOTICES.length = 0;
  CLOCK.off = 121_000;
  await sleep(1800);
  log("");
  log("--- ceiling fired, landlord is away, editor still open ---");
  log("  toast fired:", JSON.stringify(NOTICES));
  log("  editor still open:", Boolean(document.querySelector("textarea")));
  log("  typed text intact:", JSON.stringify((document.querySelector("textarea") as HTMLTextAreaElement)?.value));
  log("  Send re-enabled:", sendBtn() ? !sendBtn()!.disabled : "n/a");
  log("  UNVERIFIED_SEND_NOTICE on screen:", has(UNVERIFIED_SEND_NOTICE));
  log("  STICKY give-up notice on screen:", has(UNVERIFIED_GIVE_UP_CARD_NOTICE));
  log("  any 'couldn' / 'confirm' text anywhere on screen:",
      JSON.stringify(bodyText().match(/[^.]*confirm[^.]*\./gi) ?? []));

  log("");
  log("--- now the landlord taps Cancel (closes the editor) ---");
  (Array.from(document.querySelectorAll("button")).find((b) =>
    (b.textContent ?? "").trim() === "Cancel") as HTMLButtonElement).click();
  await sleep(200);
  log("  STICKY give-up notice on screen after Cancel:", has(UNVERIFIED_GIVE_UP_CARD_NOTICE));

  log("");
  log("--- re-open the editor (the landlord's own next look at the draft) ---");
  (Array.from(document.querySelectorAll("article button")).find((b) =>
    (b.textContent ?? "").includes("Edit")) as HTMLButtonElement).click();
  await sleep(120);
  log("  STICKY give-up notice on screen while editing again:", has(UNVERIFIED_GIVE_UP_CARD_NOTICE));
  log("  Send live:", sendBtn() ? !sendBtn()!.disabled : "n/a");
  log("  server edit-and-send calls so far:", SERVER.edits);
  log("DONE");
  flush();
}
void main();
