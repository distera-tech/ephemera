import type { JobDetail, JobEvent, JobStatus } from "@/lib/api";
import { TERMINAL } from "@/lib/api";
import { secs, time } from "@/lib/format";

type StepState = "done" | "active" | "failed" | "pending" | "skipped";

interface Step {
  key: string;
  label: string;
  statuses: JobStatus[];
  duration?: number | null;
}

function steps(job: JobDetail): Step[] {
  const d = job.durations;
  return [
    { key: "queued", label: "Queued", statuses: ["QUEUED"], duration: d.queue_seconds },
    { key: "prov", label: "Provisioning GPU", statuses: ["PROVISIONING"], duration: d.provisioning_seconds },
    { key: "boot", label: "Bootstrapping", statuses: ["BOOTSTRAPPING"], duration: d.bootstrap_seconds },
    { key: "model", label: "Model loading", statuses: ["MODEL_LOADING", "READY"], duration: d.model_loading_seconds },
    { key: "xfer", label: "Transferring", statuses: ["TRANSFERRING"] },
    { key: "infer", label: "Inferencing", statuses: ["INFERENCING"], duration: d.inference_seconds },
    { key: "clean", label: "Cleaning", statuses: ["CLEANING"], duration: d.cleanup_seconds },
    { key: "destroy", label: "Destroying GPU", statuses: ["DESTROYING"], duration: d.destruction_seconds },
    { key: "verify", label: "Verifying destruction", statuses: ["VERIFYING_DESTRUCTION"] },
  ];
}

/** Derive each step's state from the persisted transition history — never from guesses. */
export function Timeline({ job, events }: { job: JobDetail; events: JobEvent[] }) {
  const visited = new Set<JobStatus>(["QUEUED"]);
  const transitions = events.filter((e) => e.event_type === "state.transition" && e.status);
  const at = new Map<JobStatus, string>();
  transitions.forEach((e) => {
    visited.add(e.status as JobStatus);
    if (!at.has(e.status as JobStatus)) at.set(e.status as JobStatus, e.timestamp);
  });
  const firstCleaning = transitions.findIndex((e) => e.status === "CLEANING");
  const failedAt: JobStatus | null =
    job.error_code && job.status !== "COMPLETED" && firstCleaning > 0
      ? (transitions[firstCleaning - 1].status as JobStatus)
      : job.error_code && firstCleaning === 0
        ? "QUEUED"
        : null;
  const terminal = TERMINAL.includes(job.status);

  const list = steps(job).map((s) => {
    let state: StepState = "pending";
    const seen = s.statuses.some((x) => visited.has(x));
    if (s.statuses.includes(job.status) && !terminal) state = "active";
    else if (failedAt && s.statuses.includes(failedAt)) state = "failed";
    else if (seen) state = "done";
    else if (terminal) state = "skipped";
    return { ...s, state, at: s.statuses.map((x) => at.get(x)).find(Boolean) };
  });

  const zero =
    job.instance_state === "DESTROYED" ? "done" : job.instance_state === "NONE" && terminal ? "skipped" : "pending";
  const finalTone =
    job.status === "COMPLETED" ? "done" : job.status === "FAILED" || job.status === "CLEANUP_FAILED" ? "failed" : terminal ? "skipped" : "pending";

  return (
    <ol className="relative space-y-0" data-testid="timeline">
      {list.map((s) => (
        <Row key={s.key} label={s.label} state={s.state} right={s.duration != null ? secs(s.duration) : s.at ? time(s.at) : ""} />
      ))}
      <Row
        label={job.instance_state === "NONE" && terminal ? "Compute = 0 (no GPU was provisioned)" : "Compute = 0"}
        state={zero as StepState}
        emphasis
        right={job.destruction_completed_at ? time(job.destruction_completed_at) : ""}
      />
      <Row
        label={terminal ? job.status.replaceAll("_", " ") : "Completed"}
        state={finalTone as StepState}
        right={job.completed_at ? time(job.completed_at) : ""}
      />
    </ol>
  );
}

const dot: Record<StepState, string> = {
  done: "bg-ok border-ok",
  active: "bg-warn border-warn pulse-warn",
  failed: "bg-bad border-bad",
  pending: "bg-panel border-line-strong",
  skipped: "bg-panel border-line",
};
const text: Record<StepState, string> = {
  done: "text-fg",
  active: "text-warn",
  failed: "text-bad",
  pending: "text-dim",
  skipped: "text-dim line-through",
};

function Row({ label, state, right, emphasis }: { label: string; state: StepState; right?: string; emphasis?: boolean }) {
  return (
    <li className="relative flex items-center gap-3 py-1.5 pl-1" data-state={state}>
      <span className={`relative z-10 h-3 w-3 shrink-0 rounded-full border-2 ${dot[state]}`} />
      <span
        className={`flex-1 font-mono text-[13px] tracking-wide uppercase ${text[state]} ${emphasis ? "font-bold" : ""} ${
          emphasis && state === "done" ? "text-ok" : ""
        }`}
      >
        {label}
      </span>
      <span className="font-mono text-xs text-muted tabular-nums">{right}</span>
    </li>
  );
}

export function InstanceBanner({ job }: { job: JobDetail }) {
  const map: Record<string, { text: string; cls: string }> = {
    NONE: { text: TERMINAL.includes(job.status) ? "NEVER PROVISIONED" : "NOT YET PROVISIONED", cls: "border-line-strong text-muted bg-panel-2" },
    PROVISIONING: { text: "PROVISIONING", cls: "border-warn/50 text-warn bg-warn-bg pulse-warn" },
    ACTIVE: { text: "ACTIVE", cls: "border-warn/50 text-warn bg-warn-bg pulse-warn" },
    DESTROYING: { text: "DESTROYING", cls: "border-warn/50 text-warn bg-warn-bg" },
    DESTROYED: { text: "DESTROYED", cls: "border-ok/40 text-ok bg-ok-bg" },
    DESTROY_FAILED: { text: "DESTROY NOT VERIFIED", cls: "border-bad/50 text-bad bg-bad-bg" },
  };
  const s = map[job.instance_state] ?? map.NONE;
  return (
    <div className={`settle rounded-lg border px-4 py-3 ${s.cls}`} key={job.instance_state} data-testid="instance-banner">
      <div className="font-mono text-[10px] tracking-[0.2em] opacity-80">GPU INSTANCE</div>
      <div className="font-mono text-xl font-bold tracking-widest">{s.text}</div>
      <div className="mt-0.5 truncate font-mono text-xs opacity-80">{job.instance_name}</div>
    </div>
  );
}
