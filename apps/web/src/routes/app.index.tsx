import { createFileRoute } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { AppTabBar } from "@/components/stoop/AppTabBar";
import { GreetingHeader } from "@/components/clarity/GreetingHeader";
import { CountsStrip } from "@/components/clarity/CountsStrip";
import { EmergencyBanner } from "@/components/clarity/EmergencyBanner";
import { DecisionCard } from "@/components/clarity/DecisionCard";
import { SkippedCard } from "@/components/clarity/SkippedCard";
import { HandledNote } from "@/components/clarity/HandledNote";
import { AllClearState } from "@/components/clarity/AllClearState";
import { queue, handledNote } from "@/lib/mock-app";

export const Route = createFileRoute("/app/")({
  head: () => ({
    meta: [{ title: "Home — Stoop." }, { name: "robots", content: "noindex" }],
  }),
  component: AppQueuePage,
});

const SEND_WINDOW_SECONDS = 5;

// "sent" (fully sent, undo window elapsed) removes a card from the list.
// "skipped" keeps the card, but collapsed into the muted waiting state —
// skip dismisses the draft, never the case (founder decision, 2026-07-06).
type EntryStatus = "pending" | "sending" | "sent" | "skipped";
type EntryState = Record<string, { status: EntryStatus; secondsLeft: number }>;

function AppQueuePage() {
  const [entries, setEntries] = useState<EntryState>({});

  const hasSending = Object.values(entries).some((e) => e.status === "sending");

  useEffect(() => {
    if (!hasSending) return;
    const timer = setInterval(() => {
      setEntries((prev) => {
        let changed = false;
        const next: EntryState = { ...prev };
        for (const [id, entry] of Object.entries(prev)) {
          if (entry.status !== "sending") continue;
          const secondsLeft = entry.secondsLeft - 1;
          next[id] =
            secondsLeft <= 0 ? { status: "sent", secondsLeft: 0 } : { ...entry, secondsLeft };
          changed = true;
        }
        return changed ? next : prev;
      });
    }, 1000);
    return () => clearInterval(timer);
  }, [hasSending]);

  // Rule #1: the emergency line is never paywalled, throttled, or gated —
  // every emergency renders its own banner, never just the first one.
  const emergencyItems = queue.filter((q) => q.severity === "emergency");

  const decisionItems = useMemo(
    () => queue.filter((q) => q.severity !== "emergency" && entries[q.id]?.status !== "sent"),
    [entries],
  );

  const actionableCount = decisionItems.filter(
    (item) => entries[item.id]?.status !== "skipped",
  ).length;
  const needYou = actionableCount + emergencyItems.length;
  // Mock data doesn't yet model conversations that are simply waiting on a
  // tenant's reply — this stays 0 until lib/mock-app.ts grows that shape.
  const waitingOnTenants = 0;

  function handleApprove(id: string) {
    setEntries((prev) => ({
      ...prev,
      [id]: { status: "sending", secondsLeft: SEND_WINDOW_SECONDS },
    }));
  }

  function handleUndo(id: string) {
    setEntries((prev) => ({
      ...prev,
      [id]: { status: "pending", secondsLeft: SEND_WINDOW_SECONDS },
    }));
  }

  function handleSkip(id: string) {
    setEntries((prev) => ({ ...prev, [id]: { status: "skipped", secondsLeft: 0 } }));
  }

  return (
    <PhoneFrame>
      <div className="flex flex-1 flex-col bg-clarity-bg">
        <GreetingHeader name="Laith">
          <CountsStrip needYou={needYou} waitingOnTenants={waitingOnTenants} />
        </GreetingHeader>

        <main className="flex-1 overflow-y-auto px-[18px] py-4">
          {emergencyItems.map((item) => (
            <EmergencyBanner
              key={item.id}
              conversationId={item.id}
              headline={item.title}
              subtext={`${item.propertyLabel} · tap to call ${item.tenantFirst} now`}
            />
          ))}

          {decisionItems.length === 0 && emergencyItems.length === 0 ? (
            <AllClearState
              message="I'm watching your messages — go enjoy your day. I'll text you if anything needs you."
              lastChecked="Last checked 2 minutes ago."
            />
          ) : (
            decisionItems.length > 0 && (
              <div className="space-y-3.5">
                {decisionItems.map((item) => {
                  const entry = entries[item.id];

                  if (entry?.status === "skipped") {
                    return (
                      <SkippedCard
                        key={item.id}
                        conversationId={item.id}
                        tenantName={item.tenantFirst}
                        propertyLabel={item.propertyLabel}
                        timestamp={item.receivedAgo}
                      />
                    );
                  }

                  return (
                    <DecisionCard
                      key={item.id}
                      severity={item.severity}
                      tenantName={item.tenantFirst}
                      propertyLabel={`${item.unit.split(" · ")[0]}, ${item.propertyLabel}`}
                      timestamp={item.receivedAgo}
                      tenantMessage={item.tenantMessage}
                      photoNote={item.hasPhoto ? item.photoCaption : undefined}
                      draftMessage={item.draftReply}
                      why={item.why ?? "I drafted this from your house rules and past replies."}
                      conversationId={item.id}
                      status={entry?.status === "sending" ? "sending" : "pending"}
                      secondsLeft={entry?.secondsLeft}
                      totalSeconds={SEND_WINDOW_SECONDS}
                      onApprove={() => handleApprove(item.id)}
                      onSkip={() => handleSkip(item.id)}
                      onUndo={() => handleUndo(item.id)}
                    />
                  );
                })}
              </div>
            )
          )}

          <HandledNote>
            <b className="font-semibold text-clarity-ink">{handledNote.summary}</b>{" "}
            {handledNote.explanation}
          </HandledNote>
        </main>

        <AppTabBar active="home" queueCount={needYou} />
      </div>
    </PhoneFrame>
  );
}
