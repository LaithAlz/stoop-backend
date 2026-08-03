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
  severity: Severity;
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
  /** The `draft_stale` one-line notice (src/features/queue/
   *  queueEntries.ts's `draftStaleNotice`) — shown for a few seconds after
   *  a concurrent tenant reply invalidates this card's draft mid-action. */
  staleNotice?: string;
  /** True while the edit-and-send mutation for THIS card is in flight. */
  editSubmitting?: boolean;
  /** A2 (safety review, #234 PR 2): true while ANY mutation for this
   *  card's draft is in flight — disables the Edit/Skip/Approve row and
   *  the Undo tap so two actions can't race on the same draft. */
  actionsBusy?: boolean;
  onApprove?: () => void;
  onEdit?: () => void;
  onSkip?: () => void;
  onUndo?: () => void;
  onCancelEdit?: () => void;
  onSubmitEdit?: (body: string) => void;
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
  actionsBusy = false,
  onApprove,
  onEdit,
  onSkip,
  onUndo,
  onCancelEdit,
  onSubmitEdit,
  className,
}: DecisionCardProps) {
  const isSending = status === "sending";
  const isSent = status === "sent";
  const isEditing = status === "editing";
  const isPending = status === "pending";

  return (
    <article
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
            className="inline-flex min-h-8 items-center gap-1 py-1 font-clarity-sans text-xs font-bold text-clarity-ink-dim hover:text-clarity-brand"
          >
            Full view
            <ChevronRight className="size-3" aria-hidden="true" />
          </Link>
        )}
      </div>

      <p className="mb-3 font-clarity-sans text-[12.5px] font-semibold leading-relaxed text-clarity-ink-dim">
        <b className="font-bold text-clarity-ink">{tenantName}</b> — {propertyLabel}{" "}
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
          undoDisabled={actionsBusy}
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
          />
        </>
      )}
    </article>
  );
}
