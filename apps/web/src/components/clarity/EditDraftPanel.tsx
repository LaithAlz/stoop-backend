import { useId, useState } from "react";
import { Check } from "lucide-react";
import { cn } from "@/lib/utils";

interface EditDraftPanelProps {
  tenantName: string;
  initialBody: string;
  submitting?: boolean;
  onCancel: () => void;
  onSend: (body: string) => void;
  className?: string;
}

/**
 * The edit-and-send inline editor for a queue card. Reuses the exact
 * textarea + Cancel / "Send edited version" pattern already shipped in
 * the conversation thread's `DraftReply` (src/routes/
 * app.conversations.$id.tsx's "editing" mode) rather than a new design —
 * same Clarity dashed-brand-border textarea, same two-button row. Skips
 * that screen's "See original" toggle for now (the draft being edited is
 * still visible above, in the card's own tenant-message context) to keep
 * this port minimal; a later PR can share this component with that route
 * if it's worth de-duplicating.
 *
 * `POST /v1/drafts/{id}/edit-and-send` rejects an empty or
 * whitespace-only body (api-contracts.md "Drafts"), so Send stays
 * disabled until there's real text.
 */
export function EditDraftPanel({
  tenantName,
  initialBody,
  submitting = false,
  onCancel,
  onSend,
  className,
}: EditDraftPanelProps) {
  const [body, setBody] = useState(initialBody);
  const fieldId = useId();
  const canSend = body.trim().length > 0 && !submitting;

  return (
    <div className={cn(className)}>
      <label htmlFor={fieldId} className="sr-only">
        Edit your reply to {tenantName}
      </label>
      <textarea
        id={fieldId}
        value={body}
        onChange={(e) => setBody(e.target.value)}
        disabled={submitting}
        rows={4}
        className="min-h-32 w-full rounded-clarity-lg rounded-tr-clarity-sm border-[1.5px] border-clarity-brand-border bg-clarity-brand-soft p-4 font-clarity-serif text-[15.5px] italic leading-relaxed text-clarity-ink disabled:opacity-70"
      />
      <div className="mt-[15px] grid grid-cols-[auto_1fr] gap-2.5">
        <button
          type="button"
          onClick={onCancel}
          disabled={submitting}
          className="inline-flex min-h-[52px] items-center justify-center rounded-clarity-md border-[1.5px] border-clarity-line-strong bg-clarity-panel px-4 font-clarity-sans text-[15px] font-extrabold text-clarity-ink-dim transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0 disabled:opacity-60"
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={() => onSend(body.trim())}
          disabled={!canSend}
          className="flex min-h-[52px] items-center justify-center gap-2 rounded-clarity-md border-[1.5px] border-clarity-brand-deep bg-clarity-brand font-clarity-sans text-base font-extrabold text-clarity-brand-on shadow-clarity-banner transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0 disabled:opacity-60"
        >
          <Check className="size-4" aria-hidden="true" />
          {submitting ? "Sending…" : "Send edited version"}
        </button>
      </div>
    </div>
  );
}
