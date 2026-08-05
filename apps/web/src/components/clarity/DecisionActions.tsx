import type { Ref } from "react";
import { Check, Pencil, SkipForward } from "lucide-react";

interface DecisionActionsProps {
  onEdit?: () => void;
  onSkip?: () => void;
  onApprove?: () => void;
  /** A2 (safety review, #234 PR 2): true while a mutation for THIS card's
   *  draft is in flight, disabling Edit and Approve so an approve tap
   *  can't race an edit-and-send on the same card. Scoped per draft by
   *  the caller (useDraftActions' `isBusy`), never a global flag.
   *
   *  BLOCKER 2 (safety review, #291/#279): the caller may also fold in
   *  `isSendUnverified` here (an ambiguous edit-and-send this draft is
   *  still waiting to hear back about), correct for Edit and Approve,
   *  both of which would otherwise risk silently resending a draft whose
   *  true state is unknown. Skip has its own `skipDisabled` below
   *  specifically so it is NEVER gated by that flag. */
  disabled?: boolean;
  /** BLOCKER 2 (safety review, #291/#279): Skip's own disabled state,
   *  separate from `disabled` above. Skip provably sends nothing to the
   *  tenant, so it must stay reachable even while `isSendUnverified` has
   *  Edit and Approve locked (otherwise a landlord whose queue endpoint
   *  is also down, BLOCKER 2's own failure mode, has no in-app escape
   *  from this card at all until the flag eventually clears on its own).
   *  Defaults to `disabled` when the caller doesn't pass one explicitly,
   *  which is only correct as long as every real caller DOES pass one:
   *  both do (DecisionCard.tsx, app.conversations.$id.tsx's DraftFooter). */
  skipDisabled?: boolean;
  /** #191 item 1: lets the owner (DecisionCard / the conversation thread's
   *  DraftFooter) hold a live reference to THIS Edit button so it can
   *  return keyboard focus here once the editor it opens is closed. The
   *  button unmounts and remounts across that round trip, so a plain
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
  skipDisabled = disabled,
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
        disabled={skipDisabled}
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
