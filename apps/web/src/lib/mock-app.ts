import type { Severity } from "@/components/stoop/SeverityBadge";

export interface Property {
  id: string;
  nickname: string;
  address: string;
}

export const properties: Property[] = [
  { id: "main4", nickname: "123 Main #4", address: "123 Main St #4, Oakville ON" },
  { id: "walmer2", nickname: "Walmer Unit 2", address: "47 Walmer Rd Unit 2, Toronto ON" },
  { id: "stoop", nickname: "Stoop House", address: "88 Lansdowne Ave, Toronto ON" },
];

/**
 * `cases.status` (schema-v1.md) / `CaseSummary.status` (api-contracts.md
 * "Cases"). The conversation thread reads this to decide whether the
 * severity plaque still belongs in its header — a resolved case has
 * nothing left to flag.
 */
export type CaseStatus = "open" | "awaiting_approval" | "awaiting_tenant" | "resolved" | "reopened";

/**
 * One photo attached to a timeline message. `url` / `contentType` mirror
 * `messages.media` jsonb (`schema-v1.md`, `[{url, content_type}]`).
 * `caption` is a mock-only addition — see the contract-gap note on
 * `TimelineMessageEntry` below.
 */
export interface TimelineMedia {
  url: string;
  contentType: string;
  caption: string;
}

/**
 * `GET /v1/cases/{id}` returns an interleaved `timeline` of message / audit
 * / draft entries (api-contracts.md "Cases"). These three types mirror
 * that shape field-for-field so the conversation screen reads the same
 * names the real endpoint will use.
 */
export interface TimelineMessageEntry {
  kind: "message";
  direction: "inbound" | "outbound";
  /** `messages.party` — always "tenant" for a tenant channel; outbound
   * rows are Stoop sending on the landlord's behalf, never a separate
   * "landlord" row (that value is reserved for approve-by-SMS command
   * messages, which never appear in a tenant-facing thread). */
  party: "tenant";
  body: string;
  /**
   * CONTRACT GAP: `messages.media` is documented as `[{url, content_type}]`
   * only (schema-v1.md) — no per-photo caption/alt text field. The queue
   * card has an agent-written `media_note` (api-contracts.md v1.1) for
   * exactly this purpose, but nothing equivalent is specified for
   * `GET /v1/cases/{id}`'s timeline messages, and the Clarity thread's
   * photo chip needs plain-English text next to every attachment, not
   * just on the queue card. Flagged for a follow-up amendment; mock data
   * adds `caption` here to show the shape the UI needs.
   */
  media: TimelineMedia[];
  /** Display time, e.g. "8:14 AM". Static mock string — never computed
   * from `Date` at render time (see the conversation route's SSR note). */
  at: string;
  /** Set only on the first entry of a new day group — renders the
   * "Today" / "Yesterday" stamp above this message. Mock-only grouping
   * key; the real endpoint would derive this from `at` (a timestamptz)
   * client-side once real data lands. */
  dayLabel?: string;
}

export interface TimelineDraftEntry {
  kind: "draft";
  /**
   * CONTRACT GAP: the `GET /v1/cases/{id}` example in api-contracts.md
   * shows a draft timeline entry as `{ kind, status, body, at }` with no
   * `id` — but approve/reject/edit-and-send all operate on
   * `/v1/drafts/{id}/...`. The timeline's draft entry needs an id field
   * to be actionable from the full-conversation view. Flagged for a
   * follow-up amendment; mock data includes `draftId` to show the shape.
   */
  draftId: string;
  status: "pending";
  body: string;
  at: string;
  dayLabel?: string;
}

export interface TimelineAuditEntry {
  kind: "audit";
  /** api-contracts.md's `GET /v1/cases/{id}` example: `"actor": "agent"`. */
  actor: "agent";
  action: "classified";
  payload: {
    severity: Severity;
    /** Terse rule-fragment audit trail (api-contracts.md's queue example:
     * `rules_fired`) — kept even though this screen doesn't render it,
     * so the mock payload shape matches the doc, not just the one field
     * the UI happens to use. */
    rules_fired: string[];
    /**
     * The plain-English `summary` key schema-v1's v1.7 amendment adds to
     * the `audit_log` 'classified' payload — same source as the queue
     * card's `why`, rendered here as the thread's always-visible margin
     * note.
     *
     * CONTRACT GAP: the `GET /v1/cases/{id}` example in api-contracts.md
     * predates v1.7 and shows this payload as only `{severity,
     * rules_fired}` — it doesn't yet show `summary` on the *timeline's*
     * audit payload (only the flatter `/v1/queue` card's `why` is
     * documented as reading it). Whether the cases-detail endpoint's
     * audit entry also carries `summary` needs to go in the next
     * contract amendment; mock data assumes yes, since the thread's
     * margin note has nowhere else to read it from.
     */
    summary: string;
  };
  at: string;
}

