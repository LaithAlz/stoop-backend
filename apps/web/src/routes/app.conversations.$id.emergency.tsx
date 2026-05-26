import { createFileRoute, Link, notFound } from "@tanstack/react-router";
import {
  ArrowLeft,
  AlertOctagon,
  Phone,
  MessageSquare,
  Wrench,
  Image as ImageIcon,
} from "lucide-react";
import { PhoneFrame } from "@/components/stoop/PhoneFrame";
import { queue } from "@/lib/mock-app";

export const Route = createFileRoute("/app/conversations/$id/emergency")({
  head: () => ({
    meta: [
      { title: "Emergency — Stoop." },
      { name: "robots", content: "noindex" },
    ],
  }),
  loader: ({ params }) => {
    const item = queue.find((q) => q.id === params.id);
    if (!item) throw notFound();
    return item;
  },
  notFoundComponent: () => (
    <PhoneFrame tone="dark">
      <div className="flex flex-1 flex-col items-center justify-center px-6 text-center text-white">
        <h1 className="font-display text-2xl font-bold">Alert not found.</h1>
        <Link
          to="/app"
          className="mt-6 inline-flex h-12 items-center rounded-xl bg-white px-5 text-sm font-bold text-ink"
        >
          Back to queue
        </Link>
      </div>
    </PhoneFrame>
  ),
  errorComponent: () => (
    <PhoneFrame tone="dark">
      <div className="flex flex-1 flex-col items-center justify-center px-6 text-center text-white">
        <h1 className="font-display text-2xl font-bold">Couldn't load this alert.</h1>
      </div>
    </PhoneFrame>
  ),
  component: EmergencyPage,
});

function EmergencyPage() {
  const item = Route.useLoaderData();
  const phoneDigits = "905-555-4421";

  return (
    <PhoneFrame tone="dark">
      <div className="flex flex-1 flex-col text-white" style={{ backgroundColor: "#0f1311" }}>
        {/* Top banner */}
        <header className="bg-emergency px-5 pb-6 pt-4">
          <div className="flex items-center justify-between">
            <Link
              to="/app/conversations/$id"
              params={{ id: item.id }}
              aria-label="Back to conversation"
              className="inline-flex size-11 items-center justify-center rounded-full bg-white/15 text-white hover:bg-white/25"
            >
              <ArrowLeft className="size-5" aria-hidden="true" />
            </Link>
            <span className="inline-flex items-center gap-2 rounded-full bg-white/15 px-3 py-1.5 text-[11px] font-bold uppercase tracking-widest text-white">
              <AlertOctagon className="size-3.5" aria-hidden="true" />
              <span className="inline-flex size-2 animate-pulse rounded-full bg-white motion-reduce:animate-none" />
              Emergency · {item.receivedAgo}
            </span>
            <span className="w-11" />
          </div>

          <div className="mt-5">
            <p className="text-[11px] font-bold uppercase tracking-widest text-white/80">
              Property
            </p>
            <h1 className="mt-1 font-display text-[26px] font-bold leading-tight tracking-tight text-white">
              123 Main St, Unit 4
              <br />
              Oakville, ON
            </h1>
          </div>

          <a
            href={`tel:+1${phoneDigits.replace(/\D/g, "")}`}
            className="mt-5 flex items-center justify-between rounded-2xl bg-white/15 px-4 py-3 text-white hover:bg-white/25"
          >
            <span>
              <span className="block text-[11px] font-bold uppercase tracking-widest text-white/80">
                Tenant
              </span>
              <span className="block text-base font-bold">
                {item.tenantFirst} · {item.tenantPhoneMasked}
              </span>
            </span>
            <span className="inline-flex items-center gap-1.5 text-[11px] font-bold uppercase tracking-widest">
              <Phone className="size-4" aria-hidden="true" />
              Tap to call
            </span>
          </a>
        </header>

        {/* Body */}
        <main className="flex-1 overflow-y-auto px-5 py-6">
          <p className="text-[11px] font-bold uppercase tracking-widest text-white/60">
            Situation
          </p>
          <p className="mt-2 font-display text-2xl font-bold leading-snug text-white">
            Active ceiling flood above the bedroom. Tenant is awake and unhurt. Water is reaching
            the bed.
          </p>

          <div className="mt-6 rounded-2xl border border-white/15 bg-white/[0.04] p-4">
            <p className="text-[10px] font-bold uppercase tracking-widest text-white/60">
              From {item.tenantFirst} · {item.receivedAgo}
            </p>
            <p className="mt-2 text-[17px] leading-relaxed text-white">
              “{item.tenantMessage}”
            </p>
          </div>

          {/* Photo */}
          <div className="mt-4">
            <div className="flex aspect-[4/3] w-full items-center justify-center rounded-2xl border border-white/15 bg-white/[0.04] text-white/40">
              <ImageIcon className="size-10" aria-hidden="true" />
              <span className="sr-only">Tenant photo of ceiling flood</span>
            </div>
            <p className="mt-2 font-mono text-[10px] font-medium uppercase tracking-widest text-white/50">
              Photo · {item.receivedAgo}
            </p>
          </div>

          {/* Agent acknowledgement already sent */}
          <div className="mt-6 rounded-2xl border border-white/10 bg-white/[0.04] p-4">
            <p className="text-[10px] font-bold uppercase tracking-widest text-white/60">
              Stoop already replied
            </p>
            <p className="mt-2 text-sm leading-relaxed text-white/90">
              “This is an emergency — I'm contacting your landlord right now. If anyone is in
              danger or it's gas/fire, call 911. Move valuables and turn off the water main if
              you can.”
            </p>
          </div>
        </main>

        {/* Action stack */}
        <div className="space-y-2 border-t border-white/10 bg-black/30 p-4 pb-6">
          <a
            href={`tel:+1${phoneDigits.replace(/\D/g, "")}`}
            className="flex min-h-[60px] w-full items-center justify-center gap-2 rounded-2xl bg-white text-lg font-bold uppercase tracking-wide text-ink hover:bg-white/95"
          >
            <Phone className="size-5" aria-hidden="true" />
            Call {item.tenantFirst} now
          </a>
          <a
            href={`sms:+1${phoneDigits.replace(/\D/g, "")}`}
            className="flex min-h-[60px] w-full items-center justify-center gap-2 rounded-2xl border border-white/20 bg-white/[0.06] text-base font-bold text-white hover:bg-white/15"
          >
            <MessageSquare className="size-5" aria-hidden="true" />
            Text {item.tenantFirst}
          </a>
          <button
            type="button"
            className="flex min-h-[60px] w-full items-center justify-center gap-2 rounded-2xl border border-white/20 bg-white/[0.06] text-base font-bold text-white hover:bg-white/15"
          >
            <Wrench className="size-5" aria-hidden="true" />
            Dispatch Mike's Plumbing (24/7)
          </button>
        </div>
      </div>
    </PhoneFrame>
  );
}
