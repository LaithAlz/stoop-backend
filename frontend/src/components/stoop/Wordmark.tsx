import { cn } from "@/lib/utils";

interface WordmarkProps {
  size?: "favicon" | "sm" | "md" | "lg" | "xl";
  className?: string;
  tone?: "brand" | "ink" | "canvas";
}

const sizeMap = {
  favicon: "text-base",
  sm: "text-xl",
  md: "text-3xl",
  lg: "text-5xl",
  xl: "text-7xl",
} as const;

const toneMap = {
  brand: "text-brand",
  ink: "text-ink",
  canvas: "text-canvas",
} as const;

export function Wordmark({ size = "md", className, tone = "brand" }: WordmarkProps) {
  return (
    <span
      className={cn(
        "font-display font-bold tracking-tight leading-none",
        sizeMap[size],
        toneMap[tone],
        className,
      )}
      aria-label="Stoop."
    >
      Stoop<span className="text-emergency">.</span>
    </span>
  );
}
