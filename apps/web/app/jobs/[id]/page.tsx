"use client";

import Link from "next/link";
import { use, useCallback, useState } from "react";

import { EventLog } from "@/components/EventLog";
import { InstanceBanner, Timeline } from "@/components/JobLifecycle";
import { ResultPanel } from "@/components/ResultPanel";
import { KV, Panel, Pill, StatusPill } from "@/components/ui";
import { usePolling } from "@/hooks/usePolling";
import { api, TERMINAL } from "@/lib/api";
import { bytes, clock, dateTime, secs } from "@/lib/format";

export default function JobPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const [done, setDone] = useState(false);
  const loadJob = useCallback(async () => {
    const j = await api.job(id);
    if (TERMINAL.includes(j.status) && j.instance_state !== "DESTROYING") setDone(true);
    return j;
  }, [id]);
  const loadEvents = useCallback(() => api.events(id), [id]);
  const job = usePolling(loadJob, 1200, !done);
  const events = usePolling(loadEvents, 1200, !done);
  const [actionError, setActionError] = useState<string | null>(null);

  if (job.error && !job.data) {
    return (
      <div className="space-y-3">
        <p className="text-bad">Could not load job: {job.error}</p>
        <Link href="/dashboard" className="text-info hover:underline">← Control plane</Link>
      </div>
    );
  }
  const j = job.data;
  if (!j) return <p className="text-muted">Loading…</p>;
  const terminal = TERMINAL.includes(j.status);
  const zero = terminal && (j.instance_state === "DESTROYED" || j.instance_state === "NONE");

  async function cancel() {
    try {
      await api.cancel(id);
      await job.refresh();
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
  }
  async function deleteResult() {
    await api.deleteResult(id);
    await job.refresh();
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <Link href="/dashboard" className="text-xs text-muted hover:text-fg">← Control plane</Link>
          <div className="mt-1 flex items-center gap-3">
            <h1 className="font-mono text-3xl font-bold tracking-wider" data-testid="job-id">{j.display_id}</h1>
            <span data-testid="job-status"><StatusPill status={j.status} /></span>
            {j.mode === "simulation" ? <Pill tone="warn">Simulation</Pill> : <Pill tone="ok">Real · Brev</Pill>}
            {j.force_inference_failure ? <Pill tone="bad">Failure injected</Pill> : null}
          </div>
          <p className="mt-1 text-sm text-muted">{j.filename} · {j.analysis_type} analysis</p>
        </div>
        {!terminal ? (
          <button
            onClick={cancel}
            disabled={j.cancel_requested}
            className="rounded border border-bad/50 px-3 py-1.5 text-sm text-bad hover:bg-bad-bg disabled:opacity-50"
          >
            {j.cancel_requested ? "Cancelling…" : "Cancel job"}
          </button>
        ) : null}
      </div>
      {actionError ? <p className="text-sm text-bad">{actionError}</p> : null}

      <div className="grid gap-4 md:grid-cols-3">
        <InstanceBanner job={j} />
        <div className={`rounded-lg border px-4 py-3 ${zero ? "border-ok/40 bg-ok-bg text-ok" : "border-line bg-panel text-muted"}`} data-testid="compute-zero">
          <div className="font-mono text-[10px] tracking-[0.2em] opacity-80">COMPUTE</div>
          <div className="font-mono text-xl font-bold tracking-widest">{zero ? "COMPUTE = 0" : terminal ? "NOT VERIFIED" : "IN USE"}</div>
          <div className="mt-0.5 text-xs opacity-80">
            {zero ? (j.instance_state === "NONE" ? "No GPU was ever provisioned" : "Destruction verified against provider") : "GPU lifecycle in progress"}
          </div>
        </div>
        <div className="rounded-lg border border-line bg-panel px-4 py-3">
          <div className="font-mono text-[10px] tracking-[0.2em] text-muted">GPU RUNTIME</div>
          <div className="font-mono text-xl font-bold tabular-nums">{clock(j.durations.gpu_runtime_seconds)}</div>
          <div className="mt-0.5 text-xs text-muted">CREATED → USED → DESTROYED</div>
        </div>
      </div>

      {j.error_code ? (
        <div className={`rounded-lg border px-4 py-3 ${j.status === "CLEANUP_FAILED" ? "border-bad/50 bg-bad-bg" : "border-bad/30 bg-bad-bg/60"}`} data-testid="job-error">
          <div className="font-mono text-sm font-bold text-bad">{j.error_code.replaceAll("_", " ")}</div>
          <div className="text-sm text-fg/90">{j.error_message}</div>
          {j.cleanup_report ? (
            <div className="mt-2 flex flex-wrap gap-2">
              <Pill tone={j.cleanup_report.succeeded ? "ok" : "bad"}>
                Cleanup {j.cleanup_report.succeeded ? "successful" : "incomplete"}
              </Pill>
              {j.cleanup_report.destroy_attempted ? (
                <Pill tone={j.cleanup_report.destruction_verified ? "ok" : "bad"}>
                  GPU {j.cleanup_report.destruction_verified ? "destroyed" : "not verified destroyed"}
                </Pill>
              ) : <Pill tone="neutral">No GPU provisioned</Pill>}
            </div>
          ) : null}
        </div>
      ) : null}

      <div className="grid gap-6 lg:grid-cols-[360px_1fr]">
        <div className="space-y-6">
          <Panel title="Lifecycle">
            <Timeline job={j} events={events.data ?? []} />
          </Panel>
          <Panel title="Compute">
            <dl>
              <KV k="Requested GPU" v={j.requested_gpu} />
              <KV k="Actual GPU" v={j.actual_gpu} />
              <KV k="VRAM" v={j.gpu_vram_mb ? `${(j.gpu_vram_mb / 1024).toFixed(1)} GB` : null} />
              <KV k="Instance type" v={j.instance_type} />
              <KV k="Provider" v={j.compute_provider} />
              <KV k="Instance" v={j.instance_name} />
              <KV k="Instance state" v={j.instance_state} />
              <KV k="Model" v={j.model_id} />
              <KV k="Est. cost" v={j.estimated_cost_usd != null ? `$${j.estimated_cost_usd.toFixed(4)}` : "unavailable"} />
            </dl>
            <p className="mt-2 text-xs text-dim">{j.cost_note}</p>
          </Panel>
        </div>
        <div className="space-y-6">
          <ResultPanel job={j} onDelete={j.result ? deleteResult : undefined} />
          <div className="grid gap-6 md:grid-cols-2">
            <Panel title="Timings">
              <dl>
                <KV k="Queued" v={secs(j.durations.queue_seconds)} />
                <KV k="Provisioning" v={secs(j.durations.provisioning_seconds)} />
                <KV k="Bootstrap" v={secs(j.durations.bootstrap_seconds)} />
                <KV k="Model loading" v={secs(j.durations.model_loading_seconds)} />
                <KV k="Inference" v={secs(j.durations.inference_seconds)} />
                <KV k="Cleanup" v={secs(j.durations.cleanup_seconds)} />
                <KV k="Destruction" v={secs(j.durations.destruction_seconds)} />
                <KV k="Total" v={secs(j.durations.total_seconds)} />
              </dl>
            </Panel>
            <Panel title="Document & data lifecycle">
              <dl>
                <KV k="Size" v={bytes(j.file_size)} />
                <KV k="Pages" v={j.page_count} />
                <KV k="Truncated" v={j.input_truncated ? "yes" : "no"} />
                <KV k="SHA-256" v={`${j.sha256.slice(0, 16)}…`} />
                <KV k="Document state" v={j.document_state} />
                <KV k="Local data deleted" v={j.cleanup_report ? String(j.cleanup_report.local_deleted) : "pending"} />
                <KV k="Remote data deleted" v={j.cleanup_report ? String(j.cleanup_report.remote_deleted ?? "n/a") : "pending"} />
                <KV k="Created" v={dateTime(j.created_at)} />
                <KV k="Completed" v={dateTime(j.completed_at)} />
              </dl>
            </Panel>
          </div>
          <Panel title="Audit trail (job_events)">
            <EventLog events={events.data ?? []} />
          </Panel>
        </div>
      </div>
    </div>
  );
}
