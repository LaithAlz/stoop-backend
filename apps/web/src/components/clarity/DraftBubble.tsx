import { cn } from "@/lib/utils";

interface DraftBubbleProps {
  /** "I'd like to reply" (pending) or "On its way to {tenant}" (sending) —
   * the only two labels the mockup uses for this bubble. */
  label: string;
  body: string;
  className?: string;
}

/**
 * Stoop's own drafted reply, not yet delivered — dashed brand border,
 * serif italic (docs/mockups/07-clarity-redesign.html `.bubble-out` /
 * `.thread-bubble-pending`, the same material in both the queue card and
 * the full conversation view). Shared by `DecisionCard` and the
 * conversation thread route rather than duplicated between them.
 */
export function DraftBubble({ label, body, className }: DraftBubbleProps) {
  return (
    <div
      className={cn(
        "rounded-clarity-lg rounded-tr-clarity-sm border-[1.5px] border-dashed border-clarity-brand-border bg-clarity-brand-soft px-[15px] py-[13px] font-clarity-serif text-[15.5px] italic leading-relaxed text-clarity-ink",
        className,
      )}
    >
      <span className="mb-1.5 block font-clarity-sans text-[11px] font-bold not-italic uppercase tracking-[0.02em] text-clarity-brand">
        {label}
      </span>
      {body}
    </div>
  );
}
