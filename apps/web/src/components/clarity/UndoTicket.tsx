import { useId, useRef } from "react";
import { cn } from "@/lib/utils";

interface UndoTicketProps {
  /** Seconds remaining before the send is final and can no longer be undone. */
  secondsLeft: number;
  totalSeconds: number;
  onUndo?: () => void;
  /** A2 (safety review, #234 PR 2): true while this draft's own undo call
   *  is already in flight — a second tap mid-request would just double-fire
   *  the DELETE. */
  undoDisabled?: boolean;
  className?: string;
}

/**
 * The undo control drawn as a physical, perforated ticket strip — not a
 * toast that vanishes (docs/mockups/07 `.ticket`). Nothing else competes
 * with it once a reply is on its way.
 */
export function UndoTicket({
  secondsLeft,
  totalSeconds,
  onUndo,
  undoDisabled = false,
  className,
}: UndoTicketProps) {
  const clamped = Math.max(0, secondsLeft);
  const pct = totalSeconds > 0 ? Math.max(0, Math.min(100, (clamped / totalSeconds) * 100)) : 0;
  const display = `00:${String(clamped).padStart(2, "0")}`;
  const noticeId = useId();

  // #191 F5 (safety review follow-up): `undoDisabled` used to be handed
  // straight to the button's own `disabled` attribute. A browser
  // automatically blurs an element the instant it's disabled, so the tap
  // that STARTS the undo request also rips focus off the one control this
  // whole ticket exists for, exactly when a keyboard/screen-reader user
  // needs it to stay put. `aria-busy` below still announces "in flight"
  // without touching focusability; this ref is what actually stops a
  // second DELETE. It's read synchronously inside the click handler, so
  // it also covers a same-tick double-activation the next `undoDisabled`
  // prop update hasn't reached yet. Every path the undo mutation can
  // resolve through (success, generic failure, `already_sent`) removes
  // this "sending" entry (queueEntries.ts's reducer), which always
  // unmounts THIS component, so there's no later legitimate tap on the
  // same instance that would need `firedRef` reset.
  const firedRef = useRef(false);
  const handleUndo = () => {
    if (undoDisabled || firedRef.current) return;
    firedRef.current = true;
    onUndo?.();
  };

  return (
    <div
      className={cn(
        "clarity-ticket mt-[15px] rounded-clarity-md border border-clarity-line-strong bg-clarity-surface px-4 pb-3.5 pt-4",
        className,
      )}
    >
      {/* #191 F1/F2/F4 (safety review): a per-tick sr-only suffix on the
          button's own name used to carry this. It was noisy (re-announced,
          or not, unpredictably, every second), but at least SOMETHING told
          a screen-reader user arriving at the button that a clock was
          running. Replacing it with a stable "Undo" name and nothing else
          made the control silently urgent instead. This restores that
          context without the tick. It's `role="alert"`, not `status`,
          because this element is INSERTED fresh every time a card starts
          sending, and src/routes/sign-in.tsx's #248 F3 ruling already
          established that a live region announced by insertion is
          unreliable for `status` across assistive tech (higher stakes
          here, since the design DEPENDS on this one firing). It's placed
          first, above the "Sending" kicker, so it's read/announced before
          the button in browse order, and wired to the button via
          `aria-describedby` (the same idiom EditDraftPanel uses for its
          blocked-Send explanation) so the text is ALSO read every time a
          screen-reader user actually lands on Undo, not only once on
          mount. `{totalSeconds}` is the real, server-derived window
          (queueEntries.ts's `totalUndoSeconds`), never a hardcoded "5"
          that could read wrong against an on-screen countdown showing
          something else. */}
      <p id={noticeId} role="alert" className="sr-only">
        Undo is available for {totalSeconds} second{totalSeconds === 1 ? "" : "s"} after you
        approve.
      </p>
      <div className="flex items-center gap-3">
        <div className="min-w-0 flex-1">
          <span className="mb-[5px] block font-clarity-sans text-[10px] font-bold uppercase tracking-[0.1em] text-clarity-ink-dim">
            Sending
          </span>
          <span
            aria-hidden="true"
            className="font-clarity-mono text-[17px] font-bold text-clarity-ink"
          >
            {display}
          </span>
        </div>
        <div className="shrink-0 border-l border-dashed border-clarity-line-strong pl-3.5">
          <button
            type="button"
            onClick={handleUndo}
            aria-busy={undoDisabled}
            aria-describedby={noticeId}
            className={cn(
              "min-h-11 px-1.5 font-clarity-sans text-[13.5px] font-extrabold uppercase tracking-[0.03em] text-clarity-emergency underline underline-offset-[3px]",
              undoDisabled && "opacity-60",
            )}
          >
            Undo
          </button>
        </div>
      </div>
      <div className="mt-3 h-1 overflow-hidden rounded-full bg-clarity-line" aria-hidden="true">
        <div
          className="h-full rounded-full bg-clarity-brand transition-[width] duration-1000 ease-linear motion-reduce:transition-none"
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  );
}
