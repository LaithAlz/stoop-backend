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
  draftReply: string;
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
    tenantMessage:
      "There's water flooding from the ceiling — it's getting on the bed!!",
    hasPhoto: true,
    draftReply:
      "Maria — this is an emergency. I'm contacting your landlord right now. If anyone is in danger or it's gas/fire, call 911. Move valuables and turn off the water main if you can.",
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
    tenantMessage:
      "Kitchen sink is pooling water underneath — started this morning.",
    hasPhoto: true,
    draftReply:
      "Hi Jesse — thanks for the photo. That looks like a slow leak from the trap. I'll have Mike from Mike's Plumbing reach out today to take a look. In the meantime, put a bucket under the trap and try not to use that sink. I'll confirm the window once Mike replies.",
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
  },
];

export function getConversation(id: string) {
  return queue.find((q) => q.id === id) ?? queue[1];
}
