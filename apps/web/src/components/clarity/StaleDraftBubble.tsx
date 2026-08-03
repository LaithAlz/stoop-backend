import { cn } from "@/lib/utils";

interface StaleDraftBubbleProps {
  body: string;
  className?: string;
}

/**
 * A superseded draft, rendered inline in the case-detail timeline (issue
 * #256): conversation-model.md's stale-draft rule says a replaced draft is
 * "kept in the audit trail, never shown as sendable again" — that phrasing
 * implies visible-but-not-sendable, not suppressed outright. Before this
 * fix, src/routes/app.conversations.$id.tsx rendered `null` for every
 * `kind: "draft"` timeline row regardless of status, so a `stale` draft's
 * actual wording was never shown anywhere — only the generic
 * `draft_stale` audit note ("That draft was replaced after a new message
 * came in.", src/features/cases/auditLabels.ts) that sits next to it.
 *
 * Same right-aligned bubble shape as `DraftBubble` (this WAS an "I'd like
 * to reply" proposal, once) but muted like `SkippedCard`'s no-longer-
 * actionable language — dashed muted-line border, dimmed ink, no brand
 * color — and, critically, no action row: there is nothing left to
 * approve/edit/skip here, the graph already re-ran and drafted a
 * replacement. Web only; apps/mobile's identical timeline/render code has
 * the same gap and needs the same fix in its own PR.
 */
export function StaleDraftBubble({ body, className }: StaleDraftBubbleProps) {
  return (
    <div
      className={cn(
        "rounded-clarity-lg rounded-tr-clarity-sm border border-dashed border-clarity-line-strong bg-clarity-bg px-[15px] py-[13px] font-clarity-serif text-[15.5px] italic leading-relaxed text-clarity-ink-dim opacity-75",
        className,
      )}
    >
      <span className="mb-1.5 block font-clarity-sans text-[11px] font-bold not-italic uppercase tracking-[0.02em] text-clarity-ink-dim">
        Replaced
      </span>
      {body}
    </div>
  );
}