export type TimelineEntry = TimelineMessageEntry | TimelineDraftEntry | TimelineAuditEntry;

export interface QueueItem {
  id: string;
  propertyId: string;
  unit: string;
  /** Clean "Unit N" label for headers that shouldn't parse `unit` apart
   * from the tenant name baked into it (e.g. "Unit 4 · Maria"). */
  unitLabel: string;
  propertyLabel: string;
  tenantFirst: string;
  tenantPhoneMasked: string;
  severity: Severity;
  status: CaseStatus;
  receivedAgo: string;
  tenantMessage: string;
  hasPhoto: boolean;
  /** Short caption for the attached photo, shown next to the tenant's text. */
  photoCaption?: string;
  draftReply: string;
  /** Plain-English reason the draft/severity is what it is — shown as the
   * queue's margin note (docs/mockups/07-clarity-redesign.html `.margin-note`). */
  why?: string;
  /** Plain-English one-line description of the situation — e.g. the
   * emergency banner's headline. Modeled as data (founder decision,
   * 2026-07-06) instead of composing copy like `${tenantFirst} reported a
   * flood.` in the component, which can't describe any other kind of case. */
  title: string;
  /** `GET /v1/cases/{id}`-shaped history for the conversation thread
   * screen — oldest first. Deliberately separate from the fields above
   * (which mirror the flatter `GET /v1/queue` card) rather than derived
   * from them, because the two are genuinely different endpoints with
   * genuinely different shapes (api-contracts.md "Queue" vs "Cases"). */
  timeline: TimelineEntry[];
}

const mariaSafetyReply =
  "Maria — this is an emergency. I'm contacting your landlord right now. If anyone is in danger or it's gas/fire, call 911. Move valuables and turn off the water main if you can.";
const mariaWhy =
  "Water actively coming through the ceiling is a safety and property risk right now, so I skipped your queue, sent Maria those safety steps immediately, and started calling you.";

const jesseDraft =
  "Hi Jesse — thanks for the photo. That looks like a slow leak from the trap. I'll have Mike from Mike's Plumbing reach out today to take a look. In the meantime, put a bucket under the trap and try not to use that sink. I'll confirm the window once Mike replies.";
const jesseWhy =
  "A slow leak isn't dangerous, but water sitting under a cabinet can do real damage — that's enough to need you today, so it's in your queue instead of ringing your phone.";
const jessePhotoCaption = "Photo — water pooling under the kitchen sink trap";

const samDraft =
  "Welcome Sam! Garbage and recycling go out Tuesday night for Wednesday pickup. Green bin every week, blue bin every other week (next pickup is the 4th). Bins live by the side gate.";
const samWhy =
  "A garbage-day question isn't urgent, and the answer's already in your house rules — so I drafted it straight from those instead of bothering you.";

