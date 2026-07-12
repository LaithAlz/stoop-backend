import { Image as ImageIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { TimelineMessageEntry } from "@/lib/mock-app";

interface ThreadMessageRowProps {
  entry: TimelineMessageEntry;
  tenantFirst: string;
  className?: string;
}

/**
 * One row of the full conversation history — the tenant's plain-sans
 * bubble, or Stoop's own already-sent reply in solid brand serif italic
 * (docs/mockups/07-clarity-redesign.html `.thread-bubble-in` /
 * `.thread-bubble-sent`). Unlike the queue card's bubbles, there's no
 * "who said this" label inside the bubble here — attribution and time
 * live in the meta line underneath (`.thread-meta`), matching the
 * mockup's conversation-thread frame exactly.
 */
export function ThreadMessageRow({ entry, tenantFirst, className }: ThreadMessageRowProps) {
  const isOutbound = entry.direction === "outbound";
  return (
    <div className={cn("mb-3.5 max-w-[83%]", isOutbound && "ml-auto", className)}>
      <div
        className={cn(
          "rounded-clarity-lg px-[15px] py-[13px] text-[15px] leading-relaxed",
          isOutbound
            ? "rounded-tr-clarity-sm bg-clarity-brand font-clarity-serif italic text-clarity-brand-on"
            : "rounded-tl-clarity-sm border border-clarity-line-strong bg-clarity-panel font-clarity-sans text-clarity-ink",
        )}
      >
        {entry.body}
        {entry.media.map((media, i) => (
          <span
            key={i}
            className="mt-2.5 flex items-center gap-2 rounded-clarity-sm border border-clarity-line-strong bg-clarity-panel py-[5px] pl-[5px] pr-2.5 font-clarity-sans text-xs font-semibold not-italic text-clarity-ink-dim"
          >
            <span className="flex size-[30px] shrink-0 items-center justify-center rounded-[6px] bg-clarity-line text-clarity-ink-dim">
              <ImageIcon className="size-4" aria-hidden="true" />
            </span>
            {media.caption}
          </span>
        ))}
      </div>
      <p
        className={cn(
          "mt-1.5 font-clarity-sans text-[11px] font-semibold text-clarity-ink-dim",
          isOutbound && "text-right",
        )}
      >
        {isOutbound ? (
          <span className="font-bold text-clarity-brand">Sent by Stoop for you</span>
        ) : (
          tenantFirst
        )}{" "}
        · {entry.at}
      </p>
    </div>
  );
}
