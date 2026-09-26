import { ArchitectureDiagram } from "@/components/ArchitectureDiagram";
import { Panel } from "@/components/ui";

const STATES = [
  ["QUEUED", "Job accepted (HTTP 202). PDF stored as document.pdf in a job-scoped 0700 directory; only metadata in PostgreSQL."],
  ["PROVISIONING", "Worker validates + extracts the PDF first (sandboxed subprocess), then searches Brev for GPUs ≥ min VRAM, preferring L40S, and creates ephemera-<id>."],
  ["BOOTSTRAPPING", "Uploads infra/gpu/bootstrap.sh; verifies nvidia-smi, Docker and the NVIDIA container runtime; records the real GPU model and VRAM."],
  ["MODEL_LOADING → READY", "Starts vLLM in a container published on 127.0.0.1 only; polls /health with exponential backoff."],
  ["TRANSFERRING", "Copies the request payload over Brev SSH. The document is delimited as untrusted data."],
  ["INFERENCING", "curl on the instance → localhost vLLM with a JSON schema. Output validated; one stricter retry."],
  ["CLEANING", "Removes the remote job directory and model container, then the local job directory."],
  ["DESTROYING → VERIFYING", "brev delete ephemera-<id> (retried), then polls brev ls until the instance is absent."],
  ["COMPLETED / FAILED / CLEANUP_FAILED", "CLEANUP_FAILED means destruction could not be verified — the leak blocks new GPUs until reconciliation fixes it."],
];

export default function Architecture() {
  return (
    <div className="space-y-6">
      <h1 className="text-2xl font-bold">Architecture</h1>
      <Panel title="Components">
        <ArchitectureDiagram />
      </Panel>
      <Panel title="Lifecycle states">
        <dl className="divide-y divide-line">
          {STATES.map(([s, d]) => (
            <div key={s} className="grid gap-2 py-2.5 sm:grid-cols-[260px_1fr]">
              <dt className="font-mono text-xs text-accent">{s}</dt>
              <dd className="text-sm text-muted">{d}</dd>
            </div>
          ))}
        </dl>
      </Panel>
      <Panel title="Trust boundaries">
        <ul className="list-disc space-y-1.5 pl-5 text-sm text-muted">
          <li>The browser talks only to this origin; /api is proxied server-side. It can never reach a GPU or model server.</li>
          <li>The model has no tools: it receives text and returns JSON. It cannot run commands, read credentials or call Brev.</li>
          <li>Brev commands are fixed argument vectors built from UUID-derived names; filenames never reach a CLI.</li>
          <li>Secrets live only in environment variables; HF_TOKEN reaches the instance as a 0600 file deleted after model start.</li>
        </ul>
      </Panel>
    </div>
  );
}
