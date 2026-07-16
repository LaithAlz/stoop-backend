/**
 * Clarity design tokens — ported from docs/mockups/07-clarity-redesign.html
 * (the design authority for the mobile app, per issue #210 / CLAUDE.md) and
 * cross-checked against its web port, apps/web/src/styles.css (the
 * "Clarity v3" block). Every value below carries a comment pointing at its
 * source so a design change can be traced back to the mockup instead of
 * drifting between apps/web and apps/mobile.
 *
 * M0 ships LIGHT ONLY (issue #210 scope). The shape here — a flat `light`
 * palette assigned to `colors`, plus a `ClarityScheme` union of one member —
 * is deliberate so a later phase can add a `dark` palette and a
 * `useClarityColors()` hook without any screen that already imports
 * `colors` needing to change.
 *
 * Standing design directive (mockup "Don't" list, line ~459-467): no purple
 * gradients, no glassmorphism, no Inter-by-default, no rounded-lg-everywhere,
 * no full-pill radius on cards/buttons/badges, no icon-only buttons, no
 * "triage"/"AI agent" jargon (see CLAUDE.md rule 8 for the copy rules this
 * maps to).
 */
import { Platform, type TextStyle } from "react-native";

// ---------------------------------------------------------------------------
// Color — verbatim hex from docs/mockups/07-clarity-redesign.html :root
// (lines 8-34), identical to apps/web/src/styles.css's --clarity-* tokens
// (lines 163-189). Names below match the mockup's CSS custom property names
// with the leading `--(clarity-)` stripped and kebab-case -> camelCase.
// ---------------------------------------------------------------------------
export interface ClarityPalette {
  /** Mockup line 9: "brownstone plaster/brick backdrop behind the exhibit"
   *  — the museum-page backdrop behind the phone frame in the design doc.
   *  NOT part of the app UI itself. Kept here only for traceability; do not
   *  use it as a screen background inside the real app (use `bg`). */
  pageBg: string;
  /** Mockup line 10: "app canvas — warm off-white". The real screen bg. */
  bg: string;
  /** Mockup line 11: cards/surfaces — "flat, no texture". */
  surface: string;
  /** Mockup line 12: inbound message bubbles, photo chips. */
  panel: string;
  line: string;
  lineStrong: string;
  ink: string;
  inkDim: string;

  /** Mockup line 18: "Stoop's own voice & actions — deep forest green". */
  brand: string;
  brandDeep: string;
  brandSoft: string;
  brandBorder: string;
  brandOn: string;

  /** Severity: "Whenever" — enamel green (mockup line 24). */
  whenever: string;
  wheneverDeep: string;
  wheneverInk: string;

  /** Severity: "Can't wait" — enamel amber-brass (mockup line 28). */
  wait: string;
  waitDeep: string;
  waitInk: string;

  /** Severity: "Emergency" — enamel brick red (mockup line 32). */
  emergency: string;
  emergencyDeep: string;
  emergencyInk: string;
}

export const light: ClarityPalette = {
  pageBg: "#EAE6D9",
  bg: "#FDFBF6",
  surface: "#FCFAF3",
  panel: "#FFFFFF",
  line: "#E2DFD1",
  lineStrong: "#C7C0AC",
  ink: "#1E1B16",
  inkDim: "#5B564C",

  brand: "#2D4A3E",
  brandDeep: "#1E332A",
  brandSoft: "#E7ECE6",
  brandBorder: "#B7CBBE",
  brandOn: "#FBFBF6",

  whenever: "#1F7A4C",
  wheneverDeep: "#155C38",
  wheneverInk: "#EAF6EE",

  wait: "#8B5A1E",
  waitDeep: "#6B4113",
  waitInk: "#FCEFDD",

  emergency: "#963226",
  emergencyDeep: "#711B12",
  emergencyInk: "#FBEAE6",
};

/** M0 is light-only; this union grows to `"light" | "dark"` in the phase
 *  that adds a `dark` palette (the emergency takeover screen is the one
 *  place the mockup already permits dark styling — mockup line 310-312 —
 *  everything else stays light until a real dark mode ships). */
export type ClarityScheme = "light";
export const colors: ClarityPalette = light;

// ---------------------------------------------------------------------------
// Type — three system-only "voices", mockup lines 36-38 / styles.css
// lines 191-197. CSS lets one element declare a comma-separated font-stack;
// React Native's `fontFamily` on iOS/Android takes exactly one *installed*
// family name, so each web stack collapses to the closest real font per
// platform. RN Web (Expo's web target) still accepts the full CSS stack, so
// that path keeps the mockup's exact fallback chain.
// ---------------------------------------------------------------------------
export const fonts = {
  /** Stoop's own voice: greetings, wordmark, headings, "I'd like to reply"
   *  bubbles. Mockup line 36: ui-serif, "Iowan Old Style", "Palatino
   *  Linotype", Charter, Georgia, "Times New Roman", serif. */
  serif: Platform.select({
    ios: "Iowan Old Style",
    android: "serif", // No Iowan Old Style on Android; generic `serif` -> Noto Serif.
    web: 'ui-serif, "Iowan Old Style", "Palatino Linotype", Charter, Georgia, "Times New Roman", serif',
    default: "serif",
  }) as string,
  /** Tenant text + UI chrome. Mockup line 37: the standard -apple-system
   *  stack. RN's own default already renders San Francisco/Roboto, so
   *  "System" on each native platform reproduces it without pulling in a
   *  webfont — explicitly not Inter (standing design directive). */
  sans: Platform.select({
    ios: "System",
    android: "sans-serif",
    web: '-apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif',
    default: "System",
  }) as string,
  /** Timestamps, wayfinding, small uppercase caps. Mockup line 38:
   *  ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, "Liberation
   *  Mono", monospace. */
  mono: Platform.select({
    ios: "Menlo",
    android: "monospace",
    web: 'ui-monospace, "SF Mono", "Cascadia Mono", Menlo, Consolas, "Liberation Mono", monospace',
    default: "monospace",
  }) as string,
};

