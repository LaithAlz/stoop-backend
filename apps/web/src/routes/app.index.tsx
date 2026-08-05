import { createFileRoute } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Loader2 } from "lucide-react";
import { toast } from "sonner";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { GreetingHeader } from "@/components/clarity/GreetingHeader";
import { CountsStrip } from "@/components/clarity/CountsStrip";
import { EmergencyBanner } from "@/components/clarity/EmergencyBanner";
import { DecisionCard } from "@/components/clarity/DecisionCard";
import { UNVERIFIED_SEND_NOTICE } from "@/components/clarity/EditDraftPanel";
import { SkippedCard } from "@/components/clarity/SkippedCard";
import { AllClearState } from "@/components/clarity/AllClearState";
import { useAuth } from "@/auth/AuthProvider";
import { useQueue } from "@/api/queue";
import { ApiError, toHouseApiError } from "@/api/errors";
import type { QueueItem } from "@/api/types";
import { firstName } from "@/lib/tenantName";
import { formatRelativeTime } from "@/lib/relativeTime";
import {
  buildQueueView,
  pruneQueueSnapshots,
  secondsRemaining,
  totalUndoSeconds,
  type QueueSnapshot,
  type QueueViewRow,
} from "@/features/queue/queueEntries";
import { useDraftActions } from "@/features/queue/useDraftActions";
import { useResolveUnverifiedSends } from "@/features/queue/useResolveUnverifiedSends";
import {
  emergencyHeadline,
  emergencySubtext,
  emergencyTenantMessage,
  hasAcknowledgeableNotification,
} from "@/features/emergency/emergencyBanner";
import { useAcknowledge } from "@/features/emergency/useAcknowledge";

export const Route = createFileRoute("/app/")({
  head: () => ({
    meta: [{ title: "Home. Stoop." }, { name: "robots", content: "noindex" }],
  }),
  component: AppQueuePage,
});

/**
 * Home — the real approval queue (issue #234 PR 2). Fetches `GET /v1/queue`
 * (src/api/queue.ts) and layers the local approve/undo/skip state machine
 * (src/features/queue/useDraftActions.ts + queueEntries.ts) on top — see
 * queueEntries.ts's docstring for why a local overlay exists at all over a
 * server that only ever lists cases still needing action. Ported from
 * apps/mobile/src/app/(tabs)/index.tsx's same wiring.
 *
 * mock-app.ts is intentionally NOT imported here — the conversation routes
 * are live too as of campaign issue #234 PR 3 (src/api/cases.ts), so every
 * card/banner below links straight into them; properties/account stay on
 * mock-app.ts until their own PRs land (see the PR report). The empty
 * state below only ever renders once a real, successful fetch says the
 * queue is actually empty.
 */
