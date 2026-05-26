export type AutonomyMode = "shadow" | "auto-routine" | "auto-urgent" | "full-auto";

export interface AutonomyModeMeta {
  key: AutonomyMode;
  label: string;
  description: string;
  requirement: string;
}

export const autonomyModes: AutonomyModeMeta[] = [
  {
    key: "shadow",
    label: "Shadow Mode",
    description: "The agent drafts every reply. You approve before anything sends.",
    requirement: "Starting point. No streak required.",
  },
  {
    key: "auto-routine",
    label: "Auto-Routine",
    description: "Routine replies send on their own. You still approve urgent and emergency drafts.",
    requirement: "10 unedited approvals in a row.",
  },
  {
    key: "auto-urgent",
    label: "Auto-Urgent",
    description: "Routine and urgent replies send on their own. You approve emergencies.",
    requirement: "25 unedited approvals in Auto-Routine.",
  },
  {
    key: "full-auto",
    label: "Full Auto",
    description: "The agent handles everything. You're notified, never blocked.",
    requirement: "50 unedited approvals in Auto-Urgent + zero overrides.",
  },
];

export interface Vendor {
  id: string;
  type: "Plumber" | "Electrician" | "HVAC" | "Locksmith" | "Other";
  name: string;
  phone: string;
  afterHours: boolean;
}

export interface FaqEntry {
  id: string;
  q: string;
  a: string;
}

export interface PropertyConfig {
  id: string;
  address: string;
  nickname: string;
  activeSince: string;
  autonomy: AutonomyMode;
  streak: { current: number; target: number };
  notify: {
    emergencyAnytime: boolean;
    urgentWindow: string;
    digestTime: string;
    quietHours: { start: string; end: string };
  };
  rules: {
    pets: string;
    smoking: string;
    parking: string;
    quietHours: { start: string; end: string };
    guests: string;
  };
  lease: {
    rent: string;
    dueDay: string;
    deposit: string;
    end: string;
    monthToMonth: boolean;
  };
  vendors: Vendor[];
  faq: FaqEntry[];
  overrides: { id: string; issue: string; severity: string; reason: string }[];
  phoneNumber: string;
  trust: {
    approvedUnchanged: number;
    edited: number;
    unchangedRate: number;
    graduationReady: boolean;
  };
}

export const propertyConfigs: Record<string, PropertyConfig> = {
  main4: {
    id: "main4",
    address: "123 Main St #4, Oakville",
    nickname: "123 Main #4",
    activeSince: "October 2024",
    autonomy: "shadow",
    streak: { current: 7, target: 10 },
    notify: {
      emergencyAnytime: true,
      urgentWindow: "2 hrs",
      digestTime: "6:00 PM",
      quietHours: { start: "10:00 PM", end: "7:00 AM" },
    },
    rules: {
      pets: "Cats only",
      smoking: "No smoking",
      parking: "Spot 14",
      quietHours: { start: "10:00 PM", end: "7:00 AM" },
      guests: "With notice",
    },
    lease: {
      rent: "$1,950",
      dueDay: "1st",
      deposit: "$1,950",
      end: "August 2026",
      monthToMonth: false,
    },
    vendors: [
      { id: "v1", type: "Plumber", name: "Mike's Plumbing", phone: "(905) 555-0142", afterHours: true },
      { id: "v2", type: "Electrician", name: "Sparks Electric", phone: "(905) 555-0387", afterHours: false },
    ],
    faq: [
      { id: "f1", q: "Where's the parking?", a: "Spot 14 in the back lot." },
      { id: "f2", q: "When's garbage day?", a: "Tuesday before 7am." },
    ],
    overrides: [],
    phoneNumber: "+1 (905) 555-0188",
    trust: { approvedUnchanged: 10, edited: 0, unchangedRate: 100, graduationReady: true },
  },
};

export function getPropertyConfig(id: string): PropertyConfig {
  return propertyConfigs[id] ?? propertyConfigs.main4;
}
