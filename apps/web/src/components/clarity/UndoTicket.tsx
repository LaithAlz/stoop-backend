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
      {/* #191 round 4 item 3 (safety review re-verify): `role="status"`
          restored here, on THIS paragraph, a sibling of the Undo button,
          not on the button itself. Round 3 dropped the live role reasoning
          that pairing a live announcement with a FOCUS MOVE on the SAME
          element makes them race: true, but that reasoning doesn't reach
          a live region on a different node. This element is freshly
          mounted the instant a draft starts sending (UndoTicket only ever
          exists while `status === "sending"`), so the announcement fires
          once, right then, independent of whether focus ever reaches the
          button at all. That independence is the point: `markBusy`
          disables Approve on click, so focus sits at `<body>` for the
          whole in-flight request, and a landlord who presses Tab in that
          window ends up on some other control by the time this ticket
          renders, past the point the owner's own focus-move effect
          (QueueRow / ConversationPage) can catch it. This region still
          announces. `{totalSeconds}` is the real, server-derived window
          (queueEntries.ts's `totalUndoSeconds`), never a hardcoded "5"
          that could read wrong against an on-screen countdown showing
          something else. Also still wired as `aria-describedby` below, so
          a screen-reader user who tabs to Undo later in the window hears
          the same window length again, read together with its name. */}
      <p id={noticeId} role="status" className="sr-only">
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
            {/* #191 round 4 item 2 (safety review re-verify): this text
                used to live INSIDE the button, part of its accessible
                NAME, before round 3 moved it out to `aria-describedby`
                only, leaving the name a bare "Undo". A description is the
                first thing users turn off for verbosity, is dropped in
                several browse-mode and rotor contexts, and isn't part of
                the name at all: announced on focus, in the rotor, in a
                say-all pass, and never suppressible by verbosity settings,
                the way a name is. Restored here, alongside
                `aria-describedby` above (kept, for the window length, read
                again if a screen-reader user tabs to Undo later in the
                countdown). `{clamped}` (not `{totalSeconds}`) so a
                screen-reader user who tabs in mid-countdown hears how long
                is ACTUALLY left, matching the visible "00:0X" digits
                above, which stay `aria-hidden` (this span is the only
                accessible-tree carrier of that number now). */}
            <span className="sr-only">
              {" "}
              the message that&rsquo;s sending, {clamped} second{clamped === 1 ? "" : "s"} left
            </span>
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
