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

// The scenario: the DELETE (undo) APPLIES server-side (the draft goes back
// to pending, so it reappears in GET /v1/queue) but the RESPONSE is lost.
const SERVER = {
  items: [] as Card[],
  undoAppliesThenDropsConnection: false,
  fetches: [] as string[],
};
const json = (status: number, body: unknown) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json", date: new Date().toUTCString() } });
globalThis.fetch = (async (input: any, init: any = {}) => {
  const url = String(input);
  const method = init.method ?? "GET";
  SERVER.fetches.push(`${method} ${url.replace("http://api.test", "")}`);
  if (url.endsWith("/v1/queue"))
    return json(200, { items: SERVER.items, counts: { total: SERVER.items.length, awaiting_tenant: 0 } });
  const m = url.match(/\/v1\/drafts\/([^/]+)\/approve$/);
  if (m && method === "POST") {
    SERVER.items = SERVER.items.filter((i) => i.draft_id !== m[1]);
    return json(200, { draft_id: m[1], status: "approved", undo_until: new Date(Date.now() + 5000).toISOString(), scheduled_send_at: new Date(Date.now() + 5000).toISOString() });
  }
  if (m && method === "DELETE") {
    if (SERVER.undoAppliesThenDropsConnection) {
      // the server COMMITS the undo (draft back to pending, back in the queue)
      SERVER.items = [routine(m[1], "BODY P"), ...SERVER.items.filter((i) => i.draft_id !== m[1])];
      throw new TypeError("Failed to fetch"); // ...and the response never arrives
    }
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
const cardText = () => (document.querySelector("article")?.textContent ?? "(no card)");
const controls = () => Array.from(document.querySelectorAll("article button")).map((b) => (b.textContent ?? "").trim().split("\n")[0] + (((b as HTMLButtonElement).disabled) ? "[disabled]" : ""));

class Boundary extends Component<{ children: ReactNode }, { err: string | null }> {
  state = { err: null as string | null };
  static getDerivedStateFromError(e: any) { return { err: String(e?.stack ?? e) }; }
  render() { return this.state.err ? <pre>BOUNDARY: {this.state.err}</pre> : this.props.children; }
}
function render() { root.render(<QueryClientProvider client={qc}><Boundary><Home /></Boundary></QueryClientProvider>); }

async function main() {
  root = createRoot(document.getElementById("app")!);
  log("=== undo APPLIES server-side, response lost: where does Home land? ===");
  SERVER.items = [routine("p1", "BODY P")];
  render();
  await sleep(250);
  log("card present:", document.querySelectorAll("article").length === 1);
  byText("Approve & send")!.click();
  for (let w = 0; w < 3000 && undoBtns().length !== 1; w += 20) await sleep(20);
  log("undo ticket up:", undoBtns().length === 1);
  SERVER.undoAppliesThenDropsConnection = true;
  NOTICES.length = 0;
  undoBtns()[0].click();
  await sleep(500);
  log("after the ambiguous undo -> notice:", JSON.stringify(NOTICES));
  log("after the ambiguous undo -> server queue now lists p1 again (undo DID apply):", SERVER.items.map((i) => i.draft_id));
  log("after the ambiguous undo -> card still shows the undo ticket:", undoBtns().length === 1);
  // let the 5s countdown run out
  await sleep(6000);
  log("");
  log("--- 6s later, the countdown has expired ---");
  log("card text:", JSON.stringify(cardText().replace(/\s+/g, " ")));
  log("controls on the card:", JSON.stringify(controls()));
  log("says 'Sent.':", cardText().includes("Sent."));
  // force a completely fresh queue read, the way the 20s poll would
  await qc.invalidateQueries();
  await sleep(500);
  log("");
  log("--- after a forced fresh GET /v1/queue that lists p1 as PENDING ---");
  log("server says p1 is pending and in the queue:", SERVER.items.some((i) => i.draft_id === "p1"));
  log("card text:", JSON.stringify(cardText().replace(/\s+/g, " ")));
  log("controls on the card:", JSON.stringify(controls()));
  await qc.invalidateQueries();
  await sleep(600);
  await qc.invalidateQueries();
  await sleep(600);
  log("");
  log("--- after two more fresh reads ---");
  log("controls on the card:", JSON.stringify(controls()));
  log("still says 'Sent.':", cardText().includes("Sent."));
  log("counts strip:", JSON.stringify((document.querySelector("main")?.parentElement?.textContent ?? "").replace(/\s+/g," ").slice(0,120)));
  log("DONE");
  flush();
}
void main();
