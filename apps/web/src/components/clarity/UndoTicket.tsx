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

  return (
    <div
      className={cn(
        "clarity-ticket mt-[15px] rounded-clarity-md border border-clarity-line-strong bg-clarity-surface px-4 pb-3.5 pt-4",
        className,
      )}
    >
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
            onClick={onUndo}
            disabled={undoDisabled}
            aria-busy={undoDisabled}
            className="min-h-11 px-1.5 font-clarity-sans text-[13.5px] font-extrabold uppercase tracking-[0.03em] text-clarity-emergency underline underline-offset-[3px] disabled:opacity-60"
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
      {/* #191 item 2: the button's accessible name used to be "Undo the
          message that's sending — N seconds left", recomputed (and
          potentially re-announced) every tick. A screen-reader user needs
          to know a reply is sending and that Undo exists, not a live
          per-second clock — so the button keeps a stable "Undo" name, and
          this static (non-ticking) `role="status"` text carries the rest,
          announced once when the ticket first appears rather than every
          second. The visible "00:05" digits stay `aria-hidden` above,
          sighted-only, same as before. */}
      <p role="status" className="sr-only">
        Your reply is on its way. Undo is available for 5 seconds.
      </p>
    </div>
  );
}
