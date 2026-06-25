import { Sparkles, Image as ImageIcon } from "lucide-react";
import { cn } from "@/lib/utils";

type Variant = "tenant" | "agent" | "draft" | "photo";

interface MessageBubbleProps {
  variant: Variant;
  text?: string;
  timestamp?: string;
  author?: string;
  className?: string;
}

export function MessageBubble({ variant, text, timestamp, author, className }: MessageBubbleProps) {
  if (variant === "tenant") {
    return (
      <div className={cn("flex flex-col items-start gap-1.5 max-w-[85%]", className)}>
        <div className="rounded-2xl rounded-tl-sm bg-surface px-4 py-3 text-sm leading-relaxed text-ink">
          {text}
        </div>
        <span className="ml-1 text-[10px] font-medium text-ink-muted">
          {author ?? "Tenant"} {timestamp && `• ${timestamp}`}
        </span>
      </div>
    );
  }

  if (variant === "photo") {
    return (
      <div className={cn("flex flex-col items-start gap-1.5 max-w-[70%]", className)}>
        <div className="flex aspect-[4/3] w-56 items-center justify-center rounded-2xl rounded-tl-sm border border-border bg-surface text-ink-muted">
          <ImageIcon className="size-8" aria-hidden="true" />
          <span className="sr-only">Photo from tenant</span>
        </div>
        <span className="ml-1 text-[10px] font-medium text-ink-muted">
          Photo • {timestamp ?? "now"}
        </span>
      </div>
    );
  }

  const isDraft = variant === "draft";
  return (
    <div className={cn("ml-auto flex max-w-[85%] flex-col items-end gap-1.5", className)}>
      <div
        className={cn(
          "rounded-2xl rounded-tr-sm px-4 py-3 text-sm leading-relaxed",
          isDraft
            ? "border-2 border-dashed border-brand/40 bg-brand-muted/40 text-ink"
            : "bg-brand text-brand-foreground",
        )}
      >
        {text}
      </div>
      <div className="mr-1 flex items-center gap-2">
        <span
          className={cn(
            "inline-flex items-center gap-1 rounded-md border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest",
            isDraft
              ? "border-brand/30 bg-canvas text-brand"
              : "border-brand/30 bg-brand-muted text-brand",
          )}
        >
          <Sparkles className="size-2.5" aria-hidden="true" />
          AI assistant
        </span>
        <span className="text-[10px] font-medium text-ink-muted">
          {isDraft ? "Draft — awaiting approval" : "Sent"}
          {timestamp && ` • ${timestamp}`}
        </span>
      </div>
    </div>
  );
}
