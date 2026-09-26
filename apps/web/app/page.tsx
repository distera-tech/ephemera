import Link from "next/link";

import { ArchitectureDiagram } from "@/components/ArchitectureDiagram";

const LIFECYCLE = ["Provision", "Bootstrap", "Load model", "Infer", "Clean", "Destroy", "Verify", "Compute = 0"];

export default function Landing() {
  return (
    <div className="space-y-16 py-8">
      <section className="grid-bg rounded-2xl border border-line px-6 py-16 text-center sm:px-12">
        <p className="font-mono text-xs tracking-[0.3em] text-accent uppercase">Compute-to-zero inference</p>
        <h1 className="mx-auto mt-4 max-w-3xl text-4xl font-bold tracking-tight sm:text-5xl">
          AI inference that disappears when the job is done.
        </h1>
        <p className="mt-4 font-mono text-lg tracking-widest text-muted">Provision. Infer. Destroy.</p>
        <p className="mx-auto mt-6 max-w-2xl text-muted">
          Ephemeral GPU infrastructure for sensitive AI workloads. Ephemera provisions an on-demand GPU through NVIDIA
          Brev, runs an open-weight model in an isolated environment, returns a structured result, deletes temporary
          application data and destroys the instance — then verifies it is gone.
        </p>
        <div className="mt-8 flex flex-wrap justify-center gap-3">
          <Link
            href="/dashboard"
            className="rounded-md bg-accent px-6 py-3 font-mono text-sm font-bold tracking-wider text-black uppercase hover:brightness-110"
          >
            Launch a job
          </Link>
          <Link
            href="/architecture"
            className="rounded-md border border-line-strong px-6 py-3 font-mono text-sm tracking-wider uppercase hover:bg-panel-2"
          >
            View architecture
          </Link>
        </div>
      </section>

      <section>
        <h2 className="font-mono text-xs tracking-[0.2em] text-muted uppercase">The lifecycle</h2>
        <ol className="mt-4 grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
          {LIFECYCLE.map((s, i) => (
            <li
              key={s}
              className={`rounded-md border px-3 py-3 font-mono text-xs tracking-wide uppercase ${
                i === LIFECYCLE.length - 1 ? "border-ok/40 bg-ok-bg text-ok" : "border-line bg-panel"
              }`}
            >
              <span className="text-dim">{String(i + 1).padStart(2, "0")}</span> {s}
            </li>
          ))}
        </ol>
      </section>

      <section className="grid gap-6 lg:grid-cols-3">
        {[
          ["No idle GPU", "Compute exists only for the workload. Between jobs the active GPU count is zero — and the dashboard shows the provider's own listing to prove it."],
          ["Destroy on every path", "Success, model failure, inference failure, timeout, cancellation, worker shutdown: if provisioning was attempted, destruction is attempted and verified. Crashed workers are reconciled on restart."],
          ["Auditable", "Every state transition is persisted with a timestamp. Document text, prompts and credentials are never logged or stored in the database."],
        ].map(([t, d]) => (
          <div key={t} className="rounded-lg border border-line bg-panel p-5">
            <h3 className="font-semibold">{t}</h3>
            <p className="mt-2 text-sm text-muted">{d}</p>
          </div>
        ))}
      </section>

      <section className="rounded-lg border border-line bg-panel p-6">
        <h2 className="mb-4 font-mono text-xs tracking-[0.2em] text-muted uppercase">Architecture</h2>
        <ArchitectureDiagram />
      </section>

      <section className="rounded-lg border border-warn/30 bg-warn-bg/40 p-5 text-sm text-warn">
        Hackathon prototype using synthetic/public non-sensitive data. Ephemera deletes application-level temporary
        data and destroys compute instances; it does not claim physical-media erasure, absolute confidentiality or
        regulatory compliance.
      </section>
    </div>
  );
}
