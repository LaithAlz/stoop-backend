/**
 * The approve/undo/skip/edit-and-send mutations for Home
 * (src/routes/app.index.tsx). Ported near-verbatim from
 * apps/mobile/src/features/queue/useDraftActions.ts (campaign issue #234
 * PR 2) — same undo-countdown/draft_stale/already_sent handling, only the
 * import paths changed. Web has one caller today (mobile shares this
 * between Home and case-detail); a later PR wiring the conversation
 * thread's own approve/undo/reject can reuse this unchanged.
 *
 * Network calls go through src/api/drafts.ts; the local "sending"/"sent"/
 * "skipped" overlay is src/features/queue/queueEntries.ts's pure reducer.
 * This hook is the React glue between the two — not pure itself (it owns
 * mutations/timers), so it's exercised by hand (see the PR report's manual
 * QA list) rather than unit-tested; the reducer/helpers it wraps stay pure
 * and unit-testable once a test runner exists.
 */
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { approveDraft, editAndSendDraft, rejectDraft, undoDraftApprove } from "@/api/drafts";
import { ApiError, toHouseApiError } from "@/api/errors";
import {
  UNVERIFIED_GIVE_UP_CARD_NOTICE,
  UNVERIFIED_GIVE_UP_NOTICE,
} from "@/components/clarity/EditDraftPanel";
import { firstName } from "@/lib/tenantName";
import {
  computeUndoExpiresAt,
  draftStaleNotice,
  queueEntriesReducer,
  secondsRemaining,
} from "./queueEntries";
import {
  clearGiveUpNotice,
  clearUnverifiedSend,
  markUnverifiedSend,
  setGiveUpNotice,
  useGiveUpNotices,
  useUnverifiedSendIds,
} from "./unverifiedSendStore";

export interface DraftContext {
  draftId: string;
  caseId: string;
  tenantName: string;
}

/** F2 (safety review round 6): Undo carries the `approvedAtClient` of the
 *  approve cycle it was fired from, so an ambiguous failure can only ever
 *  stamp the entry it actually belongs to. Without it, a cycle-1 DELETE
 *  still hung when cycle 2 begins stamps cycle 2's entry with cycle 1's
 *  failure. `apiRequest` passes no AbortSignal for undo, so that hung
 *  fetch is genuinely unbounded and the class is worth closing outright
 *  rather than relying on a refetch winning the race. */
interface UndoContext extends DraftContext {
  approvedAtClient: number;
}

interface UseDraftActionsOptions {
  /** House-voice message surfaced for a failure the landlord should see
   *  (network errors, `already_sent`, `draft_not_undoable`, ...) — the
   *  screen decides how to present it (a toast today, src/routes/
   *  app.index.tsx). */
  onNotice: (message: string) => void;
  /** Called after any server-confirmed state change that the screen's own
   *  query doesn't already own refetching for (draft_stale, skip, an
   *  already-resolved undo/approve) — Home refetches `useQueue` so the
   *  screen stays honest about what the server actually thinks happened. */
  onSettled: () => void;
}

/** F1 (safety review round 6): shown when an Undo request fails in a way
 *  that leaves the outcome genuinely unknown (a dropped connection or a
 *  5xx, as opposed to `already_sent` or `draft_not_undoable`, which are
 *  the server answering). It must not claim the reply was stopped and it
 *  must not claim it went out, because neither is known. It also must not
 *  say "try again": by the time this surfaces the countdown has usually
 *  expired and the Undo control is gone. Pointing at the conversation is
 *  the only advice that is true and actionable in every case. */
export const UNDO_AMBIGUOUS_NOTICE =
  "I couldn't tell whether your undo went through. Open the conversation to see whether the reply went out.";

