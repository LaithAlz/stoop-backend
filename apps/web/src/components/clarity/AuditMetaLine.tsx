import { formatRelativeTime } from "@/lib/relativeTime";

interface AuditMetaLineProps {
  label: string;
  at: string;
}

/**
 * A quiet, centered meta line for an audit-trail moment in the case-detail
 * timeline. Ports apps/mobile/src/components/clarity/AuditMetaLine.tsx
 * (campaign issue #234 PR 3) — no web version existed before this PR (the
 * still-mocked thread only ever extracted `why` from the audit payload, it
 * never rendered audit entries as their own rows). Deliberately plain: no
 * icon, no color, no border — the messages and drafts are the content,
 * this is just a footnote about what happened between them.
 */
export function AuditMetaLine({ label, at }: AuditMetaLineProps) {
  return (
    <p className="my-2 text-center font-clarity-sans text-xs text-clarity-ink-dim opacity-85">
      {label} <span className="font-semibold">· {formatRelativeTime(at)}</span>
    </p>
  );
}
