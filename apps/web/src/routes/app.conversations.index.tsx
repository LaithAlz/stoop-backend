import { createFileRoute } from "@tanstack/react-router";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { ConversationRow } from "@/components/clarity/ConversationRow";
import { queue } from "@/lib/mock-app";

export const Route = createFileRoute("/app/conversations/")({
  head: () => ({
    meta: [{ title: "Conversations — Stoop." }, { name: "robots", content: "noindex" }],
  }),
  component: ConversationsIndexPage,
});

/**
 * The Conversations tab's destination (added per the Tab IA decision,
 * 2026-07-06 — previously only a conversation's detail route existed).
 * Every open thread, most recent first, in the same Clarity material as
 * Home's decision cards — a minimal list, not a table to scan.
 */
function ConversationsIndexPage() {
  return (
    <PhoneFrame>
      <div className="flex flex-1 flex-col bg-clarity-bg">
        <header className="border-b border-clarity-line px-5 pb-3.5 pt-4">
          <h1 className="font-clarity-serif text-[27px] font-semibold leading-[1.2] tracking-tight text-clarity-ink">
            Conversations
          </h1>
          <p className="mt-1 font-clarity-sans text-[13px] font-semibold text-clarity-ink-dim">
            Every tenant thread, saved with dates and times.
          </p>
        </header>

        <main className="flex-1 overflow-y-auto px-[18px] py-4">
          <ul className="space-y-3">
            {queue.map((item) => (
              <li key={item.id}>
                <ConversationRow item={item} />
              </li>
            ))}
          </ul>
        </main>

        <AppTabBar active="conversations" queueCount={queue.length} />
      </div>
    </PhoneFrame>
  );
}