function AppQueuePage() {
  const { session } = useAuth();
  // Defense-in-depth session gate — see src/api/queue.ts's docstring for
  // why this is a SECOND, independent gate on top of the route guard that
  // already keeps this component from ever mounting unauthenticated.
  const queueQuery = useQueue({ enabled: Boolean(session) });

  // Last-known QueueItem (plus its last-known position, item 6) per draft
  // id for the three entry statuses buildQueueView pins past their server
  // row disappearing from a fresh `items` read: `skipped` (the founder
  // ruling), `sending` (the undo window must survive a refetch, #291),
  // and `sent` (item 5, one more commit past the countdown hitting zero,
  // see buildQueueView's own docstring for why). Written at
  // `handleSkip`/`handleApprove`/`handleSubmitEdit` below, right before
  // the local overlay entry moves to the status that needs it pinned.
  const [queueSnapshots, setQueueSnapshots] = useState<Record<string, QueueSnapshot>>({});
  // A7 (safety review, #234 PR 2): the item whose editor is open, captured
  // at open time — buildQueueView pins it so a background poll that drops
  // the row can't unmount the editor mid-type. Cleared as soon as the
  // editor closes (the effect below).
  //
  // Finding 5 (safety review round 3, #291/#279; corrected round 4, see
  // `handleSubmitEdit` below): `index` is captured HERE too, at the same
  // open-time moment as `item`, but only as a FALLBACK now, not the
  // value `handleSubmitEdit` always uses. A prior revision of this
  // comment claimed the open-time value was used as-is because a
  // submit-time lookup "would find nothing" if the row had fallen out of
  // `decisionItems` while the editor sat open, true for THAT case, but
  // it ignored the opposite one: a row ABOVE the edited card leaving
  // `decisionItems` during the same window shifts every row after it up
  // one slot, so the STALE open-time index is now too LARGE, and
  // `buildQueueView`'s clamp walks the card toward the end of the queue,
  // item 6's harm arriving a different way, self-inflicted this time.
  // `handleSubmitEdit` now looks the row up FRESH at submit time and only
  // falls back to this captured value once the row has genuinely left
  // `decisionItems` (the one case a fresh lookup can't answer at all).
  const [editingSnapshot, setEditingSnapshot] = useState<{
    item: QueueItem;
    index: number;
  } | null>(null);

  const onNotice = useCallback((message: string) => toast(message), []);
  const onSettled = useCallback(() => void queueQuery.refetch(), [queueQuery]);
  const draftActions = useDraftActions({ onNotice, onSettled });
  const { entries } = draftActions;
  const acknowledge = useAcknowledge({ onNotice });

  // Once the server confirms a "sent" card is really gone from the queue,
  // drop its local entry too — otherwise it just sits inert forever.
  //
  // BLOCKER 1 (safety review round 3, #291/#279): that was the only exit
  // this effect had, and it depends on the draft actually LEAVING a fresh
  // `items` read. An undo whose DELETE commits server-side (draft back to
  // `pending`) but whose response is lost or arrives ambiguously
  // (useDraftActions.ts's undoMutation onError) is correctly left
  // "sending" by that fix, not cleared: the reply may genuinely still be
  // on its way. But the countdown itself is a plain timer with no
  // knowledge of any of that: five seconds later it ticks "sending" to
  // "sent" regardless, and the draft NEVER left `items` (the undo put it
  // right back), so the first branch above never fires either. The card
  // freezes on "Sent." with zero controls, forever, while the tenant is
  // genuinely still unanswered.
  //
  // BLOCKER 1 (safety review ROUND 4, #291/#279): round 3's fix here (the
  // paragraph above) compared `queueQuery.dataUpdatedAt > entry.
  // approvedAtClient` and claimed a read from before the approve "must
  // NOT trip this." That is false. TanStack Query stamps `dataUpdatedAt`
  // when a response RESOLVES on the client (`successState()`,
  // `@tanstack/query-core`), not when the server computed it, so a read
  // ISSUED before the approve and RESOLVING after it (an ordinary
  // refetch: window focus, another card's `onSettled`, `useAcknowledge`'s
  // queue invalidation) satisfies that inequality while still carrying a
  // pre-approve snapshot that honestly still lists the draft. Measured: a
  // plain Approve, no undo tap involved at all, cleared within the
  // ordinary 5s window purely because such a read happened to land, then
  // a landlord's "correction" edit-and-send on the reopened editor POSTed
  // successfully but hit the API's idempotent 200 (the draft was already
  // approved) and never changed the delivered text.
  //
  // The fix is evidence, not a clock. `entry.undoAmbiguousAt`
  // (queueEntries.ts) is set from EXACTLY ONE place: useDraftActions.ts's
  // `undoMutation` onError's ambiguous branch, the one case where an
  // Undo was genuinely attempted and the server's answer is genuinely
  // unknown. A plain Approve, or a clean undo success/failure, never sets
  // it, so no stale read can ever satisfy `undoAmbiguousAt !== undefined`
  // on an entry nothing was ever attempted against. Once it IS set, a
  // read that completed AFTER that ambiguous attempt (not the approve) is
  // the one honest signal that the server has since said something new
  // about this exact draft.
  //
  // `approvedAtClient`/`undoAmbiguousAt` and `dataUpdatedAt` are both
  // client `Date.now()` in the same browser, so #283's clock-skew class
  // doesn't apply here, and this app never dehydrates/hydrates the query
  // cache, so `dataUpdatedAt` is never server-stamped either, but
  // `Date.now()` is still non-monotonic (an NTP step could invert either
  // comparison), a second reason to gate on evidence an undo was
  // attempted rather than on any timestamp comparison at all.
  useEffect(() => {
    if (!queueQuery.data) return;
    const freshIds = new Set(queueQuery.data.items.map((item) => item.draft_id));
    for (const [draftId, entry] of Object.entries(entries)) {
      if (entry.status !== "sent") continue;
      if (!freshIds.has(draftId)) {
        draftActions.dispatch({ type: "cleared", draftId });
      } else if (
        entry.undoAmbiguousAt !== undefined &&
        queueQuery.dataUpdatedAt > entry.undoAmbiguousAt
      ) {
        draftActions.dispatch({ type: "cleared", draftId });
      }
    }
    // draftActions.dispatch is stable (useReducer) — entries is the only
    // real dependency here besides the read itself.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queueQuery.data, queueQuery.dataUpdatedAt, entries]);

  // #279: the resolution rule itself now lives in
  // useResolveUnverifiedSends.ts, shared with the conversation thread.
  // See that file's docstring for the full F1/F11 reasoning this used to
  // carry inline.
  useResolveUnverifiedSends({
    data: queueQuery.data,
    dataUpdatedAt: queueQuery.dataUpdatedAt,
    unverifiedSendIds: draftActions.unverifiedSendIds,
    resolveUnverifiedSend: draftActions.resolveUnverifiedSend,
    giveUpUnverifiedSend: draftActions.giveUpUnverifiedSend,
  });

  const items = useMemo(() => queueQuery.data?.items ?? [], [queueQuery.data]);
  // Rule #1: the emergency line is never paywalled, throttled, or gated —
  // every emergency renders its own banner, never just the first one.
  //
  // F10 (safety re-verify round 2, #252): severity is NOT the only
  // emergency signal available here. `notification_id` is populated only
  // when the case has an UNACKNOWLEDGED emergency-call notification
  // (app/routers/queue.py's third LATERAL) — an authoritative "this
  // landlord's phone is ringing about this case" flag, and the Home
  // analogue of the `emergency_triggered` audit row the thread already
  // keys on. Without it, a card whose severity is null (a defensive
  // classification miss) or written LOWER than emergency (the clamp
  // failure this same PR fixed on the thread's plaque) rendered as an
  // ordinary decision card: no banner, and — worse — no acknowledge
  // button at all, while the escalation chain was still calling. The
  // dashboard has to agree with the phone.
  const isEmergencyCard = (item: QueueItem) =>
    item.severity === "emergency" || item.notification_id !== null;
  const emergencyItems = useMemo(() => items.filter(isEmergencyCard), [items]);
  const decisionItems = useMemo(() => items.filter((item) => !isEmergencyCard(item)), [items]);
  const editingContext = draftActions.editingContext;
  useEffect(() => {
    if (!editingContext) setEditingSnapshot(null);
  }, [editingContext]);

  const rows = useMemo(
    () =>
      buildQueueView(
        decisionItems,
        entries,
        queueSnapshots,
        editingContext ? (editingSnapshot?.item ?? null) : null,
      ),
    [decisionItems, entries, queueSnapshots, editingContext, editingSnapshot],
  );

  const needYou = queueQuery.data?.counts.total ?? 0;
  const waitingOnTenants = queueQuery.data?.counts.awaiting_tenant ?? 0;

  // Item 6 (safety review, #291/#279): where a draft sat in the CURRENT
  // `decisionItems` order, the same array buildQueueView receives as its
  // `items` param, at the moment it's about to need pinning. `-1` (not
  // found) falls back to the end rather than throwing.
  //
  // Finding 5 (safety review round 3, #291/#279; corrected round 4): this
  // comment used to claim `-1` "shouldn't happen: Approve/Skip/
  // edit-and-send only ever act on a live pending row", true for Approve
  // and Skip, which always call this with the exact item the caller just
  // clicked ON, but WRONG for edit-and-send: `handleSubmitEdit` used to
  // call this again at SUBMIT time against `editingSnapshot.draft_id`,
  // and A7's whole reason to pin an editing item is that it can already
  // have fallen out of `decisionItems` by then (a background poll while
  // the editor sits open). That combination made `-1` reachable, and its
  // fallback (walk the row to the very end) is precisely what item 6
  // above was added to prevent.
  //
  // Round 3 "fixed" this by having `handleOpenEditor` capture the index
  // once, at open time, and claimed that was "the one moment a call here
  // is actually guaranteed to find the item", overstated. The open-time
  // capture is guaranteed to find the item, but by SUBMIT time it can be
  // WRONG in the opposite direction: if a row ABOVE the edited card
  // leaves `decisionItems` while the editor sits open, every row after it
  // shifts up one slot, so the captured index is now too LARGE, and the
  // same clamp walks the card toward the end, item 6's harm arriving a
  // different way, self-inflicted this time by a stale index rather than
  // a missing one. `handleSubmitEdit` below now calls this function AGAIN
  // at submit time and only falls back to the captured value on the `-1`
  // (not-found) sentinel, the one case a fresh lookup genuinely cannot
  // answer. This function itself still only promises "shouldn't happen"
  // for a caller passing the draft id of a row it can currently see; it
  // makes no promise about a stale id, which is exactly why its `-1`
  // fallback is load-bearing for `handleSubmitEdit` now, not just a
  // defensive last resort.
  const snapshotIndexOf = useCallback(
    (draftId: string) => {
      const idx = decisionItems.findIndex((i) => i.draft_id === draftId);
      return idx === -1 ? decisionItems.length : idx;
    },
    [decisionItems],
  );

  function handleSkip(item: QueueItem) {
    setQueueSnapshots((prev) => ({
      ...pruneQueueSnapshots(prev, entries),
      [item.draft_id]: { item, index: snapshotIndexOf(item.draft_id) },
    }));
    draftActions.skip({
      draftId: item.draft_id,
      caseId: item.case_id,
      tenantName: item.tenant_name,
    });
  }

  // #291: captured BEFORE `draftActions.approve` so the snapshot exists
  // the instant the mutation's `onSuccess` flips this draft's local entry
  // to "sending": buildQueueView needs it on that very first render in
  // case a concurrent refetch has already dropped the row from `items`.
  function handleApprove(item: QueueItem) {
    setQueueSnapshots((prev) => ({
      ...pruneQueueSnapshots(prev, entries),
      [item.draft_id]: { item, index: snapshotIndexOf(item.draft_id) },
    }));
    draftActions.approve({
      draftId: item.draft_id,
      caseId: item.case_id,
      tenantName: item.tenant_name,
    });
  }

  function handleOpenEditor(item: QueueItem) {
    // Finding 5 (safety review round 3, #291/#279): captured HERE, while
    // `item` is still guaranteed live in `decisionItems` (Edit only ever
    // renders on a currently-visible pending row). See `editingSnapshot`'s
    // own comment above for why this can't wait until submit time.
    setEditingSnapshot({ item, index: snapshotIndexOf(item.draft_id) });
    draftActions.openEditor(
      { draftId: item.draft_id, caseId: item.case_id, tenantName: item.tenant_name },
      item.draft_body,
    );
  }

  // #291: same reasoning as `handleApprove` above: a successful
  // edit-and-send dispatches the identical "approved" action (it's one
  // mutation outcome shape, per useDraftActions.ts), so it needs the same
  // pin. `editingSnapshot` is the queue item the editor opened with;
  // `draft_body` is overridden to the body actually just submitted so the
  // pinned "On its way to {tenant}" bubble shows the sent text, not the
  // pre-edit draft.
  //
  // Finding 5 (safety review round 3, #291/#279; corrected round 4): this
  // used to reuse `editingSnapshot.index` unconditionally, captured back
  // at `handleOpenEditor` time, on the claim that a fresh
  // `snapshotIndexOf` call here "could legitimately miss ... and silently
  // walk this card to the bottom of the queue." True, but incomplete: the
  // captured value can ALSO be wrong by now (see `snapshotIndexOf`'s own
  // comment above) if a row above the edited card left `decisionItems`
  // while the editor sat open, which walks the card toward the end just
  // as surely. A fresh lookup is honest for as long as the row is still
  // actually in `decisionItems`; only fall back to the captured value
  // once the row has genuinely left it (`snapshotIndexOf`'s own `-1`
  // sentinel, `decisionItems.length`), the one case a fresh lookup can't
  // answer. No ambiguity between "found at the last slot" and "not
  // found": a real found index is always `< decisionItems.length`, so the
  // sentinel can never collide with one.
  function handleSubmitEdit(body: string) {
    if (editingSnapshot) {
      const draftId = editingSnapshot.item.draft_id;
      const freshIndex = snapshotIndexOf(draftId);
      const index = freshIndex === decisionItems.length ? editingSnapshot.index : freshIndex;
      setQueueSnapshots((prev) => ({
        ...pruneQueueSnapshots(prev, entries),
        [draftId]: {
          item: { ...editingSnapshot.item, draft_body: body },
          index,
        },
      }));
    }
    draftActions.submitEdit(body);
  }

  // `Boolean(data)`, not `isSuccess` — during a failed background refetch
  // (B1 below) the last successful payload is still what's on screen, and
  // an empty one still honestly means "all clear as of the last update"
  // (the refresh strip right above says exactly that).
  const showAllClear = Boolean(queueQuery.data) && rows.length === 0 && emergencyItems.length === 0;

  return (
    <PhoneFrame>
      <div className="flex flex-1 flex-col bg-clarity-bg">
        <GreetingHeader>
          <CountsStrip needYou={needYou} waitingOnTenants={waitingOnTenants} />
        </GreetingHeader>

        <main className="flex-1 overflow-y-auto px-[18px] py-4">
          {/* A9 (safety review, #234 PR 2): `isPending`, not `isLoading` —
              a query that's gated off (`enabled: false`, e.g. the brief
              session-settling gap) has isLoading false but has never
              fetched, and used to fall through to the content branch and
              render an EMPTY queue as if the server had said "all clear".
              isPending is true until a first result actually exists. */}
          {queueQuery.isPending ? (
            <div
              role="status"
              aria-live="polite"
              className="flex flex-col items-center gap-3 py-16 text-center"
            >
              <Loader2
                className="size-6 animate-spin text-clarity-brand motion-reduce:animate-none"
                aria-hidden="true"
              />
              <p className="font-clarity-sans text-sm font-semibold text-clarity-ink-dim">
                Loading your queue…
              </p>
            </div>
          ) : queueQuery.isError && !queueQuery.data ? (
            <div role="alert" className="flex flex-col items-center gap-3 py-16 text-center">
              <p className="font-clarity-sans text-sm font-semibold text-clarity-ink-dim">
                {queueQuery.error instanceof ApiError
                  ? toHouseApiError(queueQuery.error)
                  : "Couldn't load your queue. Try again."}
              </p>
              <button
                type="button"
                onClick={() => void queueQuery.refetch()}
                className="inline-flex min-h-12 items-center justify-center rounded-clarity-md border-[1.5px] border-clarity-brand-deep bg-clarity-brand px-5 font-clarity-sans text-[15px] font-extrabold text-clarity-brand-on shadow-clarity-banner transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0"
              >
                Try again
              </button>
            </div>
          ) : (
            <>
              {/* B1 (safety review, #234 PR 2): the full error takeover
                  above only ever renders when NO data has ever loaded.
                  TanStack Query keeps the last successful payload through
                  a failed background refetch — blanking the whole screen
                  (including a live emergency banner and its ack button)
                  over a transient refetch error is the wrong failure
                  direction, so a refetch failure renders as this quiet
                  strip over the preserved data instead. The banners/cards
                  below carry a `conversationId` (the card's `case_id`) so
                  they link into the live conversation routes (campaign
                  issue #234 PR 3). */}
              {emergencyItems.map((item) => (
                <EmergencyBanner
                  key={item.case_id}
                  conversationId={item.case_id}
                  headline={emergencyHeadline(item)}
                  subtext={emergencySubtext(item)}
                  tenantFirstName={firstName(item.tenant_name)}
                  tenantMessage={emergencyTenantMessage(item)}
                  onAcknowledge={
                    hasAcknowledgeableNotification(item)
                      ? () => acknowledge.acknowledge(item.notification_id)
                      : undefined
                  }
                  acknowledging={
                    hasAcknowledgeableNotification(item)
                      ? acknowledge.isAcknowledging(item.notification_id)
                      : false
                  }
                />
              ))}

              {queueQuery.isError && (
                <div
                  role="status"
                  className="mb-3.5 rounded-clarity-md border border-clarity-line-strong bg-clarity-panel px-4 py-2.5 font-clarity-sans text-[13px] font-semibold text-clarity-ink-dim"
                >
                  Couldn&apos;t refresh just now. Showing the last update.
                </div>
              )}

              {showAllClear ? (
                <AllClearState message="I'm watching your messages. Go enjoy your day. I'll text you if anything needs you." />
              ) : (
                rows.length > 0 && (
                  <div className="space-y-3.5">
                    {rows.map((row) => (
                      <QueueRow
                        key={row.item.draft_id}
                        row={row}
                        draftActions={draftActions}
                        onSkip={handleSkip}
                        onApprove={handleApprove}
                        onOpenEditor={handleOpenEditor}
                        onSubmitEdit={handleSubmitEdit}
                      />
                    ))}
                  </div>
                )
              )}
            </>
          )}
        </main>

        <AppTabBar active="home" queueCount={needYou} />
      </div>
    </PhoneFrame>
  );
}

function QueueRow({
  row,
  draftActions,
  onSkip,
  onApprove,
  onOpenEditor,
  onSubmitEdit,
}: {
  row: QueueViewRow;
  draftActions: ReturnType<typeof useDraftActions>;
  onSkip: (item: QueueItem) => void;
  onApprove: (item: QueueItem) => void;
  onOpenEditor: (item: QueueItem) => void;
  onSubmitEdit: (body: string) => void;
}) {
  const { item, entry } = row;
  const tenantFirst = firstName(item.tenant_name);
  // Live `unit` is a bare label ("2", "4", "A"), composed with the
  // property label the same way the mock's `unitLabel`/`propertyLabel`
  // pair was, no new copy invented.
  const propertyLabel = item.unit
    ? `Unit ${item.unit}, ${item.property_label}`
    : item.property_label;

  // #191 F3/F6 (safety review follow-up): DecisionCard's own focus-return
  // effect only ever sees `editingContext` changing (Cancel), so it can
  // handle the Edit round trip on its own. It cannot see these three,
  // none of which touch `editingContext`: Approve moves this row's entry
  // to "sending" (the Undo ticket takes the actions row's place), Skip
  // replaces the WHOLE card with `SkippedCard` (a full unmount, so
  // nothing inside the old DecisionCard could react even if it tried),
  // and a successful Undo moves the entry back from "sending" to idle
  // (the actions row reappears, unmounting the Undo button out from
  // under a focused landlord). `rowRef` wraps whichever of the two
  // branches below ever renders here, so it survives all three swaps as
  // a stable landing spot.
  const rowRef = useRef<HTMLDivElement>(null);
  const undoButtonRef = useRef<HTMLButtonElement>(null);
  const prevEntryStatusRef = useRef(entry.status);
  // #191 round 4 item 4 (safety review re-verify): by the time the
  // transition effect below runs, a removed Undo button has already reset
  // `document.activeElement` to `<body>` (verified by hand: a focused
  // descendant removed from inside a `tabIndex={-1}` container resets
  // focus to `<body>`, never to the container), the exact same value a
  // session that never focused anything at all also sits at. Reading
  // `document.activeElement` only AFTER the removal can't tell those two
  // apart. This ref is refreshed on every render while still "sending"
  // (the countdown's own per-second tick keeps it fresh well within the
  // five-second window), so the transition effect can ask "was Undo
  // genuinely focused as of the last render before it went away" instead.
  const undoHadFocusRef = useRef(false);
  useEffect(() => {
    const prevStatus = prevEntryStatusRef.current;
    prevEntryStatusRef.current = entry.status;
    if (prevStatus === entry.status) return;
    // #191 round 4 item 4 (safety review re-verify): `sending -> sent` is
    // the 5-second countdown simply running out, a timer, never a
    // landlord action by itself. Only stay exempt when Undo did NOT
    // plausibly hold focus right up to the moment it was removed (a
    // landlord who has already moved their attention elsewhere on the
    // page); otherwise fall through to the same recovery every other
    // transition here gets, so a keyboard user whose focus was
    // legitimately on Undo isn't silently dumped onto `<body>` with
    // nothing to land on and no announcement.
    if (prevStatus === "sending" && entry.status === "sent" && !undoHadFocusRef.current) return;
    // F6: only steal focus if it was plausibly inside this row, either
    // still literally there, or reset to <body> because the control that
    // had it was just removed as part of this very transition. A
    // background change to a DIFFERENT row, or this row settling after
    // the landlord has already tabbed elsewhere, leaves focus on some
    // other, still-mounted element, which this correctly leaves alone.
    const active = document.activeElement;
    if (!(rowRef.current?.contains(active) || active === document.body)) return;
    if (entry.status === "sending") {
      // F2/F4: Approve or a successful edit-and-send. Focus the Undo
      // button itself, not just the row, so its accessible name and its
      // `aria-describedby` text (UndoTicket.tsx) are read together at the
      // instant they matter. F8: `.isConnected` alone isn't enough,
      // `.focus()` on a DISABLED button is also a silent no-op, though in
      // practice this button never mounts disabled.
      const btn = undoButtonRef.current;
      if (btn?.isConnected && !btn.disabled) {
        btn.focus();
        return;
      }
    }
    // `preventScroll` (round 5 re-verify): this landing is reached on
    // transitions the landlord did NOT initiate, including the one that
    // fires five seconds after an approve. Recovering focus is right;
    // yanking the viewport back to a card they scrolled away from is
    // not. The announcement still happens, the scroll position does not
    // move.
    rowRef.current?.focus({ preventScroll: true });
  }, [entry.status]);
  // #191 round 4 item 4 (safety review re-verify): refreshed every
  // render, not only on a status change, so it reflects the freshest real
  // focus state right up to the render before "sending" flips to "sent"
  // (see `undoHadFocusRef`'s own comment above).
  useEffect(() => {
    if (entry.status === "sending") {
      undoHadFocusRef.current = document.activeElement === undoButtonRef.current;
    }
  });

  if (entry.status === "skipped") {
    return (
      // #191 round 4 item 5 (safety review re-verify): this wrapper's own
      // content is exactly one `<Link>` (SkippedCard.tsx, when it's given
      // a `conversationId`, which Home always does): `role="group"` is
      // "a set of objects", and one link is not a set. Round 5
      // (re-verify) put both back: this div is where `rowRef.current
      // ?.focus()` lands after a Skip, and round 4 left that landing
      // computing `role=generic name=""` in Chrome's AX tree, where
      // round 3 had a real name. "One link is not a set" is a purist
      // reading of `group`, and it cost a real announcement on a
      // programmatic focus landing. A named `group` is the cheaper
      // trade, and it keeps `aria-label` on a role that supports it
      // (which is what F10 was actually about).
      <div
        ref={rowRef}
        tabIndex={-1}
        role="group"
        aria-label={`${tenantFirst}, ${item.property_label}`}
      >
        <SkippedCard
          conversationId={item.case_id}
          tenantName={tenantFirst}
          propertyLabel={item.property_label}
          timestamp={formatRelativeTime(item.received_at)}
        />
      </div>
    );
  }

  const isEditingThis = draftActions.editingContext?.draftId === item.draft_id;
  const cardStatus =
    entry.status === "sending"
      ? "sending"
      : entry.status === "sent"
        ? "sent"
        : isEditingThis
          ? "editing"
          : "pending";
  const secondsLeft = entry.status === "sending" ? secondsRemaining(entry.undoExpiresAtClient) : 0;
  const totalSeconds = entry.status === "sending" ? totalUndoSeconds(entry) : 5;
  const ctx = { draftId: item.draft_id, caseId: item.case_id, tenantName: item.tenant_name };

  return (
    // #191 round 4 item 5 (safety review re-verify): `aria-label` used to
    // be set here AND on `DecisionCard`'s own `<article>` inside it, the
    // identical string, so browsing this card announced the same name
    // twice, and round 4 dropped it here to fix that. Round 5
    // (re-verify) put it back, because the fix cost more than the
    // duplicate did: Chrome's AX tree confirmed this div then computed
    // `name=""`, and it is exactly where `rowRef.current?.focus()` lands
    // on `sending -> sent`, on undo success, and on `already_sent`. Item
    // 4 buys that focus landing precisely so the landlord is not dumped
    // somewhere unannounced.
    //
    // The duplicate is now DELIBERATE, and the reasoning is the trade,
    // not an oversight. Both this wrapper and DecisionCard's `<article>`
    // are programmatic focus landings (the article is where an
    // edit-cancel recovers to), and the article does not always re-read
    // its containing group's name, because focus moving WITHIN a group
    // does not re-announce it. Naming only one leaves the other landing
    // silent. A repeated name while browsing is verbosity; a silent
    // focus landing is a lost announcement on the approve loop's only
    // escape hatch. Verbosity is the safer failure of the two.
    // `role="group"` stays: unlike the skipped branch above, this
    // wrapper's one child is an `<article>` holding a genuine set of
    // controls (Edit, Skip, Approve, or the Undo ticket), not a lone
    // link.
    <div ref={rowRef} tabIndex={-1} role="group" aria-label={`${tenantFirst}, ${propertyLabel}`}>
      <DecisionCard
        severity={item.severity}
        conversationId={item.case_id}
        tenantName={tenantFirst}
        propertyLabel={propertyLabel}
        timestamp={formatRelativeTime(item.received_at)}
        tenantMessage={item.tenant_message ?? ""}
        photoNote={item.has_media ? (item.media_note ?? "Sent a photo") : undefined}
        draftMessage={item.draft_body}
        why={item.why}
        status={cardStatus}
        secondsLeft={secondsLeft}
        totalSeconds={totalSeconds}
        // F7 (safety re-verify round 2, #252): with the guard actually
        // holding, Cancel is the only live control in the editor, so it
        // becomes the path of least resistance, and behind it the card
        // used to return to a full action row with Approve enabled and no
        // marking at all. One tap there sends the ORIGINAL, un-edited
        // body: exactly the wording the landlord opened the editor to
        // fix. The card now carries the same explanation and the same
        // block.
        //
        // BLOCKER 2 (safety review round 3, #291/#279): once the ceiling
        // gives up, `isSendUnverified` goes false. The fallback above
        // used to end there, which is the exact regression this fix
        // closes: `giveUpNotices` is checked last so the card keeps
        // warning even after the flag itself is gone. FIX 3 (round 4):
        // `giveUpNotices` is a `ReadonlyMap` now (unverifiedSendStore.ts),
        // not a Record, `.get()`, not bracket access. DecisionCard
        // itself now also forwards this same value into the editor while
        // editing (see its own `notice` prop).
        staleNotice={
          draftActions.staleNotices[item.case_id] ??
          (draftActions.isSendUnverified(item.draft_id)
            ? UNVERIFIED_SEND_NOTICE
            : draftActions.giveUpNotices.get(item.draft_id))
        }
        editSubmitting={draftActions.isEditSubmitting}
        sendUnverified={draftActions.isSendUnverified(item.draft_id)}
        actionsBusy={
          draftActions.isBusy(item.draft_id) || draftActions.isSendUnverified(item.draft_id)
        }
        // BLOCKER 2 / item 7 (safety review, #291/#279): mutation-only
        // busy, deliberately NOT OR'd with `isSendUnverified`. See
        // DecisionCard's own `mutationBusy` comment for why Skip and Undo
        // need this instead of `actionsBusy`.
        mutationBusy={draftActions.isBusy(item.draft_id)}
        onApprove={() => onApprove(item)}
        onEdit={() => onOpenEditor(item)}
        onSkip={() => onSkip(item)}
        onUndo={() => draftActions.undo(ctx)}
        onCancelEdit={() => draftActions.cancelEditor()}
        onSubmitEdit={(body) => onSubmitEdit(body)}
        undoButtonRef={undoButtonRef}
      />
    </div>
  );
}
