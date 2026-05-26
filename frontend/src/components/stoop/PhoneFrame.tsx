import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

interface PhoneFrameProps {
  children: ReactNode;
  /** Use dark surround for the emergency view. */
  tone?: "light" | "dark";
}

export function PhoneFrame({ children, tone = "light" }: PhoneFrameProps) {
  return (
    <div
      className={cn(
        "min-h-screen px-4 py-6 md:py-12",
        tone === "dark" ? "bg-ink" : "bg-surface",
      )}
    >
      <div className="mx-auto w-full max-w-[375px]">
        <div
          className={cn(
            "overflow-hidden rounded-[2.25rem] border shadow-2xl md:rounded-[2.5rem]",
            tone === "dark"
              ? "border-white/10 bg-[#0f1311]"
              : "border-border bg-canvas",
          )}
        >
          <div className="flex min-h-[760px] flex-col">{children}</div>
        </div>
      </div>
    </div>
  );
}
