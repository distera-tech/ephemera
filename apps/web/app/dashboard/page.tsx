"use client";

import Link from "next/link";

import { ComputeHero } from "@/components/ComputeHero";
import { UploadCard } from "@/components/UploadCard";
import { Panel, Stat, StatusPill } from "@/components/ui";
import { usePolling } from "@/hooks/usePolling";
import { api } from "@/lib/api";
import { clock, dateTime } from "@/lib/format";

export default function Dashboard() {
  const stats = usePolling(api.stats, 1500);
  const jobs = usePolling(api.jobs, 2000);
  const config = usePolling(api.config, 30_000);
  const s = stats.data;

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-2">
        <div>
          <div className="font-mono text-xs tracking-[0.3em] text-accent">EPHEMERA</div>
          <h1 className="font-mono text-2xl font-bold tracking-widest">COMPUTE CONTROL PLANE</h1>
        </div>
        {s && !s.worker_online ? (
          <span className="rounded border border-bad/40 bg-bad-bg px-3 py-1 text-xs text-bad">Worker offline — jobs will stay queued</span>
        ) : null}
      </div>
      {stats.error ? <p className="text-sm text-bad">API unreachable: {stats.error}</p> : null}

      <div className="grid gap-6 lg:grid-cols-[1fr_380px]">
        <div className="space-y-6">
          <ComputeHero stats={s} />
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Active GPU" value={s?.active_gpu_instances ?? "–"} tone={s && s.active_gpu_instances > 0 ? "warn" : "ok"} />
            <Stat label="Running jobs" value={s?.active_jobs ?? "–"} hint={s ? `${s.queued_jobs} queued` : undefined} />
            <Stat label="GPU runtime" value={clock(s?.total_gpu_runtime_seconds)} hint="provision → destroyed" />
            <Stat label="Completed jobs" value={s?.completed_jobs ?? "–"} tone="ok" hint={s ? `${s.failed_jobs + s.cleanup_failed_jobs} failed` : undefined} />
          </div>
          <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
            <Stat label="Avg provisioning" value={clock(s?.avg_provisioning_seconds)} />
            <Stat label="Avg inference" value={clock(s?.avg_inference_seconds)} />
            <Stat label="Avg cleanup" value={clock(s?.avg_cleanup_seconds)} />
            <Stat
              label="Success rate"
              value={s?.success_rate != null ? `${Math.round(s.success_rate * 100)}%` : "–"}
              tone={s && s.cleanup_failed_jobs > 0 ? "bad" : undefined}
              hint={s && s.cleanup_failed_jobs > 0 ? `${s.cleanup_failed_jobs} cleanup failure(s)` : undefined}
            />
          </div>

          <Panel title="Jobs">
            {jobs.data && jobs.data.length === 0 ? (
              <p className="text-sm text-muted">No jobs yet. There is no GPU running.</p>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm" data-testid="jobs-table">
                  <thead>
                    <tr className="border-b border-line text-left font-mono text-[10px] tracking-widest text-muted uppercase">
                      <th className="py-2 pr-3">Job</th>
                      <th className="py-2 pr-3">Document</th>
                      <th className="py-2 pr-3">Status</th>
                      <th className="py-2 pr-3">GPU</th>
                      <th className="py-2 pr-3">Instance</th>
                      <th className="py-2 pr-3 text-right">GPU time</th>
                      <th className="py-2 text-right">Created</th>
                    </tr>
                  </thead>
                  <tbody>
                    {jobs.data?.map((j) => (
                      <tr key={j.id} className="border-b border-line/50 hover:bg-panel-2">
                        <td className="py-2 pr-3 font-mono">
                          <Link className="text-info hover:underline" href={`/jobs/${j.id}`}>
                            {j.display_id}
                          </Link>
                        </td>
                        <td className="max-w-[180px] truncate py-2 pr-3">{j.filename}</td>
                        <td className="py-2 pr-3">
                          <StatusPill status={j.status} />
                        </td>
                        <td className="py-2 pr-3 font-mono text-xs">{j.actual_gpu ?? j.requested_gpu ?? "—"}</td>
                        <td className="py-2 pr-3 font-mono text-xs">
                          <span className={j.instance_state === "DESTROYED" ? "text-ok" : j.instance_state === "NONE" ? "text-dim" : j.instance_state === "DESTROY_FAILED" ? "text-bad" : "text-warn"}>
                            {j.instance_state}
                          </span>
                        </td>
                        <td className="py-2 pr-3 text-right font-mono text-xs tabular-nums">{clock(j.gpu_runtime_seconds)}</td>
                        <td className="py-2 text-right text-xs text-muted">{dateTime(j.created_at)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Panel>
        </div>
        <div className="space-y-6">
          <UploadCard config={config.data} />
          <Panel title="Safety limits">
            <ul className="space-y-1 text-xs text-muted">
              <li>Max concurrent jobs: <span className="font-mono text-fg">{config.data?.max_active_jobs ?? "–"}</span></li>
              <li>Max GPU instances: <span className="font-mono text-fg">{config.data?.max_gpu_instances ?? "–"}</span></li>
              <li>Max job runtime: <span className="font-mono text-fg">{clock(config.data?.max_job_runtime_seconds)}</span></li>
              <li>Result retention: <span className="font-mono text-fg">{clock(config.data?.result_retention_seconds)}</span></li>
            </ul>
            <p className="mt-3 text-xs text-dim">
              Ephemera is designed for workloads where GPU demand is intermittent, so compute is not kept active between jobs.
            </p>
          </Panel>
        </div>
      </div>
    </div>
  );
}
