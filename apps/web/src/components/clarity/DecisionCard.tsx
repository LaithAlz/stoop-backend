import { useEffect, useRef, type Ref } from "react";
import { Link } from "@tanstack/react-router";
import { ChevronRight, Image as ImageIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { Severity } from "@/components/stoop/SeverityBadge";
import { SeverityPlaque } from "./SeverityPlaque";
import { TimestampChip } from "./TimestampChip";
import { MarginNote } from "./MarginNote";
import { UndoTicket } from "./UndoTicket";
import { DraftBubble } from "./DraftBubble";
import { DecisionActions } from "./DecisionActions";
import { EditDraftPanel } from "./EditDraftPanel";

/** Fallback when `QueueItem.why` is null — rows classified before the
 *  `summary` audit key shipped (api-contracts.md's Queue v1.1 amendment).
 *  This is this file's existing Clarity wording (docs/mockups/07),
 *  unchanged by the live-data port — apps/mobile's own DecisionCard uses
 *  a DIFFERENT fallback sentence ("I sorted this the best I could — open
 *  the full view for the details."); the two platforms have quietly
 *  drifted on this one string. Flagged for copy-guardian in the PR
 *  report, not resolved here. */
const DEFAULT_WHY = "I drafted this from your house rules and past replies.";

type DecisionCardStatus = "pending" | "sending" | "sent" | "editing";

interface DecisionCardProps {
  severity: Severity | null;
  tenantName: string;
  propertyLabel: string;
  timestamp: string;
  tenantMessage: string;
  photoNote?: string;
  draftMessage: string;
  /** Null falls back to `DEFAULT_WHY` above — see that constant's comment. */
  why: string | null;
  whyLinkHref?: string;
  whyLinkLabel?: string;
  /** Renders the "Full view" link in the card head when set. */
  conversationId?: string;
  /** "sending" swaps the actions row for the undo ticket; "sent" shows a
   *  brief confirmation note; "editing" swaps the draft bubble for an
   *  inline editor. */
  status?: DecisionCardStatus;
  secondsLeft?: number;
  totalSeconds?: number;
  /** The card's one-line notice slot, the caller (src/routes/
   *  app.index.tsx's `QueueRow`) merges three sources into this ONE
   *  string in priority order: the `draft_stale` notice
   *  (src/features/queue/queueEntries.ts's `draftStaleNotice`, shown for
   *  a few seconds after a concurrent tenant reply invalidates this
   *  card's draft mid-action), else `UNVERIFIED_SEND_NOTICE` while
   *  `sendUnverified` is true, else the give-up ceiling's sticky notice
   *  once it isn't. Rendered below the draft bubble while `!isEditing`,
   *  AND (round 4, BLOCKER 2) threaded straight into `EditDraftPanel`'s
   *  own `notice` prop while editing, see that branch below. */
  staleNotice?: string;
  /** True while the edit-and-send mutation for THIS card is in flight. */
  editSubmitting?: boolean;
  /** R3-1 (safety review round 3 follow-up, issue #252): true while THIS
   *  card's last edit-and-send ended in an ambiguous failure and hasn't
   *  been resolved against a fresh queue read yet — see EditDraftPanel's
   *  own comment on `sendDisabled`. */
  sendUnverified?: boolean;
  /** A2 (safety review, #234 PR 2): true while ANY mutation for this
   *  card's draft is in flight, OR'd with `sendUnverified` by the caller.
   *  Disables the Edit/Approve pair so neither can race a mutation or
   *  silently resend a draft whose last edit-and-send is still
   *  unconfirmed. Skip and Undo do NOT use this, see `mutationBusy`
   *  below (BLOCKER 2, safety review #291/#279). */
  actionsBusy?: boolean;
  /** BLOCKER 2 / item 7 (safety review, #291/#279): true while a mutation
   *  for THIS draft is in flight, `isBusy(draftId)` alone, deliberately
   *  NEVER OR'd with `sendUnverified`. Gates Skip (it provably sends
   *  nothing to the tenant, so it must stay reachable even while an
   *  unrelated edit-and-send is unconfirmed: the escape hatch a locked
   *  card would otherwise have none of) and Undo (the ambiguity an
   *  unconfirmed edit-and-send raises is never about THIS undo call; an
   *  Undo tap already can't reach a draft that's mid-edit-and-send, since
   *  those two states are mutually exclusive on one draft id). */
  mutationBusy?: boolean;
  onApprove?: () => void;
  onEdit?: () => void;
  onSkip?: () => void;
  onUndo?: () => void;
  onCancelEdit?: () => void;
  onSubmitEdit?: (body: string) => void;
  /** #191 F2/F4 (safety review follow-up): forwarded straight through to
   *  `UndoTicket`'s own Undo button so the row above can focus it the
   *  moment this card starts sending. See that prop's own comment. */
  undoButtonRef?: Ref<HTMLButtonElement>;
  className?: string;
}

/**
 * One decision, full stop — tenant's text, Stoop's drafted reply, the
 * plain-English reason why, and exactly one primary action
 * (docs/mockups/07-clarity-redesign.html `.entry`). Not a table row.
 */
export function DecisionCard({
  severity,
  tenantName,
  propertyLabel,
  timestamp,
  tenantMessage,
  photoNote,
  draftMessage,
  why,
  whyLinkHref,
  whyLinkLabel,
  conversationId,
  status = "pending",
  secondsLeft = 5,
  totalSeconds = 5,
  staleNotice,
  editSubmitting = false,
  sendUnverified = false,
  actionsBusy = false,
  mutationBusy = actionsBusy,
  onApprove,
  onEdit,
  onSkip,
  onUndo,
  onCancelEdit,
  onSubmitEdit,
  undoButtonRef,
  className,
}: DecisionCardProps) {
  const isSending = status === "sending";
  const isSent = status === "sent";
  const isEditing = status === "editing";
  const isPending = status === "pending";

  // #191 item 1: the Edit button (rendered by DecisionActions below)
  // unmounts the instant edit mode opens and only remounts if the
  // landlord cancels back to "pending". It never remounts on a
  // successful edit-and-send, which moves straight to "sending".
  // `editButtonRef` always points at whichever Edit button is currently
  // mounted, or `null` if none is, so the effect below can tell reachable
  // from not.
  const editButtonRef = useRef<HTMLButtonElement>(null);
  const cardRef = useRef<HTMLElement>(null);
  const wasEditingRef = useRef(isEditing);

  useEffect(() => {
    // #191 F2/F4 (safety review re-verify): this used to fire on ANY
    // isEditing true -> false edge, including a successful edit-and-send
    // (which moves straight to "sending", never back to "pending"). That
    // raced the row-level effect below the queue that now focuses the
    // Undo button on that same transition: this one (a child, so it
    // commits its effects first) would focus the card, and the row's
    // effect would immediately re-focus the Undo button, firing focus
    // twice in one commit for a single user action. Scoping this to
    // `isPending` narrows it to the ONE case it's actually for: Cancel,
    // which is the only close path that lands back on "pending".
    if (wasEditingRef.current && !isEditing && isPending) {
      // F6 (safety review, #191 follow-up): only move focus if it was
      // plausibly here. Either it's still literally inside the card, or
      // it was reset to <body> because the element that had it (the
      // editor's textarea or Cancel) was just removed as part of THIS
      // transition. Without this guard, a background settle that has
      // nothing to do with the landlord's own click (R3-1's
      // `resolveUnverifiedSend`, called from a queue poll) could close a
      // DIFFERENT card's editor while the landlord has already tabbed
      // elsewhere, and yank their focus and the page's scroll back up to
      // this one.
      const active = document.activeElement;
      if (cardRef.current?.contains(active) || active === document.body) {
        // Cancel just closed the editor. Land focus on the Edit button
        // if it came back, otherwise the card itself, so a keyboard user
        // is never dropped onto <body>.
        // F8 (re-verify): `.isConnected` alone isn't enough. `.focus()`
        // on a DISABLED button is also a silent no-op that never reaches
        // the fallback below, and Edit can come back disabled: an
        // ambiguous edit-and-send sets `sendUnverified`, `actionsBusy`
        // stays true, the landlord taps Cancel, and Edit remounts
        // connected but disabled, inside the #252 danger window.
        const btn = editButtonRef.current;
        if (btn?.isConnected && !btn.disabled) {
          btn.focus();
        } else {
          cardRef.current?.focus();
        }
      }
    }
    wasEditingRef.current = isEditing;
  }, [isEditing, isPending]);

  return (
    <article
      ref={cardRef}
      tabIndex={-1}
      aria-label={`${tenantName}, ${propertyLabel}`}
      className={cn(
        "rounded-clarity-lg border border-clarity-line-strong bg-clarity-surface p-[18px] shadow-clarity-card",
        className,
      )}
    >
      <div className="mb-2.5 flex items-center justify-between gap-2.5">
        <SeverityPlaque severity={severity} />
        {conversationId && (
          <Link
            to="/app/conversations/$id"
            params={{ id: conversationId }}
            className="inline-flex min-h-11 items-center gap-1 py-1 font-clarity-sans text-xs font-bold text-clarity-ink-dim hover:text-clarity-brand"
          >
            Full view
            <ChevronRight className="size-3" aria-hidden="true" />
          </Link>
        )}
      </div>

      <p className="mb-3 font-clarity-sans text-[12.5px] font-semibold leading-relaxed text-clarity-ink-dim">
        <b className="font-bold text-clarity-ink">{tenantName}</b>, {propertyLabel}{" "}
        <TimestampChip>{timestamp}</TimestampChip>
      </p>

      <div className="rounded-clarity-lg rounded-tl-clarity-sm border border-clarity-line-strong bg-clarity-panel px-[15px] py-[13px] font-clarity-sans text-[15.5px] leading-relaxed text-clarity-ink">
        <span className="mb-1.5 block font-clarity-sans text-[11px] font-bold uppercase tracking-[0.02em] text-clarity-ink-dim">
          {tenantName} said
        </span>
        {tenantMessage}
        {photoNote && (
          <span className="mt-2.5 inline-flex items-center gap-2 rounded-clarity-sm border border-clarity-line-strong bg-clarity-panel py-[5px] pl-[5px] pr-2.5 font-clarity-sans text-xs font-semibold text-clarity-ink-dim">
            <span className="flex size-[30px] shrink-0 items-center justify-center rounded-[6px] bg-clarity-line text-clarity-ink-dim">
              <ImageIcon className="size-4" aria-hidden="true" />
            </span>
            {photoNote}
          </span>
        )}
      </div>

      {isEditing ? (
        <EditDraftPanel
          className="mt-2"
          tenantName={tenantName}
          initialBody={draftMessage}
          submitting={editSubmitting}
          sendDisabled={sendUnverified}
          // BLOCKER 2 (safety review round 4, #291/#279): the same
          // `staleNotice` this card would otherwise show below the draft
          // bubble (the block right under this ternary, gated on
          // `!isEditing`), passed straight through so the give-up
          // ceiling's sticky notice keeps showing once the landlord opens
          // Edit, instead of disappearing at exactly the moment they can
          // act on it.
          notice={staleNotice}
          onCancel={() => onCancelEdit?.()}
          onSend={(body) => onSubmitEdit?.(body)}
        />
      ) : (
        <DraftBubble
          className="mt-2"
          label={isSending ? `On its way to ${tenantName}` : "I'd like to reply"}
          body={draftMessage}
        />
      )}

      {staleNotice && !isEditing && (
        <p className="mt-2.5 font-clarity-sans text-[13px] font-semibold text-clarity-brand">
          {staleNotice}
        </p>
      )}

      {isSending && (
        <UndoTicket
          secondsLeft={secondsLeft}
          totalSeconds={totalSeconds}
          onUndo={onUndo}
          undoDisabled={mutationBusy}
          undoButtonRef={undoButtonRef}
        />
      )}
      {isSent && (
        <p className="mt-3.5 font-clarity-sans text-[13px] font-semibold text-clarity-whenever">
          Sent.
        </p>
      )}
      {isPending && (
        <>
          <MarginNote linkHref={whyLinkHref} linkLabel={whyLinkLabel}>
            {why ?? DEFAULT_WHY}
          </MarginNote>
          <DecisionActions
            onEdit={onEdit}
            onSkip={onSkip}
            onApprove={onApprove}
            disabled={actionsBusy}
            skipDisabled={mutationBusy}
            editButtonRef={editButtonRef}
          />
        </>
      )}
    </article>
  );
}
