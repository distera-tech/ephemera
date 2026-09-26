import type { JobDetail } from "@/lib/api";
import { dateTime } from "@/lib/format";

import { Panel, Pill } from "./ui";

const sev = { low: "neutral", medium: "warn", high: "bad" } as const;

// Model output is untrusted text: it is rendered only as React text nodes (auto-escaped),
// never as HTML or markdown, and its size was bounded by schema validation server-side.
export function ResultPanel({ job, onDelete }: { job: JobDetail; onDelete?: () => void }) {
  const r = job.result;
  if (!r) {
    const msg =
      job.status === "COMPLETED"
        ? "Result expired or was deleted (results are retained temporarily only)."
        : job.status === "FAILED" || job.status === "CLEANUP_FAILED" || job.status === "CANCELLED"
          ? "No result — the job did not complete successfully."
          : "Result will appear once inference completes.";
    return (
      <Panel title="Result">
        <p className="text-sm text-muted">{msg}</p>
      </Panel>
    );
  }
  return (
    <Panel
      title="Result"
      right={
        <div className="flex items-center gap-2">
          {r.requires_human_review ? <Pill tone="warn">Human review required</Pill> : <Pill tone="ok">Review optional</Pill>}
          {onDelete ? (
            <button onClick={onDelete} className="rounded border border-line-strong px-2 py-0.5 text-xs text-muted hover:text-bad">
              Delete result
            </button>
          ) : null}
        </div>
      }
    >
      <div data-testid="result">
        <div className="text-xs text-muted">Document type</div>
        <div className="mb-3 text-sm font-semibold">{r.document_type}</div>
        <div className="text-xs text-muted">Summary</div>
        <p className="mb-4 text-sm leading-relaxed whitespace-pre-wrap">{r.summary}</p>

        {r.key_points.length ? (
          <>
            <div className="text-xs text-muted">Key points</div>
            <ul className="mb-4 list-disc space-y-1 pl-5 text-sm">
              {r.key_points.map((k, i) => (
                <li key={i}>{k}</li>
              ))}
            </ul>
          </>
        ) : null}

        <div className="text-xs text-muted">Potential risks</div>
        {r.potential_risks.length ? (
          <ul className="mb-4 space-y-2">
            {r.potential_risks.map((risk, i) => (
              <li key={i} className="flex items-start gap-2 text-sm">
                <Pill tone={sev[risk.severity]} className="mt-0.5 shrink-0">
                  {risk.severity}
                </Pill>
                <span>{risk.description}</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="mb-4 text-sm text-dim">None identified.</p>
        )}

        {r.entities.length ? (
          <>
            <div className="text-xs text-muted">Entities</div>
            <div className="mb-3 flex flex-wrap gap-1.5">
              {r.entities.map((e, i) => (
                <span key={i} className="rounded border border-line-strong bg-panel-2 px-2 py-0.5 text-xs">
                  {e.name} <span className="text-dim">· {e.type}</span>
                </span>
              ))}
            </div>
          </>
        ) : null}
        <p className="text-xs text-dim">
          Model: <span className="font-mono">{job.model_id}</span> · output validated against a strict schema · retained
          until {dateTime(job.result_expires_at)}. AI output may be wrong — verify before relying on it.
        </p>
      </div>
    </Panel>
  );
}
