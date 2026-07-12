import { Check, Pencil, SkipForward } from "lucide-react";

interface DecisionActionsProps {
  onEdit?: () => void;
  onSkip?: () => void;
  onApprove?: () => void;
  className?: string;
}

/**
 * The one-primary-action row under a drafted reply — Edit / Skip /
 * Approve & send (docs/mockups/07-clarity-redesign.html `.actions`).
 * Shared by `DecisionCard` (the queue) and the conversation thread route
 * so the same decision always looks and behaves the same way wherever
 * it's approved from.
 */
export function DecisionActions({ onEdit, onSkip, onApprove, className }: DecisionActionsProps) {
  return (
    <div className={className ? `mt-[15px] flex gap-2.5 ${className}` : "mt-[15px] flex gap-2.5"}>
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
  );
}