// ---------------------------------------------------------------------------
// Type scale — sizes/weights lifted from the mockup's actual rules (there is
// no named --type-* scale in the source; each entry cites the selector it
// came from). "Do" rule (mockup line 452): 16px+ body, 48px+ touch targets —
// both enforced below (`type.body` is the floor; see `touchTarget`).
// letterSpacing is stored in RN's points, converted from the mockup's `em`
// (em * fontSize) since RN has no `em` unit.
// ---------------------------------------------------------------------------
export const type = {
  greeting: { fontFamily: fonts.serif, fontSize: 27, fontWeight: "600", lineHeight: 32 }, // .greeting, mockup line 161
  wordmark: { fontFamily: fonts.serif, fontSize: 21, fontWeight: "700", lineHeight: 24 }, // .wordmark, mockup line 154
  cardTitle: { fontFamily: fonts.serif, fontSize: 16, fontWeight: "600", lineHeight: 22 }, // .trust-card h3, mockup line 365
  allClearTitle: { fontFamily: fonts.serif, fontSize: 22, fontWeight: "600", lineHeight: 28 }, // .all-clear h3, mockup line 283
  body: { fontFamily: fonts.sans, fontSize: 16, lineHeight: 24 }, // body{}, mockup lines 56-57 (16px floor)
  bubble: { fontFamily: fonts.sans, fontSize: 15.5, lineHeight: 24 }, // .bubble, mockup line 226
  meta: { fontFamily: fonts.sans, fontSize: 12.5, fontWeight: "600", lineHeight: 18 }, // .entry-meta, mockup line 219
  counts: { fontFamily: fonts.sans, fontSize: 13.5, fontWeight: "700", lineHeight: 18 }, // .counts, mockup line 164
  button: { fontFamily: fonts.sans, fontSize: 15, fontWeight: "800", lineHeight: 20 }, // .btn, mockup line 258
  buttonPrimary: { fontFamily: fonts.sans, fontSize: 16, fontWeight: "800", lineHeight: 20 }, // .btn.primary, mockup lines 262-264
  plaque: {
    fontFamily: fonts.sans,
    fontSize: 12,
    fontWeight: "800",
    lineHeight: 14,
    letterSpacing: 0.96, // 0.08em * 12px, mockup line 107
  },
  plaqueSm: { fontFamily: fonts.sans, fontSize: 10.5, fontWeight: "800", lineHeight: 13 }, // .plaque.sm, mockup line 119
  marginKicker: {
    fontFamily: fonts.sans,
    fontSize: 10,
    fontWeight: "800",
    lineHeight: 12,
    letterSpacing: 1, // 0.1em * 10px, mockup line 249
  },
  marginBody: {
    fontFamily: fonts.serif,
    fontSize: 14.5,
    lineHeight: 23,
    fontStyle: "italic",
  }, // .margin-note p, mockup line 250
  footnote: { fontFamily: fonts.sans, fontSize: 12.5, lineHeight: 20 }, // .footnote, mockup line 276
  stamp: {
    fontFamily: fonts.mono,
    fontSize: 10.5,
    fontWeight: "700",
    letterSpacing: 0.5, // 0.05em * 10.5px, mockup line 222-224
  },
  tabLabel: {
    fontFamily: fonts.sans,
    fontSize: 10.5,
    fontWeight: "800",
    letterSpacing: 0.21, // 0.02em * 10.5px, mockup lines 180-182
  },
} satisfies Record<string, TextStyle>;

// ---------------------------------------------------------------------------
// Spacing — the mockup has no named --space-* scale; this is a scale
// *derived* from the padding/gap values actually used across its rules
// (e.g. .entry padding:18px line 209, .actions gap:9px line 257, .em-banner
// padding:14px 16px line 195) rounded to a consistent progression so RN
// layout code has one thing to import instead of forty magic numbers.
// ---------------------------------------------------------------------------
export const spacing = {
  xs: 4,
  sm: 8,
  md: 12,
  base: 16,
  lg: 18, // matches .entry's own padding (mockup line 209) exactly
  xl: 24,
  xxl: 32,
} as const;

/** "Do" rule, mockup line 452: "48px+ targets". Use as `minHeight` on every
 *  Pressable/Button. */
export const touchTarget = 48;

// ---------------------------------------------------------------------------
// Radius — verbatim 3-step scale + plaque step, mockup lines 41-47 /
// styles.css lines 198-201. NEVER a full pill (999px) on cards, buttons, or
// badges (mockup "Don't" list, line 462) — pill radius is reserved for
// genuine hardware-style controls (toggle switches, progress tracks), none
// of which M0 renders yet.
// ---------------------------------------------------------------------------
export const radius = {
  sm: 8, // chips, thumbnails, bubble tail-corners
  md: 14, // buttons, the undo ticket's outer shell, notification banners
  lg: 16, // queue cards, trust cards, safety box, tab-bar-adjacent surfaces
  plaque: 6, // enamel severity plaques — softened, but a plaque, never a pill
} as const;

const clarity = { colors, fonts, type, spacing, touchTarget, radius };
export default clarity;
