import type { Stats } from "@/lib/api";
import { dateTime } from "@/lib/format";

/** The hero metric: how many GPU instances exist right now. */
export function ComputeHero({ stats }: { stats: Stats | null }) {
  const active = stats?.active_gpu_instances ?? null;
  const zero = stats?.compute_to_zero ?? false;
  const tone = active == null ? "text-dim" : zero ? "text-ok" : "text-warn";
  const ring = active == null ? "border-line" : zero ? "border-ok/40" : "border-warn/50 pulse-warn";
  return (
    <section
      className={`relative overflow-hidden rounded-xl border ${ring} grid-bg bg-panel px-6 py-7`}
      data-testid="compute-hero"
    >
      <div className="font-mono text-[11px] tracking-[0.2em] text-muted uppercase">Current compute</div>
      <div className="mt-2 flex items-end gap-4">
        <span key={String(active)} className={`settle font-mono text-7xl font-bold tabular-nums ${tone}`} data-testid="active-gpus">
          {active ?? "–"}
        </span>
        <span className="mb-2 font-mono text-lg tracking-widest text-muted uppercase">
          GPU instance{active === 1 ? "" : "s"}
        </span>
      </div>
      <div className="mt-4 flex flex-wrap items-center gap-3">
        {zero ? (
          <span className="rounded border border-ok/40 bg-ok-bg px-3 py-1 font-mono text-sm font-bold tracking-widest text-ok">
            COMPUTE = 0
          </span>
        ) : active != null ? (
          <span className="rounded border border-warn/40 bg-warn-bg px-3 py-1 font-mono text-sm font-bold tracking-widest text-warn">
            GPU ACTIVE
          </span>
        ) : null}
        {stats ? (
          <span className="text-xs text-dim">
            {stats.compute_to_zero_verified_by_provider
              ? `Confirmed by ${stats.mode === "simulation" ? "simulated" : "Brev"} provider listing · ${dateTime(stats.provider_observed_at)}`
              : stats.provider_observation_error
                ? `Provider listing failed: ${stats.provider_observation_error}`
                : stats.worker_online
                  ? "Awaiting provider confirmation"
                  : "Worker offline — provider state not observed"}
          </span>
        ) : null}
      </div>
    </section>
  );
}
