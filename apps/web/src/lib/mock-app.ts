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

export interface QueueItem {
  id: string;
  propertyId: string;
  unit: string;
  propertyLabel: string;
  tenantFirst: string;
  tenantPhoneMasked: string;
  severity: Severity;
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
}

export const queue: QueueItem[] = [
  {
    id: "c-maria-flood",
    propertyId: "main4",
    unit: "Unit 4 · Maria",
    propertyLabel: "123 Main #4",
    tenantFirst: "Maria",
    tenantPhoneMasked: "+1 905 ●●● 4421",
    severity: "emergency",
    receivedAgo: "2 min ago",
    tenantMessage: "There's water flooding from the ceiling — it's getting on the bed!!",
    hasPhoto: true,
    draftReply:
      "Maria — this is an emergency. I'm contacting your landlord right now. If anyone is in danger or it's gas/fire, call 911. Move valuables and turn off the water main if you can.",
    title: "Water coming through the ceiling — Unit 4",
  },
  {
    id: "c-jesse-sink",
    propertyId: "walmer2",
    unit: "Unit 2 · Jesse",
    propertyLabel: "Walmer Unit 2",
    tenantFirst: "Jesse",
    tenantPhoneMasked: "+1 416 ●●● 7421",
    severity: "urgent",
    receivedAgo: "17 min ago",
    tenantMessage: "Kitchen sink is pooling water underneath — started this morning.",
    hasPhoto: true,
    photoCaption: "Photo — water pooling under the kitchen sink trap",
    draftReply:
      "Hi Jesse — thanks for the photo. That looks like a slow leak from the trap. I'll have Mike from Mike's Plumbing reach out today to take a look. In the meantime, put a bucket under the trap and try not to use that sink. I'll confirm the window once Mike replies.",
    why: "A slow leak isn't dangerous, but water sitting under a cabinet can do real damage — that's enough to need you today, so it's in your queue instead of ringing your phone.",
    title: "Kitchen sink leaking under the cabinet — Unit 2",
  },
  {
    id: "c-sam-garbage",
    propertyId: "stoop",
    unit: "Unit A · Sam",
    propertyLabel: "Stoop House",
    tenantFirst: "Sam",
    tenantPhoneMasked: "+1 647 ●●● 2210",
    severity: "routine",
    receivedAgo: "1 hr ago",
    tenantMessage: "When's the next garbage day? Just moved in.",
    hasPhoto: false,
    draftReply:
      "Welcome Sam! Garbage and recycling go out Tuesday night for Wednesday pickup. Green bin every week, blue bin every other week (next pickup is the 4th). Bins live by the side gate.",
    why: "A garbage-day question isn't urgent, and the answer's already in your house rules — so I drafted it straight from those instead of bothering you.",
    title: "Asking when garbage day is — Unit A",
  },
];

export function getConversation(id: string) {
  return queue.find((q) => q.id === id) ?? queue[1];
}

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
