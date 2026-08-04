import type { Ref } from "react";
import { useEffect, useId, useRef, useState } from "react";
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

/** One source for the window-length sentence, so the `aria-describedby`
 *  description and the live announcement can never drift apart. */
function noticeText(totalSeconds: number): string {
  return `Undo is available for ${totalSeconds} second${totalSeconds === 1 ? "" : "s"} after you approve.`;
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

  // #191 round 5 (safety review re-verify): a live region that is
  // INSERTED already populated is unreliably announced by NVDA and JAWS.
  // The pattern that actually works is an empty region first, populated
  // on a later commit, so the AT observes a mutation rather than a new
  // node. Round 4 mounted `role="status"` with its text already in place
  // and asserted in a comment that it "fires once, right then", which is
  // precisely the shape of claim this file's own house rule warns about.
  // Two nodes now, each doing one job: the static paragraph below is the
  // `aria-describedby` target, and this one is the announcement.
  const [liveNotice, setLiveNotice] = useState("");
  useEffect(() => {
    setLiveNotice(noticeText(totalSeconds));
  }, [totalSeconds]);

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
      {/* #191 round 4 item 3: a live announcement on a node that is NOT
          the focus target. Round 3 dropped the live role reasoning that
          pairing a live announcement with a FOCUS MOVE on the SAME
          element makes them race. True, and it does not reach a live
          region on a different node.

          Why this matters: `markBusy` disables Approve on click, so
          focus sits at `<body>` for the whole in-flight request. A
          landlord who presses Tab in that window ends up on some other
          control by the time this ticket renders, past the point the
          owner's focus-move effect (QueueRow / ConversationPage) can
          catch it. That path, and the one where a queue refetch unmounts
          the card mid-window, are structurally out of reach of any focus
          move. Only a live region covers them.

          Round 5 (re-verify) split this into TWO nodes. The region below
          starts EMPTY and is populated from an effect, because a live
          region inserted already populated is unreliably announced by
          NVDA and JAWS: they announce mutations to a region they were
          already observing, not the arrival of a new one. The static
          paragraph stays the `aria-describedby` target so a landlord who
          tabs to Undo later in the window still hears the window length
          read with the button's name.

          `{totalSeconds}` is the real, server-derived window
          (queueEntries.ts's `totalUndoSeconds`), never a hardcoded "5"
          that could read wrong against an on-screen countdown showing
          something else. */}
      <p role="status" className="sr-only">
        {liveNotice}
      </p>
      <p id={noticeId} className="sr-only">
        {noticeText(totalSeconds)}
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
