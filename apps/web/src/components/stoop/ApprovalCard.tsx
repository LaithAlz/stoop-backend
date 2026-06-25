import { Check, Pencil, ChevronRight } from "lucide-react";
import { Button } from "@/components/ui/button";
import { SeverityBadge, type Severity } from "./SeverityBadge";
import { MessageBubble } from "./MessageBubble";

interface ApprovalCardProps {
  unit: string;
  property: string;
  receivedAgo: string;
  severity: Severity;
  tenantMessage: string;
  draftReply: string;
  onApprove?: () => void;
  onEdit?: () => void;
}

export function ApprovalCard({
  unit,
  property,
  receivedAgo,
  severity,
  tenantMessage,
  draftReply,
  onApprove,
  onEdit,
}: ApprovalCardProps) {
  return (
    <article className="overflow-hidden rounded-3xl border border-border bg-card shadow-sm">
      <header className="flex items-center justify-between border-b border-border bg-surface/50 px-5 py-4">
        <div>
          <p className="text-sm font-bold text-ink">{unit}</p>
          <p className="text-[11px] font-medium uppercase tracking-wider text-ink-muted">
            {property} • Received {receivedAgo}
          </p>
        </div>
        <SeverityBadge severity={severity} />
      </header>

      <div className="space-y-5 px-5 py-6">
        <MessageBubble variant="tenant" text={tenantMessage} timestamp="10:14 AM" />
        <MessageBubble variant="draft" text={draftReply} timestamp="10:14 AM" />
      </div>

      <footer className="grid grid-cols-2 gap-3 border-t border-border bg-surface/40 p-4">
        <Button
          type="button"
          variant="outline"
          className="h-14 min-h-11 text-base font-semibold"
          onClick={onEdit}
        >
          <Pencil className="size-4" aria-hidden="true" />
          Edit draft
        </Button>
        <Button type="button" className="h-14 min-h-11 text-base font-semibold" onClick={onApprove}>
          <Check className="size-4" aria-hidden="true" />
          Approve & send
        </Button>
      </footer>

      <div className="flex items-center justify-center gap-1 border-t border-border bg-canvas px-5 py-2 text-[11px] font-medium text-ink-muted">
        View full conversation
        <ChevronRight className="size-3" aria-hidden="true" />
      </div>
    </article>
  );
}
