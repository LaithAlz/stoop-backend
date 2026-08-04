import { createFileRoute, Link } from "@tanstack/react-router";
import { useCallback, useEffect, useMemo, useRef, useState, type Ref } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, ChevronLeft, Loader2 } from "lucide-react";
import { toast } from "sonner";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { SeverityPlaque } from "@/components/clarity/SeverityPlaque";
import { EmergencyBanner } from "@/components/clarity/EmergencyBanner";
import { DayDivider } from "@/components/clarity/DayDivider";
import { ThreadMessageRow } from "@/components/clarity/ThreadMessageRow";
import { AuditMetaLine } from "@/components/clarity/AuditMetaLine";
import { DraftBubble } from "@/components/clarity/DraftBubble";
import { StaleDraftBubble } from "@/components/clarity/StaleDraftBubble";
import { DecisionActions } from "@/components/clarity/DecisionActions";
import { MarginNote } from "@/components/clarity/MarginNote";
import { UndoTicket } from "@/components/clarity/UndoTicket";
import { EditDraftPanel, UNVERIFIED_SEND_NOTICE } from "@/components/clarity/EditDraftPanel";
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { useAuth } from "@/auth/AuthProvider";
import { useCase, resolveCase, caseQueryKey, casesQueryKey } from "@/api/cases";
import { useQueue, queueQueryKey } from "@/api/queue";
import { ApiError, toHouseApiError } from "@/api/errors";
import type {
  CaseDetail,
  ClassifiedAuditPayload,
  TimelineDraftEntry,
  TimelineEntry,
} from "@/api/types";
import { firstName } from "@/lib/tenantName";
import { entryFor, secondsRemaining, totalUndoSeconds } from "@/features/queue/queueEntries";
import { useDraftActions } from "@/features/queue/useDraftActions";
import { useResolveUnverifiedSends } from "@/features/queue/useResolveUnverifiedSends";
import { emergencyHeadline, emergencySubtext } from "@/features/emergency/emergencyBanner";
import { buildTimelineRows, type TimelineRow } from "@/features/cases/timeline";
import { isEmergencySignal } from "@/features/cases/emergencySignal";
import {
  RESOLVE_CONFIRM_LABEL,
  RESOLVE_CONFIRM_MESSAGE,
  RESOLVE_CONFIRM_TITLE,
  RESOLVE_DONE_NOTICE,
} from "@/features/cases/resolveCase";

export const Route = createFileRoute("/app/conversations/$id")({
  head: ({ params }) => ({
    meta: [{ title: "Conversation. Stoop." }, { name: "robots", content: "noindex" }],
    links: [{ rel: "canonical", href: `/app/conversations/${params.id}` }],
  }),
  // N2 (safety re-verify, #234 PR 3): TanStack Router REUSES the component
  // instance across /conversations/A → /conversations/B (no key on the
  // match unless remountDeps says so — verified in the router source). This
  // screen now holds per-case local state (`pinnedDraft`, useDraftActions'
  // overlay entries, `editingContext`) that must never survive a case
  // switch: case B rendering case A's pinned draft body — or an open editor
  // wired to A's draft id over B's timeline — is an approval-context
  // mismatch on the "nothing sends without landlord approval" path. Not
  // reachable through today's link graph (thread→thread always routes via
  // the list), but that's an accident of the links, not an invariant.
  remountDeps: ({ params }) => params.id,
  component: ConversationPage,
});

/** Matches DecisionCard.tsx's own fallback verbatim (same web app, same
 *  surface family) rather than apps/mobile's differently-worded one — that
 *  cross-platform drift is a pre-existing, already-flagged gap
 *  (DecisionCard.tsx's own comment), not something this PR resolves. */
const DEFAULT_WHY = "I drafted this from your house rules and past replies.";

/**
 * The full conversation thread — wired to `GET /v1/cases/{id}` (campaign
 * issue #234 PR 3, replacing src/lib/mock-app.ts's `getConversation`).
 * Loading/error states follow src/routes/app.index.tsx's exact pattern:
 * `isPending` for the first load, a full takeover ONLY when
 * `isError && !data`, a quiet refresh strip otherwise. The pending draft's
 * approve/undo/skip/edit-and-send reuses src/features/queue/
 * useDraftActions.ts unchanged — the same hook Home uses — per that file's
 * own docstring ("a later PR wiring the conversation thread's own approve/
 * undo/reject can reuse this unchanged").
 *
 * NOTE (api-contracts.md's Cases section, v1.15 amendment on the Queue
 * section): `GET /v1/cases/{id}` carries no `notification_id` — that field
 * is scoped to the queue card only — so this screen's `EmergencyBanner`
 * never gets an `onAcknowledge`. The acknowledge action lives on Home
 * only, the sole surface the contract actually wires it to.
 */
