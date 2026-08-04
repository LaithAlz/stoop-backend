import { Image as ImageIcon } from "lucide-react";
import { cn } from "@/lib/utils";
import type { TimelineMessageEntry } from "@/api/types";
import { formatRelativeTime } from "@/lib/relativeTime";

interface ThreadMessageRowProps {
  entry: TimelineMessageEntry;
  tenantFirst: string;
  className?: string;
}

/** `party` covers `tenant` / `vendor` / `landlord` (schema-v1.md
 *  `messages.party` CHECK) — an outbound row is always Stoop sending on the
 *  landlord's behalf; an inbound row can be any of the three (a vendor
 *  texting back, or the landlord's own approve-by-SMS command channel
 *  reply, api-contracts.md's Webhooks section — surfaced here rather than
 *  mislabeled as the tenant).
 *
 * M2 (safety review, #234 PR 3 fix round): an outbound row isn't always
 * TO the tenant — `drafts.recipient` (schema-v1.md) can be `"vendor"`, and
 * that reply lands in this same timeline. Labeling it "Sent by Stoop for
 * you" read as if the TENANT received it, which they didn't. */
function speakerLabel(entry: TimelineMessageEntry, tenantFirst: string): string {
  if (entry.direction === "outbound") {
    return entry.party === "vendor" ? "Sent to the vendor" : "Sent by Stoop for you";
  }
  if (entry.party === "vendor") return "Vendor";
  if (entry.party === "landlord") return "You";
  return tenantFirst;
}

/**
 * One row of the full conversation history — the tenant's plain-sans
 * bubble, or Stoop's own already-sent reply in solid brand serif italic
 * (docs/mockups/07-clarity-redesign.html `.thread-bubble-in` /
 * `.thread-bubble-sent`). Unlike the queue card's bubbles, there's no
 * "who said this" label inside the bubble here — attribution and time
 * live in the meta line underneath (`.thread-meta`), matching the
 * mockup's conversation-thread frame exactly.
 *
 * Wired to `GET /v1/cases/{id}`'s timeline shape (campaign issue #234
 * PR 3, replacing src/lib/mock-app.ts's mock `TimelineMessageEntry`).
 * CONTRACT GAP (api-contracts.md's Cases section, v1.9 amendment): the
 * real `messages.media` shape is `[{url, content_type}]` with no per-photo
 * caption — the mock's `caption` field doesn't exist on the live endpoint,
 * so a media chip here reads a generic "Photo attached" label instead
 * (matches apps/mobile's own port of this component). `at` is a real
 * ISO timestamp now, formatted the same relative way as everywhere else
 * in the app rather than a static mock display string.
 */
export function ThreadMessageRow({ entry, tenantFirst, className }: ThreadMessageRowProps) {
  const isOutbound = entry.direction === "outbound";
  const speaker = speakerLabel(entry, tenantFirst);
  const relativeTime = formatRelativeTime(entry.at);
  return (
    // #191 item 3 / F7 (safety review follow-up): the mockup puts the
    // attribution/time line visually BELOW the bubble
    // (docs/mockups/07-clarity-redesign.html `.thread-meta`), so DOM
    // order matches that: bubble text first, "who sent it" second. A
    // screen reader would then read the message before saying who sent
    // it. An earlier version of this fix used `role="group"` with an
    // `aria-label` to carry who-sent-it/when instead, but group names are
    // materially less reliable in say-all and under VoiceOver than plain
    // content, and an unattributed bubble here means a landlord could
    // read the vendor's words as the tenant's, on the one screen whose
    // whole promise is the record. This sr-only span is just real text,
    // read in normal document order before the bubble it's about, with
    // no ARIA semantics that could go uninterpreted. The now-redundant
    // visible meta `<p>` stays hidden from the accessibility tree so it
    // isn't read a second time.
    <div className={cn("mb-3.5 max-w-[83%]", isOutbound && "ml-auto", className)}>
      <span className="sr-only">
        {speaker}, {relativeTime}
      </span>
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
            key={`${media.url}-${i}`}
            className="mt-2.5 flex items-center gap-2 rounded-clarity-sm border border-clarity-line-strong bg-clarity-panel py-[5px] pl-[5px] pr-2.5 font-clarity-sans text-xs font-semibold not-italic text-clarity-ink-dim"
          >
            <span className="flex size-[30px] shrink-0 items-center justify-center rounded-[6px] bg-clarity-line text-clarity-ink-dim">
              <ImageIcon className="size-4" aria-hidden="true" />
            </span>
            Photo attached
          </span>
        ))}
      </div>
      <p
        aria-hidden="true"
        className={cn(
          "mt-1.5 font-clarity-sans text-[11px] font-semibold text-clarity-ink-dim",
          isOutbound && "text-right",
        )}
      >
        {isOutbound ? <span className="font-bold text-clarity-brand">{speaker}</span> : speaker} ·{" "}
        {relativeTime}
      </p>
    </div>
  );
}
