import { useEffect, useId, useRef, useState } from "react";
import { Check } from "lucide-react";
import { cn } from "@/lib/utils";

/** Shown wherever an ambiguous edit-and-send has blocked a send control —
 *  inside this editor, and on the card itself once the landlord cancels
 *  out of the editor (F7, src/routes/app.index.tsx). One string, so the
 *  two surfaces can't drift. */
export const UNVERIFIED_SEND_NOTICE =
  "Checking whether your last reply went out. You'll be able to send again in a moment.";

/** BLOCKER 2's wall-clock ceiling (safety review, #291/#279,
 *  useResolveUnverifiedSends.ts's `UNVERIFIED_CEILING_MS`): the one-time
 *  toast when `UNVERIFIED_SEND_NOTICE` above has been showing for two
 *  minutes with no server read ever confirming which way it went, and
 *  the guard releases anyway rather than holding the landlord silent
 *  forever. States plainly what did and didn't happen: the check never
 *  came back either way (not "it failed", which would overclaim a
 *  negative result this client doesn't have), and what to do about it:
 *  the conversation, not this toast, is the source of truth for what
 *  actually sent. */
export const UNVERIFIED_GIVE_UP_NOTICE =
  "Couldn't confirm whether your last edit sent. Open the conversation to check, then try again if it didn't.";

interface EditDraftPanelProps {
  tenantName: string;
  initialBody: string;
  submitting?: boolean;
  /** R3-1 (safety review round 3 follow-up, issue #252): true while THIS
   *  draft's last edit-and-send ended in an ambiguous failure (network
   *  drop / 5xx) that hasn't been resolved against a fresh server read
   *  yet — disables Send ONLY (not Cancel, not the textarea) so a
   *  retype-and-resend can't silently overwrite an already-delivered body
   *  while its fate is still unknown. */
  sendDisabled?: boolean;
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
  sendDisabled = false,
  onCancel,
  onSend,
  className,
}: EditDraftPanelProps) {
  const [body, setBody] = useState(initialBody);
  const fieldId = useId();
  const canSend = body.trim().length > 0 && !submitting && !sendDisabled;
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  // #191 item 1: this panel is only ever mounted while edit mode is open
  // (both call sites swap it in via a ternary rather than toggling its
  // visibility), so "on mount" IS "on open"; no separate open/closed prop
  // is needed. This runs client-side only (useEffect never fires during
  // SSR), so it can't fight hydration: there is nothing to focus until
  // the browser has already committed this DOM node.
  useEffect(() => {
    textareaRef.current?.focus();
  }, []);

  return (
    <div className={cn(className)}>
      <label htmlFor={fieldId} className="sr-only">
        Edit your reply to {tenantName}
      </label>
      <textarea
        id={fieldId}
        ref={textareaRef}
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
          aria-describedby={sendDisabled ? `${fieldId}-unverified` : undefined}
          className="flex min-h-[52px] items-center justify-center gap-2 rounded-clarity-md border-[1.5px] border-clarity-brand-deep bg-clarity-brand font-clarity-sans text-base font-extrabold text-clarity-brand-on shadow-clarity-banner transition-transform duration-150 ease-clarity hover:-translate-y-px motion-reduce:transition-none motion-reduce:hover:translate-y-0 disabled:opacity-60"
        >
          <Check className="size-4" aria-hidden="true" />
          {submitting ? "Sending…" : "Send edited version"}
        </button>
      </div>
      {/* F6 (safety re-verify, #252): a dimmed button with no explanation
          is not enough for a blocked SAFETY control — once the guard
          resolves only against a genuinely newer read, Send can stay dead
          for a whole poll interval (or longer while the API is down,
          which is the safe direction but only if it's understandable).
          The toast that raised it is gone in four seconds; this isn't. */}
      {sendDisabled && !submitting && (
        <p
          id={`${fieldId}-unverified`}
          role="status"
          className="mt-2.5 font-clarity-sans text-[13px] font-semibold text-clarity-ink-dim"
        >
          {UNVERIFIED_SEND_NOTICE}
        </p>
      )}
    </div>
  );
}
