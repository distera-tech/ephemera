import type { ReactNode } from "react";

import type { JobStatus } from "@/lib/api";
import { label, statusTone, toneClasses, type Tone } from "@/lib/format";

export function Panel({
  title,
  right,
  children,
  className = "",
}: {
  title?: ReactNode;
  right?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-lg border border-line bg-panel ${className}`}>
      {title ? (
        <header className="flex items-center justify-between gap-3 border-b border-line px-4 py-2.5">
          <h2 className="font-mono text-[11px] font-semibold tracking-[0.14em] text-muted uppercase">{title}</h2>
          {right}
        </header>
      ) : null}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function Pill({ tone, children, className = "" }: { tone: Tone; children: ReactNode; className?: string }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded border px-2 py-0.5 font-mono text-[11px] font-semibold tracking-wide uppercase ${toneClasses[tone]} ${className}`}
    >
      {children}
    </span>
  );
}

export function StatusPill({ status }: { status: JobStatus }) {
  return <Pill tone={statusTone(status)}>{label(status)}</Pill>;
}

export function Stat({ label, value, hint, tone }: { label: string; value: ReactNode; hint?: ReactNode; tone?: Tone }) {
  const color =
    tone === "ok" ? "text-ok" : tone === "bad" ? "text-bad" : tone === "warn" ? "text-warn" : "text-fg";
  return (
    <div className="rounded-lg border border-line bg-panel px-4 py-3">
      <div className="font-mono text-[10px] tracking-[0.16em] text-muted uppercase">{label}</div>
      <div className={`mt-1 font-mono text-2xl font-semibold tabular-nums ${color}`}>{value}</div>
      {hint ? <div className="mt-0.5 text-xs text-dim">{hint}</div> : null}
    </div>
  );
}

export function KV({ k, v, mono = true }: { k: string; v: ReactNode; mono?: boolean }) {
  return (
    <div className="flex items-baseline justify-between gap-4 border-b border-line/60 py-1.5 last:border-0">
      <dt className="shrink-0 text-xs text-muted">{k}</dt>
      <dd className={`min-w-0 truncate text-right text-sm ${mono ? "font-mono" : ""}`}>{v ?? "—"}</dd>
    </div>
  );
}
