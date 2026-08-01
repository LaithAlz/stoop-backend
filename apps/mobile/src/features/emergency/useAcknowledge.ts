/**
 * The acknowledge mutation for a queue emergency item's `notification_id`
 * (api-contracts.md's Queue section, v1.15 amendment: `notification_id` +
 * `POST /v1/notifications/{id}/ack` — the shipped path for the emergency
 * banner's acknowledge action). Home (src/app/(tabs)/index.tsx) is the only
 * caller: `GET /v1/cases/{id}` carries no `notification_id` (same
 * amendment), so case-detail has no ack surface — see that screen's own
 * comment on its still-informational `EmergencyBanner`.
 *
 * Acknowledging stops the landlord escalation chain
 * (docs/02-product/emergency-prefilter.md "The escalation chain") — it
 * does not resolve the case, and it changes nothing tenant-facing. Success
 * here only ever invalidates the queue query (src/api/queue.ts's
 * `queueQueryKey`); the banner disappears once the server itself stops
 * reporting an unacknowledged notification. This deliberately never
 * locally fakes the removal — hiding a live emergency banner on an
 * optimistic guess is the wrong failure direction for a safety surface,
 * unlike an ordinary list mutation.
 *
 * `retry: 0` comes from the shared QueryClient's mutation default
 * (src/api/queryClient.ts) — same house pattern every other mutation in
 * this app relies on rather than re-declaring per call site.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ackNotification } from "@/api/notifications";
import { queueQueryKey } from "@/api/queue";
import { ApiError, toHouseApiError } from "@/api/errors";

interface UseAcknowledgeOptions {
  /** House-voice message surfaced on failure — the screen decides how to
   *  present it (`Alert.alert` today, same as useDraftActions). */
  onNotice: (message: string) => void;
}

export function useAcknowledge({ onNotice }: UseAcknowledgeOptions) {
  const queryClient = useQueryClient();

  const mutation = useMutation({
    mutationFn: (notificationId: string) => ackNotification(notificationId),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queueQueryKey });
    },
    onError: (error: unknown) =>
      onNotice(
        error instanceof ApiError
          ? toHouseApiError(error)
          : "Something didn't go through. Try again in a moment.",
      ),
  });

  return {
    acknowledge: (notificationId: string) => mutation.mutate(notificationId),
    /** Scoped to the specific notification id — a landlord with more than
     *  one live emergency card must only see the button they tapped
     *  change state, never every card at once. */
    isAcknowledging: (notificationId: string) =>
      mutation.isPending && mutation.variables === notificationId,
  };
}
