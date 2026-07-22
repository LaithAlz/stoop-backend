/**
 * Pure mapping from a push notification's `data` payload to an expo-router
 * target (issue #210 M3). Kept separate from src/features/push/
 * usePushRegistration.ts so the routing decision is unit-testable without
 * mocking expo-router or expo-notifications.
 *
 * `data` only ever carries uuids (`app/push_outbox.py::_build_message` on
 * the backend reads ONLY `case_id`/`draft_id` out of the outbox payload —
 * see that module's "Payload safety" docstring section) — this function
 * mirrors that discipline by reading nothing else out of it. The tapped
 * notification's own title/body are the fixed, generic strings the
 * backend sent ("A reply is waiting for your approval") — never rendered
 * here as tenant content, and this function doesn't touch them at all;
 * the destination screen (src/app/(tabs)/conversations/[id].tsx) fetches
 * the real case content itself once navigated.
 */

export interface CaseDeepLinkTarget {
  pathname: "/conversations/[id]";
  params: { id: string };
}

/** `data` is the notification's opaque `content.data` — typed `unknown`
 *  here since it transits Apple/Google's servers and expo-notifications'
 *  own type (`Record<string, unknown>`) makes no shape guarantee. Returns
 *  `null` for anything that isn't a usable case id, so a malformed or
 *  future-shaped payload never crashes navigation — it's silently a no-op
 *  tap instead (the queue/case list are still reachable normally). */
export function resolveNotificationDeepLink(data: unknown): CaseDeepLinkTarget | null {
  if (!data || typeof data !== "object") return null;
  const caseId = (data as Record<string, unknown>).case_id;
  if (typeof caseId !== "string" || caseId.length === 0) return null;
  return { pathname: "/conversations/[id]", params: { id: caseId } };
}
