import { Link } from "@tanstack/react-router";
import { Check, ChevronRight, Image as ImageIcon, Pencil, SkipForward } from "lucide-react";
import { cn } from "@/lib/utils";
import type { Severity } from "@/components/stoop/SeverityBadge";
import { SeverityPlaque } from "./SeverityPlaque";
import { TimestampChip } from "./TimestampChip";
import { MarginNote } from "./MarginNote";
import { UndoTicket } from "./UndoTicket";

interface DecisionCardProps {
  severity: Severity;
  tenantName: string;
  propertyLabel: string;
  timestamp: string;
  tenantMessage: string;
  photoNote?: string;
  draftMessage: string;
  why: string;
  whyLinkHref?: string;
  whyLinkLabel?: string;
  /** Renders the "Full view" link in the card head when set. */
  conversationId?: string;
  /** "sending" swaps the actions row for the undo ticket. */
  status?: "pending" | "sending";
  secondsLeft?: number;
  totalSeconds?: number;
  onApprove?: () => void;
  onEdit?: () => void;
  onSkip?: () => void;
  onUndo?: () => void;
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
  onApprove,
  onEdit,
  onSkip,
  onUndo,
  className,
}: DecisionCardProps) {
  const isSending = status === "sending";

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

      <div className="mt-2 rounded-clarity-lg rounded-tr-clarity-sm border-[1.5px] border-dashed border-clarity-brand-border bg-clarity-brand-soft px-[15px] py-[13px] font-clarity-serif text-[15.5px] italic leading-relaxed text-clarity-ink">
        <span className="mb-1.5 block font-clarity-sans text-[11px] font-bold not-italic uppercase tracking-[0.02em] text-clarity-brand">
          {isSending ? `On its way to ${tenantName}` : "I'd like to reply"}
        </span>
        {draftMessage}
      </div>

      {isSending ? (
        <UndoTicket secondsLeft={secondsLeft} totalSeconds={totalSeconds} onUndo={onUndo} />
      ) : (
        <>
          <MarginNote linkHref={whyLinkHref} linkLabel={whyLinkLabel}>
            {why}
          </MarginNote>
          <div className="mt-[15px] flex gap-2.5">
            <button
              type="button"
              onClick={onEdit}
              className="inline-flex min-h-12 items-center gap-1.5 rounded-clarity-md border-[1.5px] border-clarity-line-strong bg-clarity-panel px-4 font-clarity-sans text-[15px] font-extrabold text-clarity-ink-dim transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0"
            >
              <Pencil className="size-4" aria-hidden="true" />
              Edit
            </button>
            <button
              type="button"
              onClick={onSkip}
              className="inline-flex min-h-12 items-center gap-1.5 rounded-clarity-md border-[1.5px] border-clarity-line-strong bg-clarity-panel px-4 font-clarity-sans text-[15px] font-extrabold text-clarity-ink-dim transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0"
            >
              <SkipForward className="size-4" aria-hidden="true" />
              Skip
            </button>
            <button
              type="button"
              onClick={onApprove}
              className="flex min-h-[52px] flex-1 items-center justify-center gap-2 rounded-clarity-md border-[1.5px] border-clarity-brand-deep bg-clarity-brand font-clarity-sans text-base font-extrabold text-clarity-brand-on shadow-clarity-banner transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0"
            >
              <Check className="size-4" aria-hidden="true" />
              Approve &amp; send
            </button>
          </div>
        </>
      )}
    </article>
  );
}
