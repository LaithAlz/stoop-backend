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
const SERVER = { items: [] as Card[], editStatus: 500, rejects: 0 };
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", date: new Date().toUTCString() } });
globalThis.fetch = (async (input: any, init: any = {}) => {
  const url = String(input); const method = init.method ?? "GET";
  if (url.endsWith("/v1/queue")) return json(200, { items: SERVER.items, counts: { total: SERVER.items.length, awaiting_tenant: 0 } });
  const e = url.match(/\/v1\/drafts\/([^/]+)\/edit-and-send$/);
  if (e && method === "POST") return json(SERVER.editStatus, { error: { code: "server_error", message: "x", request_id: "r" } });
  return json(200, {});
}) as any;
const Home = (HomeRoute as any).options.component as () => any;
let qc: QueryClient = createQueryClient(); let root: Root;
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
const btns = () => Array.from(document.querySelectorAll("article button")) as HTMLButtonElement[];
const byText = (t: string) => btns().find((e) => (e.textContent ?? "").includes(t));
const state = () => JSON.stringify(btns().map(b => (b.textContent ?? "").trim().split("\n")[0].slice(0,22) + (b.disabled ? "[DIS]" : "[en]")));
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
  log("=== give-up ceiling fires while the EDITOR IS STILL OPEN with typed text ===");
  byText("Edit")!.click(); await sleep(60);
  const ta = document.querySelector("textarea") as HTMLTextAreaElement;
  const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value")!.set!;
  setter.call(ta, "TWO MINUTES OF TYPED REPLY"); ta.dispatchEvent(new Event("input", { bubbles: true }));
  await sleep(50);
  (Array.from(document.querySelectorAll("button")).find(b => (b.textContent ?? "").includes("Send edited version")) as HTMLButtonElement).click();
  await sleep(400);
  log("editor still mounted after the ambiguous failure:", Boolean(document.querySelector("textarea")));
  log("textarea value right after the failure:", JSON.stringify((document.querySelector("textarea") as HTMLTextAreaElement)?.value));
  NOTICES.length = 0;
  CLOCK.off = 121_000;
  await sleep(1600);
  log("");
  log("--- ceiling has fired while editor was still open ---");
  log("toast:", JSON.stringify(NOTICES));
  log("editor STILL mounted (typed text not discarded):", Boolean(document.querySelector("textarea")));
  log("textarea value survived:", JSON.stringify((document.querySelector("textarea") as HTMLTextAreaElement)?.value));
  const sendBtn = Array.from(document.querySelectorAll("button")).find(b => (b.textContent ?? "").includes("Send edited version")) as HTMLButtonElement;
  log("Send re-enabled:", sendBtn ? !sendBtn.disabled : "no send button");
  log("DONE");
  flush();
}
void main();