function ConversationPage() {
  const { id } = Route.useParams();
  const { session } = useAuth();
  const queryClient = useQueryClient();
  const caseQuery = useCase(id, { enabled: Boolean(session) });
  const queueQuery = useQueue({ enabled: Boolean(session) });
  const bottomRef = useRef<HTMLDivElement>(null);

  const onNotice = useCallback((message: string) => toast(message), []);
  const onSettled = useCallback(() => {
    void queryClient.invalidateQueries({ queryKey: caseQueryKey(id) });
    void queryClient.invalidateQueries({ queryKey: queueQueryKey });
  }, [queryClient, id]);
  const draftActions = useDraftActions({ onNotice, onSettled });

  // #279: this route never wired the #252 unverified-send guard at all:
  // zero references to `isSendUnverified`/`unverifiedSendIds` before this
  // fix, so an ambiguous edit-and-send failure here raised the flag
  // (useDraftActions.ts) and nothing ever resolved it, leaving Send fully
  // enabled through the exact window it exists to close. `queueQuery` is
  // already fetched on this route (for the tab bar's badge count below)
  // and is a valid resolution source for ANY draft id, not just ones on
  // this case (see useResolveUnverifiedSends.ts's own docstring).
  useResolveUnverifiedSends({
    data: queueQuery.data,
    dataUpdatedAt: queueQuery.dataUpdatedAt,
    unverifiedSendIds: draftActions.unverifiedSendIds,
    resolveUnverifiedSend: draftActions.resolveUnverifiedSend,
    giveUpUnverifiedSend: draftActions.giveUpUnverifiedSend,
  });

  const caseDetail = caseQuery.data;
  const tenantFirst = firstName(caseDetail?.tenant.name);

  useEffect(() => {
    if (caseDetail) bottomRef.current?.scrollIntoView({ behavior: "auto" });
    // Deliberately keyed on the case id only, not the whole `caseDetail`
    // object — a background refetch (new timeline entries, a stale-draft
    // notice) must not yank the landlord back to the bottom mid-read; only
    // navigating to a genuinely different conversation should.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [caseDetail?.id]);

  const rows = useMemo<TimelineRow[]>(
    () => (caseDetail ? buildTimelineRows(caseDetail.timeline) : []),
    [caseDetail],
  );

  const isPendingDraft = (entry: TimelineEntry): entry is TimelineDraftEntry =>
    entry.kind === "draft" && entry.status === "pending";
  const livePendingDraft = caseDetail?.timeline.find(isPendingDraft);

  // M4 (safety review, #234 PR 3 fix round): `livePendingDraft` is looked
  // up fresh from `caseDetail.timeline` on every render — the moment an
  // approve/skip actually applies server-side, that draft's `status`
  // moves off `"pending"`, so a background refetch mid-action (React
  // Query's `staleTime`/`refetchOnWindowFocus`) makes `livePendingDraft`
  // disappear even while the LOCAL overlay (`draftActions.entries`) is
  // still honestly "sending" with a live 5s undo countdown on screen.
  // Pinning the id/body the first time we see them — and continuing to
  // use the pin for as long as the local entry for that id is non-idle —
  // keeps the footer rendered through that window instead of blanking it
  // out from under an active Undo ticket. Mirrors Home's own pinned-item
  // pattern for the identical reason (src/features/queue/queueEntries.ts's
  // `pinnedEditingItem`).
  const [pinnedDraft, setPinnedDraft] = useState<{ id: string; body: string } | null>(null);
  useEffect(() => {
    if (livePendingDraft) {
      setPinnedDraft({ id: livePendingDraft.id, body: livePendingDraft.body });
    }
    // `livePendingDraft` is a NEW object from `.find()` every render —
    // depending on the two primitive fields this effect actually reads
    // (rather than the object reference) is deliberate, not an omission.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [livePendingDraft?.id, livePendingDraft?.body]);

  const pinnedEntry = pinnedDraft ? entryFor(draftActions.entries, pinnedDraft.id) : undefined;
  // N1 (safety re-verify, #234 PR 3): the pin only ever needs to survive
  // the UNDO WINDOW ("sending"). Keeping it for any non-idle status meant
  // that after the countdown expired ("sent") the footer kept rendering
  // the same body as a "I'd like to reply" DraftBubble with "Sent."
  // under it — the landlord's reply shown twice, the second copy dressed
  // as still-queued, on the screen whose whole promise is the record. A
  // skipped draft deliberately falls back to the timeline's own "You
  // skipped this reply — case stayed open." audit line (the honest
  // thread-native representation; Home's muted skip CARD is a queue
  // ruling, not a thread one).
  const keepPinned = Boolean(!livePendingDraft && pinnedDraft && pinnedEntry?.status === "sending");

  const draftId = livePendingDraft?.id ?? (keepPinned ? pinnedDraft?.id : undefined);
  const draftBody = livePendingDraft?.body ?? (keepPinned ? pinnedDraft?.body : undefined);
  const draftEntry = draftId
    ? entryFor(draftActions.entries, draftId)
    : { status: "idle" as const };

  // M5 (safety review, #234 PR 3 fix round): the editor, once open, must
  // render from `useDraftActions`' OWN pinned `editingContext` (draft id +
  // body captured at open time) — never from the live/pinned `draftId`
  // above. A tenant reply arriving mid-type makes the OLD draft `stale`
  // and drafts a NEW one with a different id; deriving the editor's
  // "am I still open for this draft" from the live `draftId` would flip
  // to false the instant that happens and unmount `EditDraftPanel` with
  // whatever the landlord had just typed still in it.
  const editingContext = draftActions.editingContext;

  // #191 item 1: same focus-return pattern as DecisionCard's own copy of
  // this effect (src/components/clarity/DecisionCard.tsx). This route has
  // no single "card" to land on, so `draftAreaRef` (the wrapper below that
  // holds the editor/footer either way) stands in for it. The Edit button
  // unmounts the instant `editingContext` is set and only remounts if the
  // landlord cancels back out, never on a successful edit-and-send, so
  // `editButtonRef.current` being null is the normal, expected "not
  // reachable" case, not a bug.
  const editButtonRef = useRef<HTMLButtonElement>(null);
  const draftAreaRef = useRef<HTMLDivElement>(null);
  const wasEditingRef = useRef(Boolean(editingContext));
  useEffect(() => {
    const isEditing = Boolean(editingContext);
    // #191 F2/F4 (safety review re-verify): this used to fire on ANY
    // isEditing true -> false edge, including a successful edit-and-send,
    // which moves `draftEntry.status` straight to "sending" rather than
    // back to idle. That would race the row-level effect below (added
    // for F3), which now focuses the Undo button on that same
    // transition: without this check both would move focus in one
    // commit for a single user action. Checking `draftEntry.status` at
    // close time narrows this effect to the one case it is actually for:
    // Cancel, or the `draft_not_found` close, neither of which lands on
    // "sending".
    if (wasEditingRef.current && !isEditing && draftEntry.status !== "sending") {
      // F6 (safety review, #191 follow-up): only move focus if it was
      // plausibly here. Either it's still literally inside the draft
      // area, or it was reset to <body> because the element that had it
      // was just removed as part of THIS transition. Concretely: a
      // landlord who taps Send and then immediately taps something else
      // (the "Resolve case" button below, say) while `editAndSendDraft`
      // is still in flight would, without this guard, get their focus
      // and the page's scroll yanked back to this editor's remains once
      // the request resolves, well after they'd already moved on. This
      // also covers `resolveUnverifiedSend`'s own `setEditingContext(null)`
      // (useDraftActions.ts): as of #279 this route's `queueQuery` feeds
      // `useResolveUnverifiedSends`, which can call it, so this hijack
      // shape is reachable here too, not just on Home.
      const active = document.activeElement;
      if (draftAreaRef.current?.contains(active) || active === document.body) {
        // Cancel just closed the editor. Land focus on the Edit button
        // if it came back, otherwise the draft area, so a keyboard user
        // is never dropped onto <body>.
        // F8 (re-verify): `.isConnected` alone is not enough. `.focus()`
        // on a DISABLED button is also a silent no-op that never reaches
        // the fallback below. #191 round 4 item 6 flagged this as
        // unreachable on THIS screen at the time, because the route
        // passed no `sendDisabled` into `EditDraftPanel` and `isBusy`
        // never folded in `isSendUnverified`. #279 fixed exactly that
        // gap, so the path IS reachable here now, the same as
        // DecisionCard's: an ambiguous edit-and-send sets
        // `isSendUnverified`, `DraftFooter`'s `isBusy` stays true off
        // that alone, the landlord taps Cancel (never blocked by
        // `isBusy`), and Edit remounts connected but disabled, inside the
        // #252 danger window. This fallback is what recovers the
        // keyboard user's focus onto the draft area instead of `<body>`
        // when that happens.
        const btn = editButtonRef.current;
        if (btn?.isConnected && !btn.disabled) {
          btn.focus();
        } else {
          draftAreaRef.current?.focus();
        }
      }
    }
    wasEditingRef.current = isEditing;
  }, [editingContext, draftEntry.status]);

  // #191 F3 (safety review re-verify): the identical three transitions
  // exist here as in Home's QueueRow (src/routes/app.index.tsx): Approve
  // moves `draftEntry.status` to "sending" (the Undo ticket takes
  // DecisionActions' place), Skip replaces the whole footer with the
  // thread's own "No reply sent" line (DraftFooter's `isSkipped`
  // branch), and a successful Undo moves it back from "sending" to idle
  // (the actions row reappears). Issue #191 filed this item under "From
  // PR #190 (conversation thread)", and the first pass at this fix only
  // ever touched Home's QueueRow. This mirrors it here, on the surface
  // the item was actually about.
  const undoButtonRef = useRef<HTMLButtonElement>(null);
  const prevDraftStatusRef = useRef(draftEntry.status);
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
    const prevStatus = prevDraftStatusRef.current;
    prevDraftStatusRef.current = draftEntry.status;
    if (prevStatus === draftEntry.status) return;
    // #191 round 4 item 1 (safety review re-verify): `sent -> idle` is the
    // pinned draft's own cleanup settling once the server refetch this
    // screen kicks off on "sent" (the effect a little below that clears
    // `pinnedDraft`) catches up, never a landlord action, and there's
    // nothing to land on by then anyway: the draft area's idle content is
    // a static disclaimer, not a control. Exempt outright, the same as
    // the timer-driven transition right below.
    if (prevStatus === "sent") return;
    // #191 round 4 item 4 (safety review re-verify): `sending -> sent` is
    // the five-second countdown simply running out, a timer, never a
    // landlord action by itself. Only stay exempt when Undo did NOT
    // plausibly hold focus right up to the moment it was removed (a
    // landlord who has already moved their attention elsewhere on the
    // page); otherwise fall through to the same recovery every other
    // transition here gets, so a keyboard user whose focus was
    // legitimately on Undo isn't silently dumped onto `<body>` with
    // nothing to land on and no announcement.
    if (prevStatus === "sending" && draftEntry.status === "sent" && !undoHadFocusRef.current) {
      return;
    }
    // F6: only steal focus if it was plausibly inside the draft area,
    // either still literally there, or reset to <body> because the
    // control that had it was just removed as part of this transition.
    const active = document.activeElement;
    if (!(draftAreaRef.current?.contains(active) || active === document.body)) return;
    if (draftEntry.status === "sending") {
      // F2/F4: Approve or a successful edit-and-send. Focus the Undo
      // button itself, not just the draft area, so its accessible name
      // and its `aria-describedby` text (UndoTicket.tsx) are read
      // together at the instant they matter.
      const btn = undoButtonRef.current;
      if (btn?.isConnected && !btn.disabled) {
        btn.focus();
        return;
      }
    }
    // `preventScroll` (round 5 re-verify): same reasoning as QueueRow's.
    // This landing is reached on transitions the landlord did not
    // initiate, and on a long thread `focus()` would scroll them to the
    // bottom, away from whatever they had scrolled up to read. On an
    // emergency case that scroll can also push the EmergencyBanner off a
    // phone-sized viewport with no user action at all. Recover the
    // focus, leave the viewport alone.
    draftAreaRef.current?.focus({ preventScroll: true });
  }, [draftEntry.status]);
  // #191 round 4 item 4 (safety review re-verify): refreshed every
  // render, not only on a status change, so it reflects the freshest real
  // focus state right up to the render before "sending" flips to "sent"
  // (see `undoHadFocusRef`'s own comment above).
  useEffect(() => {
    if (draftEntry.status === "sending") {
      undoHadFocusRef.current = document.activeElement === undoButtonRef.current;
    }
  });

  // Once the local approve overlay settles on "sent", the server's own
  // timeline (a real outbound `message` entry replacing the drafted one,
  // per src/features/cases/timeline.ts's docstring) is the honest next
  // read — refetch instead of waiting for whatever next triggers a fetch.
  // Watches the PINNED entry, not `draftEntry`: N1's `keepPinned` drops to
  // false the instant the entry leaves "sending", which zeroes `draftId`
  // and would make `draftEntry` skip straight to "idle" without this
  // effect ever seeing "sent". Clearing the pin here also retires it for
  // good once the send is final.
  useEffect(() => {
    if (pinnedEntry?.status === "sent") {
      void queryClient.invalidateQueries({ queryKey: caseQueryKey(id) });
      setPinnedDraft(null);
    }
  }, [pinnedEntry?.status, queryClient, id]);

  const isClassifiedAudit = (
    entry: TimelineEntry,
  ): entry is Extract<TimelineEntry, { kind: "audit" }> =>
    entry.kind === "audit" && entry.action === "classified";
  const classifiedEntry = caseDetail?.timeline.find(isClassifiedAudit);
  // M6 (safety review, #234 PR 3 fix round): `payload` is an opaque jsonb
  // blob (ClassifiedAuditPayload is a read-time cast, never a guaranteed
  // shape — see that type's own comment in src/api/types.ts) — a
  // non-string `summary` reaching JSX here would throw at render time and
  // white-screen the whole thread. Guarded rather than trusted.
  const rawWhy = (classifiedEntry?.payload as ClassifiedAuditPayload | undefined)?.summary;
  const why = typeof rawWhy === "string" ? rawWhy : DEFAULT_WHY;

  const caseIsOpen = caseDetail ? caseDetail.status !== "resolved" : false;
  // H1 (safety review, #234 PR 3 fix round): never let a `null` severity
  // (transient pre-classification, or permanent degraded mode — see
  // src/features/cases/emergencySignal.ts's full writeup) suppress a real
  // Tier-0 emergency signal the timeline otherwise carries.
  const showEmergencyBanner = Boolean(caseDetail && caseIsOpen && isEmergencySignal(caseDetail));

  const [resolveConfirmOpen, setResolveConfirmOpen] = useState(false);
  const resolveMutation = useMutation({
    mutationFn: () => resolveCase(id),
    onSuccess: () => {
      toast(RESOLVE_DONE_NOTICE);
      onSettled();
      // A resolve moves this case out of every open-case list — the
      // Conversations tab's own filters must not keep showing it as open
      // until the next fetch.
      void queryClient.invalidateQueries({ queryKey: casesQueryKey() });
      setResolveConfirmOpen(false);
    },
    onError: (error) => {
      onNotice(
        error instanceof ApiError
          ? toHouseApiError(error)
          : "Something didn't go through. Try again in a moment.",
      );
      setResolveConfirmOpen(false);
    },
  });

  return (
    <PhoneFrame>
      <div className="flex flex-1 flex-col bg-clarity-bg">
        <ThreadHeader caseDetail={caseDetail} tenantFirst={tenantFirst} showPlaque={caseIsOpen} />

        <main className="flex-1 overflow-y-auto px-[18px] py-4">
          {caseQuery.isPending ? (
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
                Loading this conversation…
              </p>
            </div>
          ) : !caseDetail ? (
            // H3 (safety review, #234 PR 3 fix round): this branch is now
            // "no data, whatever the reason" (a genuine fetch error, OR —
            // belt-and-braces — the defensive case a fixed
            // src/api/client.ts should no longer be able to produce: a 2xx
            // that somehow resolved to no usable body). Previously this
            // was `caseQuery.isError && !caseDetail`, with a bare `null`
            // trailing branch below for the "not error, but also no data"
            // gap — a blank screen instead of a readable one.
            <div role="alert" className="flex flex-col items-center gap-3 py-16 text-center">
              <p className="font-clarity-sans text-sm font-semibold text-clarity-ink-dim">
                {caseQuery.error instanceof ApiError
                  ? toHouseApiError(caseQuery.error)
                  : "Couldn't load this conversation. Try again."}
              </p>
              <button
                type="button"
                onClick={() => void caseQuery.refetch()}
                className="inline-flex min-h-12 items-center justify-center rounded-clarity-md border-[1.5px] border-clarity-brand-deep bg-clarity-brand px-5 font-clarity-sans text-[15px] font-extrabold text-clarity-brand-on shadow-clarity-banner transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0"
              >
                Try again
              </button>
            </div>
          ) : (
            <>
              {caseQuery.isError && (
                <div
                  role="status"
                  className="mb-3.5 rounded-clarity-md border border-clarity-line-strong bg-clarity-panel px-4 py-2.5 font-clarity-sans text-[13px] font-semibold text-clarity-ink-dim"
                >
                  Couldn&apos;t refresh just now. Showing the last update.
                </div>
              )}

              {showEmergencyBanner && (
                <EmergencyBanner
                  conversationId={caseDetail.id}
                  headline={emergencyHeadline({
                    title: caseDetail.title,
                    tenant_name: caseDetail.tenant.name ?? "",
                    property_label: caseDetail.property.label,
                  })}
                  subtext={emergencySubtext({ property_label: caseDetail.property.label })}
                  className="mb-4"
                />
              )}

              {rows.map((row) => renderTimelineRow(row, tenantFirst))}

              <div
                ref={draftAreaRef}
                tabIndex={-1}
                role="group"
                // #191 round 4 item 5 (safety review re-verify): this
                // wrapper covers three branches (editing, the pending
                // draft's own footer, and, once the case is resolved or
                // draftless, a bare disclaimer paragraph with nothing to
                // reply to). Naming it "Reply to {tenantFirst}"
                // unconditionally put a screen-reader user landing on a
                // resolved case inside a group named "Reply to Maria"
                // that in fact holds only "nothing here can be edited or
                // removed once it's sent". Only name it when there is
                // actually something to reply with; the plain disclaimer
                // branch stays unlabeled (its own text is read directly).
                aria-label={
                  editingContext || (draftId && draftBody !== undefined)
                    ? `Reply to ${tenantFirst}`
                    : undefined
                }
              >
                {editingContext ? (
                  <div className="mt-2">
                    <EditDraftPanel
                      tenantName={tenantFirst}
                      initialBody={editingContext.body}
                      submitting={draftActions.isEditSubmitting}
                      // #279: this route passed no `sendDisabled` at all,
                      // so an ambiguous edit-and-send left Send fully
                      // live here: the exact gap this issue closes. Same
                      // prop Home's DecisionCard already threads through.
                      sendDisabled={draftActions.isSendUnverified(editingContext.draftId)}
                      onCancel={() => draftActions.cancelEditor()}
                      onSend={(body) => draftActions.submitEdit(body)}
                    />
                    {draftActions.staleNotices[caseDetail.id] && (
                      <p className="mt-2.5 text-right font-clarity-sans text-[13px] font-semibold text-clarity-brand">
                        {draftActions.staleNotices[caseDetail.id]}
                      </p>
                    )}
                  </div>
                ) : draftId && draftBody !== undefined ? (
                  <div className="mt-2">
                    <DraftFooter
                      tenantFirst={tenantFirst}
                      draftBody={draftBody}
                      draftEntry={draftEntry}
                      why={why}
                      // F7 (#252, mirrored from Home's DecisionCard): once
                      // the landlord cancels back out of the editor, the
                      // action row itself needs the same explanation for
                      // why it's still locked, since the toast that raised
                      // the guard is long gone by then.
                      staleNotice={
                        draftActions.staleNotices[caseDetail.id] ??
                        (draftActions.isSendUnverified(draftId)
                          ? UNVERIFIED_SEND_NOTICE
                          : undefined)
                      }
                      // #279: OR'd with `isSendUnverified`, same as Home's
                      // `actionsBusy`: Approve/Edit stay locked while this
                      // draft's last edit-and-send is still unresolved, so
                      // a landlord can't tap Approve and silently send the
                      // ORIGINAL, un-edited body while its fate is
                      // unknown.
                      isBusy={
                        draftActions.isBusy(draftId) || draftActions.isSendUnverified(draftId)
                      }
                      // BLOCKER 2 / item 7 (safety review, #291/#279):
                      // mutation-only busy, deliberately NEVER OR'd with
                      // `isSendUnverified`. Gates Skip (the escape hatch
                      // that must survive a locked Approve/Edit) and Undo
                      // (see DecisionCard's own `mutationBusy` comment,
                      // the identical reasoning on the other surface).
                      mutationBusy={draftActions.isBusy(draftId)}
                      editButtonRef={editButtonRef}
                      undoButtonRef={undoButtonRef}
                      onApprove={() =>
                        draftActions.approve({
                          draftId,
                          caseId: caseDetail.id,
                          tenantName: caseDetail.tenant.name ?? "",
                        })
                      }
                      onEdit={() =>
                        draftActions.openEditor(
                          {
                            draftId,
                            caseId: caseDetail.id,
                            tenantName: caseDetail.tenant.name ?? "",
                          },
                          draftBody,
                        )
                      }
                      onSkip={() =>
                        draftActions.skip({
                          draftId,
                          caseId: caseDetail.id,
                          tenantName: caseDetail.tenant.name ?? "",
                        })
                      }
                      onUndo={() =>
                        draftActions.undo({
                          draftId,
                          caseId: caseDetail.id,
                          tenantName: caseDetail.tenant.name ?? "",
                        })
                      }
                    />
                  </div>
                ) : (
                  <p className="mt-4 font-clarity-sans text-xs leading-relaxed text-clarity-ink-dim">
                    Nothing here can be edited or removed once it&rsquo;s sent. That&rsquo;s what
                    makes it useful if you ever need the record.
                  </p>
                )}
              </div>

              {caseIsOpen && (
                <div className="mt-6 flex flex-col items-start gap-1">
                  <button
                    type="button"
                    onClick={() => setResolveConfirmOpen(true)}
                    disabled={resolveMutation.isPending}
                    className="inline-flex min-h-11 items-center gap-1.5 font-clarity-sans text-sm font-extrabold text-clarity-brand disabled:opacity-60"
                  >
                    <Check className="size-4" aria-hidden="true" />
                    {resolveMutation.isPending ? "Resolving…" : RESOLVE_CONFIRM_LABEL}
                  </button>
                  <p className="font-clarity-sans text-xs text-clarity-ink-dim">
                    Closes the case. A drafted reply that hasn&rsquo;t sent won&rsquo;t go out.
                  </p>
                </div>
              )}

              <div ref={bottomRef} />
            </>
          )}
        </main>

        <AppTabBar active="conversations" queueCount={queueQuery.data?.counts.total ?? 0} />
      </div>

      <AlertDialog open={resolveConfirmOpen} onOpenChange={setResolveConfirmOpen}>
        <AlertDialogContent className="border-clarity-line-strong bg-clarity-surface">
          <AlertDialogHeader>
            <AlertDialogTitle className="font-clarity-serif text-clarity-ink">
              {RESOLVE_CONFIRM_TITLE}
            </AlertDialogTitle>
            <AlertDialogDescription className="font-clarity-sans text-clarity-ink-dim">
              {RESOLVE_CONFIRM_MESSAGE}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel className="border-[1.5px] border-clarity-line-strong bg-clarity-panel font-clarity-sans font-extrabold text-clarity-ink-dim">
              Cancel
            </AlertDialogCancel>
            <AlertDialogAction
              onClick={() => resolveMutation.mutate()}
              disabled={resolveMutation.isPending}
              className="border-[1.5px] border-clarity-brand-deep bg-clarity-brand font-clarity-sans font-extrabold text-clarity-brand-on hover:bg-clarity-brand"
            >
              {resolveMutation.isPending ? "Resolving…" : RESOLVE_CONFIRM_LABEL}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </PhoneFrame>
  );
}

function renderTimelineRow(row: TimelineRow, tenantFirst: string) {
  if (row.kind === "day-divider") return <DayDivider key={row.key}>{row.label}</DayDivider>;
  if (row.kind === "message")
    return <ThreadMessageRow key={row.key} entry={row.entry} tenantFirst={tenantFirst} />;
  if (row.kind === "audit")
    return <AuditMetaLine key={row.key} label={row.label} at={row.entry.at} />;
  // "draft" rows: a live PENDING draft renders via the pinned footer below,
  // never inline (that would duplicate the one actionable bubble at the
  // foot of the thread). A `stale` draft has no footer — nothing else
  // duplicates it — so it renders here as a muted, non-sendable bubble
  // (issue #256: conversation-model.md's stale-draft rule, "kept in the
  // audit trail, never shown as sendable again").
  if (row.kind === "draft" && row.entry.status === "stale") {
    return <StaleDraftBubble key={row.key} className="ml-auto max-w-[83%]" body={row.entry.body} />;
  }
  return null;
}

function ThreadHeader({
  caseDetail,
  tenantFirst,
  showPlaque,
}: {
  caseDetail: CaseDetail | undefined;
  tenantFirst: string;
  showPlaque: boolean;
}) {
  // H1 (safety review, #234 PR 3 fix round): a `null` severity must not
  // render as "no plaque" (which reads as "nothing to flag here") when
  // the timeline actually carries an `emergency_triggered` audit row —
  // see src/features/cases/emergencySignal.ts's full writeup.
  //
  // #256 comment item 1 (safety re-verify): the ORIGINAL version of this
  // let a real, non-null `severity` always win over the Tier-0 signal —
  // correct for the ordinary "null until classified" case, but wrong for
  // the clamp-failure case emergencySignal.ts's own writeup documents: a
  // Tier-0 trigger fired AND the backend's never-de-escalate clamp still
  // somehow wrote a severity below "emergency". That combination has to
  // read as an active emergency HERE too — the banner right below this
  // header already does (`showEmergencyBanner` uses the same
  // `isEmergencySignal`, unconditionally), so the plaque one line above it
  // must never show a calmer word ("Routine") directly over an emergency
  // banner. `isEmergencySignal` wins outright now; the written severity is
  // only consulted once that signal is false.
  const plaqueSeverity =
    caseDetail && isEmergencySignal(caseDetail) ? "emergency" : (caseDetail?.severity ?? null);

  return (
    <header className="border-b border-clarity-line px-5 pb-3.5 pt-4">
      <div className="flex items-center justify-between gap-2">
        <Link
          to="/app/conversations"
          className="inline-flex min-h-11 items-center gap-1 py-1 font-clarity-sans text-[13px] font-bold text-clarity-ink-dim hover:text-clarity-ink"
        >
          <ChevronLeft className="size-[15px]" aria-hidden="true" />
          Conversations
        </Link>
        {plaqueSeverity && showPlaque && <SeverityPlaque severity={plaqueSeverity} />}
      </div>
      <h1 className="mt-2 font-clarity-serif text-[19px] font-semibold leading-[1.3] tracking-tight text-clarity-ink">
        {caseDetail
          ? `${tenantFirst}${caseDetail.tenant.unit ? `, Unit ${caseDetail.tenant.unit}` : ""}`
          : "Conversation"}
      </h1>
      <p className="mt-0.5 font-clarity-sans text-[12.5px] text-clarity-ink-dim">
        {caseDetail ? `${caseDetail.property.address_line1} · ` : ""}every message, saved with dates
        and times
      </p>
    </header>
  );
}

/**
 * The single pending draft at the foot of the thread — Stoop's dashed
 * "I'd like to reply" bubble, the always-visible margin note, and the
 * Edit / Skip / Approve & send row (the same decision Home's
 * `DecisionCard` renders, now the last thing in the full history rather
 * than its own card).
 */
function DraftFooter({
  tenantFirst,
  draftBody,
  draftEntry,
  why,
  staleNotice,
  isBusy,
  mutationBusy,
  editButtonRef,
  undoButtonRef,
  onApprove,
  onEdit,
  onSkip,
  onUndo,
}: {
  tenantFirst: string;
  draftBody: string;
  draftEntry: ReturnType<typeof entryFor>;
  why: string;
  staleNotice?: string;
  /** Gates Edit and Approve, may be OR'd with `isSendUnverified` by the
   *  caller. See ConversationPage's own `isBusy` comment. */
  isBusy: boolean;
  /** BLOCKER 2 / item 7 (safety review, #291/#279): gates Skip and Undo
   *  ONLY, `isBusy(draftId)` alone, never OR'd with `isSendUnverified`.
   *  See ConversationPage's own `mutationBusy` comment. */
  mutationBusy: boolean;
  /** #191 item 1: see ConversationPage's own `editButtonRef` comment. */
  editButtonRef?: Ref<HTMLButtonElement>;
  /** #191 F2/F4: see ConversationPage's own `undoButtonRef` comment. */
  undoButtonRef?: Ref<HTMLButtonElement>;
  onApprove: () => void;
  onEdit: () => void;
  onSkip: () => void;
  onUndo: () => void;
}) {
  const isSending = draftEntry.status === "sending";
  const isSent = draftEntry.status === "sent";
  const isSkipped = draftEntry.status === "skipped";
  const secondsLeft =
    draftEntry.status === "sending" ? secondsRemaining(draftEntry.undoExpiresAtClient) : 0;
  const totalSeconds = draftEntry.status === "sending" ? totalUndoSeconds(draftEntry) : 5;

  if (isSkipped) {
    return (
      <p className="rounded-clarity-lg border border-dashed border-clarity-line-strong bg-clarity-bg px-[18px] py-3.5 text-center font-clarity-sans text-[13px] text-clarity-ink-dim">
        No reply sent. Case still open
      </p>
    );
  }

  return (
    <div>
      <DraftBubble
        className="ml-auto max-w-[83%]"
        label={isSending ? `On its way to ${tenantFirst}` : "I'd like to reply"}
        body={draftBody}
      />
      {staleNotice && (
        <p className="mt-2.5 text-right font-clarity-sans text-[13px] font-semibold text-clarity-brand">
          {staleNotice}
        </p>
      )}
      {isSending ? (
        <UndoTicket
          secondsLeft={secondsLeft}
          totalSeconds={totalSeconds}
          onUndo={onUndo}
          undoDisabled={mutationBusy}
          undoButtonRef={undoButtonRef}
        />
      ) : isSent ? (
        <p className="mt-3.5 text-right font-clarity-sans text-[13px] font-semibold text-clarity-whenever">
          Sent.
        </p>
      ) : (
        <>
          <MarginNote>{why}</MarginNote>
          <DecisionActions
            onEdit={onEdit}
            onSkip={onSkip}
            onApprove={onApprove}
            disabled={isBusy}
            skipDisabled={mutationBusy}
            editButtonRef={editButtonRef}
          />
        </>
      )}
    </div>
  );
}