export const queue: QueueItem[] = [
  {
    id: "c-maria-flood",
    propertyId: "main4",
    unit: "Unit 4 · Maria",
    unitLabel: "Unit 4",
    propertyLabel: "123 Main #4",
    tenantFirst: "Maria",
    tenantPhoneMasked: "+1 905 ●●● 4421",
    severity: "emergency",
    status: "open",
    receivedAgo: "2 min ago",
    tenantMessage: "There's water flooding from the ceiling — it's getting on the bed!!",
    hasPhoto: true,
    draftReply: mariaSafetyReply,
    why: mariaWhy,
    title: "Water coming through the ceiling — Unit 4",
    timeline: [
      {
        kind: "message",
        direction: "inbound",
        party: "tenant",
        body: "There's water flooding from the ceiling — it's getting on the bed!!",
        media: [
          {
            url: "#",
            contentType: "image/jpeg",
            caption: "Photo — water spreading across the ceiling above the bed",
          },
        ],
        at: "12:47 AM",
        dayLabel: "Today",
      },
      {
        kind: "message",
        direction: "outbound",
        party: "tenant",
        body: mariaSafetyReply,
        media: [],
        at: "12:47 AM",
      },
      {
        kind: "audit",
        actor: "agent",
        action: "classified",
        payload: {
          severity: "emergency",
          rules_fired: ["active water intrusion", "safety risk unresolved by tenant"],
          summary: mariaWhy,
        },
        at: "12:47 AM",
      },
    ],
  },
  {
    id: "c-jesse-sink",
    propertyId: "walmer2",
    unit: "Unit 2 · Jesse",
    unitLabel: "Unit 2",
    propertyLabel: "Walmer Unit 2",
    tenantFirst: "Jesse",
    tenantPhoneMasked: "+1 416 ●●● 7421",
    severity: "urgent",
    status: "awaiting_approval",
    receivedAgo: "17 min ago",
    tenantMessage: "Kitchen sink is pooling water underneath — started this morning.",
    hasPhoto: true,
    photoCaption: jessePhotoCaption,
    draftReply: jesseDraft,
    why: jesseWhy,
    title: "Kitchen sink leaking under the cabinet — Unit 2",
    timeline: [
      {
        kind: "message",
        direction: "inbound",
        party: "tenant",
        body: "Kitchen sink is pooling water underneath — started this morning.",
        media: [],
        at: "8:12 AM",
        dayLabel: "Today",
      },
      {
        kind: "message",
        direction: "outbound",
        party: "tenant",
        body: "Thanks for flagging it — can you send a quick photo?",
        media: [],
        at: "8:14 AM",
      },
      {
        kind: "message",
        direction: "inbound",
        party: "tenant",
        body: "Here you go — it's coming from underneath, not a ton but steady.",
        media: [{ url: "#", contentType: "image/jpeg", caption: jessePhotoCaption }],
        at: "8:20 AM",
      },
      {
        kind: "message",
        direction: "outbound",
        party: "tenant",
        body: "How much water is pooling under there?",
        media: [],
        at: "8:21 AM",
      },
      {
        kind: "draft",
        draftId: "d-jesse-sink-1",
        status: "pending",
        body: jesseDraft,
        at: "8:22 AM",
      },
      {
        kind: "audit",
        actor: "agent",
        action: "classified",
        payload: {
          severity: "urgent",
          rules_fired: ["water damage risk, not active flooding", "next-day vendor slot available"],
          summary: jesseWhy,
        },
        at: "8:22 AM",
      },
    ],
  },
  {
    id: "c-sam-garbage",
    propertyId: "stoop",
    unit: "Unit A · Sam",
    unitLabel: "Unit A",
    propertyLabel: "Stoop House",
    tenantFirst: "Sam",
    tenantPhoneMasked: "+1 647 ●●● 2210",
    severity: "routine",
    status: "awaiting_approval",
    receivedAgo: "1 hr ago",
    tenantMessage: "When's the next garbage day? Just moved in.",
    hasPhoto: false,
    draftReply: samDraft,
    why: samWhy,
    title: "Asking when garbage day is — Unit A",
    timeline: [
      {
        kind: "message",
        direction: "inbound",
        party: "tenant",
        body: "When's the next garbage day? Just moved in.",
        media: [],
        at: "6:40 PM",
        dayLabel: "Yesterday",
      },
      {
        kind: "draft",
        draftId: "d-sam-garbage-1",
        status: "pending",
        body: samDraft,
        at: "6:42 PM",
      },
      {
        kind: "audit",
        actor: "agent",
        action: "classified",
        payload: {
          severity: "routine",
          rules_fired: ["non-emergency admin question", "answer already in house rules"],
          summary: samWhy,
        },
        at: "6:42 PM",
      },
    ],
  },
];

/** Returns `undefined` for an unknown id — callers (route loaders) are
 * responsible for turning that into a 404. No silent fallback: a prior
 * version of this function defaulted to `queue[1]`, which meant an
 * invalid id in the URL rendered Jesse's conversation instead of a real
 * 404 (found while wiring this into the conversation thread route). */
export function getConversation(id: string): QueueItem | undefined {
  return queue.find((q) => q.id === id);
}

/** Fallback margin-note copy for a case with no agent-written `why` yet
 * (see the v1.1 amendment note on `TimelineAuditEntry.payload.summary`
 * above — rows classified before that field shipped have `why: null`).
 * Shared by Home's
 * queue and the conversation thread so the same case never shows two
 * different fallback sentences depending which screen you view it from. */
export const DEFAULT_WHY = "I drafted this from your house rules and past replies.";

export interface HandledNoteData {
  tenantFirst: string;
  /** The bold lead sentence — what Stoop already answered on its own. */
  summary: string;
  /** The explanation that follows, inside the note's dashed box. */
  explanation: string;
}

/**
 * The "I handled this myself" note on Home — modeled as data (the same
 * reason `why` above is data) so the copy can't drift out of sync with
 * which tenant's case is actually still open. Sam's garbage-day question
 * is fully resolved (routine, answered from house rules); Jesse's sink
 * leak is still an open, unapproved draft, so it can never be used here.
 */
export const handledNote: HandledNoteData = {
  tenantFirst: "Sam",
  summary: "Sam also asked about weekend parking.",
  explanation:
    "I answered that one myself from your house rules — you've approved enough of these that I don't need to ask about the simple ones.",
};
