import type { Ref } from "react";
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
  /** #191 F2/F4 (safety review follow-up): the owner (DecisionCard's row,
   *  or the conversation thread) focuses THIS button the moment a draft
   *  starts sending, so the button's own accessible name and its
   *  `aria-describedby` text get read together at the one moment that
   *  matters, on the control they describe. */
  undoButtonRef?: Ref<HTMLButtonElement>;
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
  undoButtonRef,
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
      {/* #191 F2/F4 (safety review follow-up): an earlier version of this
          fix made this a `role="alert"` live region, on the theory that a
          screen-reader user needs to be told a clock is running. The
          reviewer's re-verify found the live region wasn't actually doing
          that: nothing here focused the Undo button itself, so on Home
          focus landed two tab stops above it (the row wrapper) and on the
          thread it stayed at `<body>`, meaning the description only ever
          fired if the landlord happened to tab in during the five-second
          window, with the live region racing that same focus move. The
          owner now focuses THIS button directly on the `-> sending`
          transition (see QueueRow / ConversationPage), so the browser's
          normal "read the accessible name plus its description on focus"
          behavior delivers one clean announcement, "Undo, button, Undo is
          available for N seconds after you approve", at the instant it
          matters, on the control it describes. No live role here on
          purpose: pairing a live announcement with a focus move on the
          SAME element makes them race and can cancel each other.
          `{totalSeconds}` is the real, server-derived window
          (queueEntries.ts's `totalUndoSeconds`), never a hardcoded "5"
          that could read wrong against an on-screen countdown showing
          something else. */}
      <p id={noticeId} className="sr-only">
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
            ref={undoButtonRef}
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
