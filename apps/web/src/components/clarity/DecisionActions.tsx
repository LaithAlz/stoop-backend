import type { Ref } from "react";
import { Check, Pencil, SkipForward } from "lucide-react";

interface DecisionActionsProps {
  onEdit?: () => void;
  onSkip?: () => void;
  onApprove?: () => void;
  /** A2 (safety review, #234 PR 2): true while a mutation for THIS card's
   *  draft is in flight — disables all three controls at once so an
   *  approve tap can't race a skip tap on the same card. Scoped per draft
   *  by the caller (useDraftActions' `isBusy`), never a global flag. */
  disabled?: boolean;
  /** #191 item 1: lets the owner (DecisionCard / the conversation thread's
   *  DraftFooter) hold a live reference to THIS Edit button so it can
   *  return keyboard focus here once the editor it opens is closed —
   *  the button unmounts and remounts across that round trip, so a plain
   *  `useRef` captured once by the caller would go stale. */
  editButtonRef?: Ref<HTMLButtonElement>;
  className?: string;
}

/**
 * The one-primary-action row under a drafted reply — Edit / Skip /
 * Approve & send (docs/mockups/07-clarity-redesign.html `.actions`).
 * Shared by `DecisionCard` (the queue) and the conversation thread route
 * so the same decision always looks and behaves the same way wherever
 * it's approved from.
 */
export function DecisionActions({
  onEdit,
  onSkip,
  onApprove,
  disabled = false,
  editButtonRef,
  className,
}: DecisionActionsProps) {
  return (
    <div className={className ? `mt-[15px] flex gap-2.5 ${className}` : "mt-[15px] flex gap-2.5"}>
      <button
        ref={editButtonRef}
        type="button"
        onClick={onEdit}
        disabled={disabled}
        className="inline-flex min-h-12 items-center gap-1.5 rounded-clarity-md border-[1.5px] border-clarity-line-strong bg-clarity-panel px-4 font-clarity-sans text-[15px] font-extrabold text-clarity-ink-dim transition-transform duration-150 ease-clarity hover:-translate-y-px disabled:translate-y-0 disabled:opacity-60 motion-reduce:transition-none motion-reduce:hover:translate-y-0"
      >
        <Pencil className="size-4" aria-hidden="true" />
        Edit
      </button>
      <button
        type="button"
        onClick={onSkip}
        disabled={disabled}
        className="inline-flex min-h-12 items-center gap-1.5 rounded-clarity-md border-[1.5px] border-clarity-line-strong bg-clarity-panel px-4 font-clarity-sans text-[15px] font-extrabold text-clarity-ink-dim transition-transform duration-150 ease-clarity hover:-translate-y-px disabled:translate-y-0 disabled:opacity-60 motion-reduce:transition-none motion-reduce:hover:translate-y-0"
      >
        <SkipForward className="size-4" aria-hidden="true" />
        Skip
      </button>
      <button
        type="button"
        onClick={onApprove}
        disabled={disabled}
        aria-busy={disabled}
        className="flex min-h-[52px] flex-1 items-center justify-center gap-2 rounded-clarity-md border-[1.5px] border-clarity-brand-deep bg-clarity-brand font-clarity-sans text-base font-extrabold text-clarity-brand-on shadow-clarity-banner transition-transform duration-150 ease-clarity hover:-translate-y-px disabled:translate-y-0 disabled:opacity-60 motion-reduce:transition-none motion-reduce:hover:translate-y-0"
      >
        <Check className="size-4" aria-hidden="true" />
        Approve &amp; send
      </button>
    </div>
  );
}
