/**
 * Push-notification copy (issue #210 M3) — the Me tab's notifications
 * card. Plain English, no jargon (CLAUDE.md rule 8). Every status/explainer
 * line below says, or is compatible with saying, that push is NOT the
 * emergency channel (CLAUDE.md rule 1: "the emergency line is never
 * paywalled, throttled, or gated" — a landlord must never come away
 * thinking a silenced/declined push notification means a missed
 * emergency). The explainer states this outright rather than leaving it
 * implied, since it's the one line every landlord is likely to actually
 * read.
 */

export const PUSH_SECTION_TITLE = "Notifications";

export const PUSH_EXPLAINER =
  "Stoop can nudge your phone when a reply is waiting for your approval. " +
  "This is never how an emergency reaches you — a true emergency always " +
  "calls your phone, whether or not notifications are on.";

export const PUSH_STATUS_ON = "On — you'll get a nudge when a reply needs you.";

export const PUSH_STATUS_OFF_CAN_ASK =
  "Off. Turn them on to get a nudge when a reply needs you.";

export const PUSH_STATUS_OFF_SETTINGS =
  "Off. You said no to notifications earlier — turn them on in your phone's Settings if you change your mind.";

export const PUSH_STATUS_UNSUPPORTED = "Notifications aren't available on this device.";

export const PUSH_ENABLE_BUTTON_LABEL = "Turn on notifications";

export const PUSH_OPEN_SETTINGS_BUTTON_LABEL = "Open Settings";

export const PUSH_REQUEST_FAILED_NOTICE = "That didn't go through. Try again in a moment.";