export function useDraftActions({ onNotice, onSettled }: UseDraftActionsOptions) {
  const [entries, dispatch] = useReducer(queueEntriesReducer, {});
  const [staleNotices, setStaleNotices] = useState<Record<string, string>>({});
  // BLOCKER 2 (safety review round 3, #291/#279): the give-up ceiling's
  // own sticky notice, keyed by draft id, parallel to `staleNotices` above
  // but deliberately with NO auto-dismiss timer. See
  // `UNVERIFIED_GIVE_UP_CARD_NOTICE`'s own comment for why. Cleared by
  // `markBusy` below the moment the landlord takes a fresh action on THIS
  // draft (Approve, Skip, or a new edit-and-send attempt) rather than on
  // any clock, since there is no honest wall-clock answer to "how long
  // should a warning about an unconfirmed send stay up", only "until the
  // landlord has acted on it knowing it was there."
  //
  // FIX 3 (safety review round 4, #291/#279): sourced from
  // unverifiedSendStore.ts's own module-scope map now, not a local
  // `useState`, for the identical reason #279 already hoisted
  // `unverifiedSendIds` out of local state, this hook is instantiated
  // PER ROUTE, and the give-up toast's own advice ("Open the conversation
  // to check") sends the landlord to the OTHER route, which used to mount
  // a fresh hook instance with no memory of the notice at all.
  const giveUpNotices = useGiveUpNotices();
  const [editingContext, setEditingContext] = useState<(DraftContext & { body: string }) | null>(
    null,
  );
  const [tick, forceTick] = useReducer((count: number) => count + 1, 0);

  // A2 (safety review, #234 PR 2): which draft ids currently have a
  // mutation in flight — approve, undo, skip, or edit-and-send. A single
  // `useMutation()` instance's own `isPending`/`variables` only reflect
  // the MOST RECENT call (react-query replaces the tracked mutation per
  // `.mutate()`), which is wrong here since one hook instance serves every
  // card on the queue at once — this is a plain per-draft-id set instead,
  // set synchronously before each `.mutate()` call and cleared in that
  // specific call's own `onSettled` (react-query calls per-invocation
  // callbacks for every concurrent call, unlike the observer's aggregate
  // state). Exposed as `isBusy` so the screen can disable a card's
  // Edit/Skip/Approve/Undo controls for exactly the draft with something
  // in flight — the concrete case this guards is an approve tap racing a
  // skip tap on the same card.
  const [busyDraftIds, setBusyDraftIds] = useState<ReadonlySet<string>>(() => new Set());

  // R3-1 (safety review round 3, #234 PR 2 follow-up, issue #252): draft
  // ids whose edit-and-send just hit an AMBIGUOUS failure (network_error/
  // 5xx — R3-2 below) — the request may have already applied server-side,
  // and the API's approve-family idempotent 200 means a retype-and-resend
  // on this same draft would silently deliver the FIRST body while the
  // screen shows a countdown for the second. Send stays disabled for
  // exactly these ids (the screen threads `isSendUnverified` into its
  // editor) until `resolveUnverifiedSend` below is told what the next
  // SUCCESSFUL read of this draft's true state actually was.
  //
  // F1 (safety re-verify, #252): the value is the queue data GENERATION
  // this flag was raised against, not just membership. The first cut used
  // a bare Set and resolved on the next effect flush — but at that instant
  // `queueQuery.data` is still the last SUCCESSFUL payload, taken BEFORE
  // the send, which of course still lists the draft. It resolved
  // "still pending", re-enabled Send about one frame later, and the whole
  // guard was inert. A resolution is only trustworthy against a read that
  // completed AFTER the failure, which is what the generation comparison
  // in src/features/queue/useResolveUnverifiedSends.ts (shared by every
  // caller as of #279) enforces.
  //
  // #279: this used to be a local `useState`, invisible to any OTHER
  // `useDraftActions` instance (this hook is instantiated PER ROUTE:
  // Home, the conversation thread), so a flag raised on one route was
  // simply gone the moment that route's instance unmounted. Sourced from
  // `unverifiedSendStore.ts` now, a module-scope store every instance
  // reads and writes through, so a flag raised on either surface is
  // visible, and resolvable, on both.
  const unverifiedSendIds = useUnverifiedSendIds();

  const resolveUnverifiedSend = useCallback(
    (draftId: string, stillPending: boolean) => {
      // F12 (safety re-verify round 2): `clearUnverifiedSend` reports
      // whether `draftId` WAS flagged, atomically with the removal.
      // Today's callers (one shared resolution effect per mounted route,
      // as of #279) each iterate their own snapshot of the map, so two
      // mounted routes could in principle both observe the same flagged
      // id and both call this; the atomic check-and-clear is what keeps
      // only the first from firing the notice below.
      const wasFlagged = clearUnverifiedSend(draftId);
      if (!wasFlagged) return;
      // Draft still pending → the ambiguous edit-and-send never applied;
      // clearing the flag above already re-enables Send, nothing else to
      // do.
      if (stillPending) return;
      // F2 (safety re-verify, #252): the draft leaving the queue does NOT
      // prove the reply went out. `GET /v1/queue` joins
      // cases.status='awaiting_approval' with drafts.status='pending', so
      // a draft also leaves it when a tenant reply made it `stale`, when
      // it was rejected/cancelled, or when the case transiently left
      // awaiting_approval mid-graph-rerun. Claiming "your earlier reply
      // already went out" in those cases tells the landlord the tenant
      // was answered when nobody was — the silence direction, in the exact
      // window where a tenant is actively texting. State only what this
      // read supports and point at the record.
      onNotice(
        "This draft isn't waiting to send anymore. Open the conversation to see what happened.",
      );
      // F4: the notice above is unconditional and OUTSIDE the state
      // updater. Previously it fired inside `setEditingContext`, so it was
      // delivered only if the editor happened to still be open on this
      // draft — a landlord who followed the toast's own advice and tapped
      // Cancel got no notice at all — and a React state updater can be
      // invoked more than once, which would have double-toasted.
      setEditingContext((current) => (current?.draftId === draftId ? null : current));
    },
    // #279: `unverifiedSendIds` is no longer read inside this callback's
    // body (`clearUnverifiedSend` reads the shared store directly), so
    // it's correctly gone from these deps rather than kept for a
    // resemblance to the old shape.
    [onNotice],
  );

  // BLOCKER 2 (safety review, #291/#279): the wall-clock ceiling's
  // resolution, a THIRD outcome, distinct from both branches
  // `resolveUnverifiedSend` above handles. Not "a fresh read confirms
  // still pending" and not "a fresh read confirms gone": no qualifying
  // read ever arrived at all (see useResolveUnverifiedSends.ts's
  // `UNVERIFIED_CEILING_MS`). Unlike `resolveUnverifiedSend`'s silent
  // "still pending" branch, this ALWAYS notices: a guard that gives up
  // without saying so would just look like it healed itself, when what
  // actually happened is Stoop couldn't tell either way.
  const giveUpUnverifiedSend = useCallback(
    (draftId: string) => {
      const wasFlagged = clearUnverifiedSend(draftId);
      if (!wasFlagged) return;
      onNotice(UNVERIFIED_GIVE_UP_NOTICE);
      // BLOCKER 2 (safety review round 3, #291/#279): the toast above is
      // gone in a few seconds and was, before this fix, the ONLY trace
      // this ever happened. A landlord who steps away and comes back
      // past the two minute mark returned to a card that looked
      // untouched, with Approve live and the pre-edit body, which is
      // verbatim the F7 hazard this whole guard exists to prevent,
      // reintroduced on a timer. This sticky per-draft notice is what the
      // card renders instead once `isSendUnverified` (the flag just
      // cleared above) stops being true. FIX 3: written to the shared
      // module store now, not local state, see `giveUpNotices`'s own
      // comment above.
      setGiveUpNotice(draftId, UNVERIFIED_GIVE_UP_CARD_NOTICE);
      // BLOCKER 2: deliberately does NOT touch `editingContext`, unlike
      // the version of this function that used to run here. The give-up
      // ceiling firing is a clock, not a landlord action. A landlord who
      // left the editor open with two minutes of typed reply still
      // hasn't decided anything, and A7's rule ("Leave it open with the
      // text intact") applies to this closure exactly as much as it does
      // to any other failure that isn't `draft_not_found` (the editMutation
      // onError's NEW-4, the one case closing it is actually correct).
      // `sendDisabled` on the still-open editor simply goes false: Send
      // becomes reachable again with whatever the landlord already typed
      // still there.
    },
    [onNotice],
  );

  const markBusy = useCallback((draftId: string) => {
    setBusyDraftIds((prev) => {
      if (prev.has(draftId)) return prev;
      const next = new Set(prev);
      next.add(draftId);
      return next;
    });
    // BLOCKER 2: the landlord acting on this draft again (Approve, Skip,
    // or a new edit-and-send) is what retires the sticky give-up notice.
    // See that state's own comment above for why this, not a timer. FIX
    // 3: clears the shared module store now, not local state.
    clearGiveUpNotice(draftId);
  }, []);

  const clearBusy = useCallback((draftId: string) => {
    setBusyDraftIds((prev) => {
      if (!prev.has(draftId)) return prev;
      const next = new Set(prev);
      next.delete(draftId);
      return next;
    });
  }, []);

  // A8 (safety review, #234 PR 2): the stale-draft notice used to fire a
  // bare `setTimeout` with no stored id — never cleared on unmount (a
  // pending timer would still try to update state after the screen was
  // gone) and never cleared on a REPEAT call for the same case (a second
  // `draft_stale` within the 6s window would let the first timer delete
  // the second notice early). Timers are tracked per `caseId` here so a
  // repeat call resets its own timer instead of racing it, and the
  // cleanup effect below clears every outstanding one on unmount.
  const staleNoticeTimers = useRef<Record<string, ReturnType<typeof setTimeout>>>({});

  useEffect(() => {
    const timers = staleNoticeTimers.current;
    return () => {
      for (const timerId of Object.values(timers)) clearTimeout(timerId);
    };
  }, []);

  const showStaleNotice = useCallback((caseId: string, tenantName: string) => {
    setStaleNotices((prev) => ({ ...prev, [caseId]: draftStaleNotice(firstName(tenantName)) }));
    const existing = staleNoticeTimers.current[caseId];
    if (existing) clearTimeout(existing);
    staleNoticeTimers.current[caseId] = setTimeout(() => {
      delete staleNoticeTimers.current[caseId];
      setStaleNotices((prev) => {
        if (!(caseId in prev)) return prev;
        const next = { ...prev };
        delete next[caseId];
        return next;
      });
    }, 6000);
  }, []);

  // A1 (safety review, #234 PR 2): this only ever runs from a genuine
  // mutationFn rejection now — every `onSuccess` below catches its own
  // bookkeeping instead of letting it throw back into react-query's
  // pipeline, where a throwing `onSuccess` is otherwise indistinguishable
  // from the request itself failing (react-query's `execute()` wraps
  // mutationFn + onSuccess + onSettled in ONE try/catch and calls
  // `onError` for a throw from ANY of them). So "clear the optimistic
  // state" here is now only ever a response to the server actually
  // rejecting the call, never to our own post-success code slipping.
  //
  // NEW-2 (safety review round 3, #234 PR 2): `onSettled()` runs on EVERY
  // failure now, not just the codes that provably changed server state. A
  // `network_error` can mean "request applied, response lost" — the only
  // honest response is to re-ask the server what actually happened rather
  // than leave a card whose real state is unknown sitting approvable.
  const handleError = useCallback(
    (error: unknown, ctx: DraftContext) => {
      dispatch({ type: "cleared", draftId: ctx.draftId });
      if (error instanceof ApiError && error.code === "draft_stale") {
        showStaleNotice(ctx.caseId, ctx.tenantName);
      } else {
        onNotice(
          error instanceof ApiError
            ? toHouseApiError(error)
            : "Something didn't go through. Try again in a moment.",
        );
      }
      onSettled();
    },
    [onNotice, onSettled, showStaleNotice],
  );

  const approveMutation = useMutation({
    mutationFn: (ctx: DraftContext) => approveDraft(ctx.draftId),
    onSuccess: (result, ctx) => {
      try {
        const approvedAtClient = Date.now();
        dispatch({
          type: "approved",
          draftId: ctx.draftId,
          undoExpiresAtClient: computeUndoExpiresAt(
            result.data.undo_until,
            result.dateHeader,
            approvedAtClient,
          ),
          approvedAtClient,
        });
      } catch {
        // A1: the approve REQUEST already succeeded server-side — a throw
        // in this block is our own bookkeeping breaking, not a rejected
        // approve. Never dispatch "cleared" here (that would wrongly
        // revert the card to an approvable-looking state on a reply
        // that's already sending); refetch instead so the next server
        // read is the honest source of truth for this card.
        onNotice("That went through, but the on-screen countdown didn't update.");
        onSettled();
      }
    },
    onError: handleError,
    onSettled: (_data, _error, ctx) => clearBusy(ctx.draftId),
  });

  const undoMutation = useMutation({
    mutationFn: (ctx: UndoContext) => undoDraftApprove(ctx.draftId),
    // #256 comment item 2 (safety re-verify): this used to only clear the
    // local overlay — fine for Home, which self-heals on the queue's own
    // 20s poll (src/api/queue.ts), but the conversation thread's
    // `useCase` has no polling at all. Without this, a restored `pending`
    // draft's cached timeline entry stayed stuck on the undo's stale
    // "approved" snapshot until the landlord happened to refocus the
    // window or navigate away and back. Same onSettled() every other
    // server-confirmed transition in this hook already calls.
    onSuccess: (_data, ctx) => {
      // F8 (safety re-verify): guarded like every other post-success
      // block in this hook (the A1 convention). react-query routes an
      // `onSuccess` throw into `onError`, which here is `handleError`,
      // i.e. a "Something didn't go through" toast after an undo that
      // actually succeeded.
      //
      // #191 F5 (safety review follow-up): this used to be the one
      // outcome in this whole hook with no `onNotice` at all. Undo is the
      // control that saves the tenant from a wrong reply going out, and
      // it was the only success that stayed completely silent (every
      // error path here toasts). New string, flagged for copy-guardian.
      try {
        dispatch({ type: "undone", draftId: ctx.draftId });
        onSettled();
        onNotice("Undone. That reply won't go out.");
      } catch {
        onNotice("That went through, but the queue didn't refresh.");
      }
    },
    onError: (error, ctx) => {
      // M1 senior advisory (mobile, ported here): a 409 `already_sent` on
      // undo means the reply genuinely went out — the honest card state is
      // "sent", not a flash of the idle decision card (which would invite
      // a second approve tap on a reply that already left) while the
      // refetch catches up. `expired` only fires from "sending", which is
      // the only state Undo is ever offered from.
      if (error instanceof ApiError && error.code === "already_sent") {
        dispatch({ type: "expired", draftId: ctx.draftId });
        onNotice(toHouseApiError(error));
        onSettled();
        return;
      }
      // BLOCKER 1 (safety review, #291/#279): `draft_not_undoable` is,
      // like `already_sent`, a DEFINITIVE server signal: there is no
      // live send left for this draft to protect, so falling through to
      // `handleError`'s `cleared` dispatch (drop the local overlay
      // entirely) is correct here, same as it was before this fix.
      if (error instanceof ApiError && error.code === "draft_not_undoable") {
        handleError(error, ctx);
        return;
      }
      // Everything else (a dropped connection, a 5xx, a timeout, any
      // other code) is AMBIGUOUS: the DELETE may have applied server-side
      // and only the response was lost, so the reply may genuinely still
      // be on its way. `handleError` used to run for these too, and its
      // unconditional `dispatch({type: "cleared"})` deletes this draft's
      // local "sending" entry outright. `buildQueueView`
      // (queueEntries.ts) only ever pins a snapshot for a NON-idle entry
      // ("sending"/"sent"/"skipped"), so the instant the entry is
      // cleared, the pinned card, and the Undo control the landlord just
      // tapped and is still looking at, vanishes if a concurrent refetch
      // has already dropped the server row from `items` (#291's whole
      // reason for pinning in the first place), while the reply is, as
      // far as this client honestly knows, still sending. Toast so the
      // landlord knows the tap itself didn't confirm, refetch so the next
      // server read (not a client guess) settles it, but leave the entry
      // exactly where it was: "sending" is the one state that's still
      // true here.
      //
      // BLOCKER 1 (safety review ROUND 4, #291/#279): this is the ONE
      // place `undoAmbiguousAt` gets DISPATCHED. Whether it is STAMPED is
      // decided in the reducer, from the entry's status, and round 5
      // found that distinction load-bearing: round 4 accepted the
      // dispatch only while "sending", which dropped it for every
      // ambiguous failure slower than the 5 second countdown, i.e. nearly
      // all of them. See queueEntries.ts's
      // `undoAmbiguous` action), the positive evidence the two retirement
      // effects (src/routes/app.index.tsx, app.conversations.$id.tsx) gate
      // on instead of the false guarantee round 3's `dataUpdatedAt >
      // approvedAtClient` check assumed it had. See those effects' own
      // comments for the full reasoning; the short version is that
      // `dataUpdatedAt` is stamped when a response RESOLVES on the client,
      // not when the server computed it, so a read issued before ANY
      // approve and resolving after it could trip that comparison on a
      // draft nothing was ever attempted against. `undoAmbiguousAt` is
      // only ever set here, from a genuine ambiguous Undo failure, so no
      // stale read can trip it on a plain Approve.
      dispatch({
        type: "undoAmbiguous",
        draftId: ctx.draftId,
        at: Date.now(),
        approvedAtClient: ctx.approvedAtClient,
      });
      // F1 (safety review round 6): this used to be
      // `error instanceof ApiError ? toHouseApiError(error) : <honest line>`,
      // and the honest line was DEAD CODE. `apiRequest` wraps a dropped
      // connection into `new ApiError(0, { code: "network_error" })`, so
      // the instanceof is always true here and the landlord got
      // "Couldn't reach Stoop. Check your connection and try again.", or
      // for a 5xx the generic "Something didn't go through. Try again in
      // a moment." Both assert that NOTHING happened, in the one case
      // where Stoop provably cannot know, and both point at "try again"
      // on an Undo button that is gone by then.
      //
      // One unconditional, ambiguity-honest line instead, the same shape
      // the edit-and-send ambiguous branch below already uses (and for
      // the same reason it deliberately does not route through
      // `toHouseApiError` either).
      onNotice(UNDO_AMBIGUOUS_NOTICE);
      onSettled();
    },
    onSettled: (_data, _error, ctx) => clearBusy(ctx.draftId),
  });

  const skipMutation = useMutation({
    mutationFn: (ctx: DraftContext) => rejectDraft(ctx.draftId),
    // A6 (safety review, #234 PR 2): the previous cut never invalidated
    // the queue/counts after a successful skip — the header's counts
    // strip (and anything else reading `useQueue`) only caught up on the
    // next 20s poll or window-focus refetch. Approve/undo/edit don't need
    // this (their local "sending"/"sent" overlay IS the honest UI state
    // for those transitions, per queueEntries.ts's own docstring); skip
    // has no such overlay for the queue's own counts, so it refetches
    // immediately on success — same A1 split as every mutation above,
    // wrapped so a bookkeeping throw here can't masquerade as the reject
    // itself failing.
    onSuccess: () => {
      try {
        onSettled();
      } catch {
        onNotice("That went through, but the queue didn't refresh.");
      }
    },
    onError: handleError,
    onSettled: (_data, _error, ctx) => clearBusy(ctx.draftId),
  });

  const editMutation = useMutation({
    mutationFn: (ctx: DraftContext & { body: string }) => editAndSendDraft(ctx.draftId, ctx.body),
    onSuccess: (result, ctx) => {
      try {
        const approvedAtClient = Date.now();
        dispatch({
          type: "approved",
          draftId: ctx.draftId,
          undoExpiresAtClient: computeUndoExpiresAt(
            result.data.undo_until,
            result.dateHeader,
            approvedAtClient,
          ),
          approvedAtClient,
        });
        setEditingContext(null);
      } catch {
        // A1, same reasoning as approve's onSuccess above — the
        // edit-and-send request already succeeded.
        onNotice("That went through, but the on-screen countdown didn't update.");
        onSettled();
        setEditingContext(null);
      }
    },
    onError: (error, ctx) => {
      // NEW-4 (safety review round 3, #234 PR 2): a 404'd draft means the
      // thing being edited no longer exists server-side — with A7 keeping
      // the editor pinned open, leaving it up would invite retrying
      // forever against a draft that's gone. Closing it here is the one
      // case where discarding the typed text is correct.
      if (error instanceof ApiError && error.code === "draft_not_found") {
        setEditingContext(null);
        handleError(error, ctx);
        return;
      }
      // NEW-2 (safety review round 3, #234 PR 2): an AMBIGUOUS failure —
      // network error or 5xx — can mean the edit-and-send actually applied
      // server-side and only the response was lost. The API's approve path
      // is idempotent for a draft already in the approved family (it
      // returns 200 WITHOUT applying a new body), so a retype-and-resend
      // after a lost response would silently send the FIRST text while
      // showing a countdown for the second. Say so, and let the
      // unconditional refetch below bring back the honest state (if the
      // edit applied, the card leaves the queue).
      //
      // R3-2 (safety review round 3 follow-up, issue #252): ambiguity is
      // about whether the server actually NAMED a documented failure —
      // `network_error` (a genuinely dropped connection/timeout) or a real
      // 5xx — never a bare `status === 0`. `server_context`/`not_configured`
      // (src/api/client.ts) also carry status 0, but both are thrown BEFORE
      // any `fetch` call is made; there is nothing ambiguous about a
      // request that provably never left the browser, so telling the
      // landlord it "may have gone through" would be the dishonest,
      // silence-inducing direction for those two codes specifically.
      // F5 (safety re-verify, #252): an unparsable 2xx body counts as
      // ambiguous too — the server answered 2xx, so the edit-and-send
      // DEFINITELY applied; we just couldn't read the confirmation. That
      // is the one case where a retype-and-resend is most certain to
      // deliver the wrong body, so it must raise the guard rather than
      // fall through to "try again in a moment".
      if (
        error instanceof ApiError &&
        (error.code === "network_error" ||
          error.status >= 500 ||
          (error.code === "unknown_error" && error.status >= 200 && error.status < 300))
      ) {
        dispatch({ type: "cleared", draftId: ctx.draftId });
        // R3-1: track this draft as unverified so Send stays disabled
        // until a later successful read resolves it one way or the other.
        // F1: stamped with `Date.now()`, which is directly comparable to
        // TanStack's `dataUpdatedAt` (same clock, same units) — the screen
        // refuses to resolve against any read that didn't complete AFTER
        // this moment. No plumbing needed for the query's generation.
        // #279: written to the shared module-scope store (not local
        // state) so this flag is visible to whichever route, this one or
        // the other, ends up resolving it.
        const failedAt = Date.now();
        markUnverifiedSend(ctx.draftId, failedAt);
        onNotice("That may have gone through. Give it a moment to update before sending again.");
        onSettled();
        return;
      }
      // A7 (safety review, #234 PR 2): everything else used to
      // unconditionally close the editor here (`setEditingContext(null)`)
      // on ANY failure — from a dropped connection to `draft_stale` —
      // throwing away whatever the landlord had just typed. Leave it open
      // with the text intact; the landlord decides whether to retry Send
      // or tap Cancel themselves (Cancel already closes it deliberately,
      // as a landlord-initiated action).
      handleError(error, ctx);
    },
    onSettled: (_data, _error, ctx) => clearBusy(ctx.draftId),
  });

  // Ticks any live undo countdown once a second; the effect below then
  // flips a countdown that's genuinely hit zero from "sending" to "sent".
  useEffect(() => {
    const hasSending = Object.values(entries).some((entry) => entry.status === "sending");
    if (!hasSending) return;
    const timer = setInterval(forceTick, 1000);
    return () => clearInterval(timer);
  }, [entries]);

  // `tick` is a real dependency — the reducer above increments it once a
  // second while anything is "sending", and wall-clock time (what
  // `secondsRemaining` reads) only moves the outcome between ticks.
  useEffect(() => {
    for (const [draftId, entry] of Object.entries(entries)) {
      if (entry.status === "sending" && secondsRemaining(entry.undoExpiresAtClient) <= 0) {
        dispatch({ type: "expired", draftId });
      }
    }
  }, [entries, tick]);

  return {
    entries,
    dispatch,
    staleNotices,
    /** BLOCKER 2: the give-up ceiling's own sticky per-draft notice
     *  (`draftId -> message`). See this hook's own `giveUpNotices`
     *  comment above for where the map itself lives now (FIX 3, round 4).
     *
     *  Round 4 correction: this is NOT simply "wherever
     *  `UNVERIFIED_SEND_NOTICE` was showing" as a prior revision of this
     *  comment claimed, `isSendUnverified` for the same draft id is
     *  false again by the time this has anything in it, but that only
     *  covers the CARD'S own notice line. The editor
     *  (EditDraftPanel.tsx's `notice` prop) is a SEPARATE render target
     *  that needs this passed in explicitly too; both call sites
     *  (src/routes/app.index.tsx's DecisionCard, app.conversations.$id
     *  .tsx's own editing branch) now do. */
    giveUpNotices,
    editingContext,
    /** A2: true while an approve/undo/skip/edit-and-send is in flight for
     *  THIS draft id specifically — never a global "something is
     *  happening" flag. */
    isBusy: (draftId: string) => busyDraftIds.has(draftId),
    /** R3-1: true while THIS draft's last edit-and-send ended in an
     *  ambiguous failure and hasn't been resolved yet — the screen should
     *  keep its Send control disabled for exactly this draft id. */
    isSendUnverified: (draftId: string) => unverifiedSendIds.has(draftId),
    /** R3-1: every draft id currently unverified — a caller iterates this
     *  against its own next successful read to call `resolveUnverifiedSend`
     *  below (see src/routes/app.index.tsx). */
    unverifiedSendIds,
    resolveUnverifiedSend,
    /** BLOCKER 2: the wall-clock ceiling's own release, see this
     *  callback's own comment above and useResolveUnverifiedSends.ts's
     *  `UNVERIFIED_CEILING_MS`. */
    giveUpUnverifiedSend,
    approve: (ctx: DraftContext) => {
      markBusy(ctx.draftId);
      approveMutation.mutate(ctx);
    },
    undo: (ctx: DraftContext) => {
      // Read the cycle stamp off the live entry (see `UndoContext`). Undo
      // is only ever offered from a `sending` entry, so this is present
      // whenever this is reachable; the fallback keeps the reducer's own
      // equality check honest rather than stamping a cycle that is not
      // this one.
      const entry = entries[ctx.draftId];
      const approvedAtClient = entry?.status === "sending" ? entry.approvedAtClient : Number.NaN;
      markBusy(ctx.draftId);
      undoMutation.mutate({ ...ctx, approvedAtClient });
    },
    skip: (ctx: DraftContext) => {
      markBusy(ctx.draftId);
      dispatch({ type: "skipped", draftId: ctx.draftId });
      skipMutation.mutate(ctx);
    },
    openEditor: (ctx: DraftContext, body: string) => setEditingContext({ ...ctx, body }),
    cancelEditor: () => setEditingContext(null),
    submitEdit: (body: string) => {
      if (!editingContext) return;
      markBusy(editingContext.draftId);
      editMutation.mutate({ ...editingContext, body });
    },
    isEditSubmitting: editMutation.isPending,
  };
}
