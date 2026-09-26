"use client";

import { useRouter } from "next/navigation";
import { useRef, useState } from "react";

import { api, type PublicConfig } from "@/lib/api";
import { bytes } from "@/lib/format";

import { Panel } from "./ui";

const TYPES: Record<string, string> = {
  general: "General analysis",
  contract: "Contract review",
  risk: "Risk assessment",
};

export function UploadCard({ config }: { config: PublicConfig | null }) {
  const router = useRouter();
  const input = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [analysisType, setAnalysisType] = useState("contract");
  const [forceFailure, setForceFailure] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const maxMb = config?.max_document_size_mb ?? 25;

  function pick(f: File | undefined | null) {
    setError(null);
    if (!f) return;
    if (f.size > maxMb * 1024 * 1024) {
      setError(`File exceeds the ${maxMb} MB limit.`);
      return;
    }
    setFile(f);
  }

  async function submit() {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const job = await api.createJob(file, analysisType, forceFailure);
      router.push(`/jobs/${job.job_id}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <Panel title="Launch a job">
      <div
        className="flex cursor-pointer flex-col items-center justify-center rounded-md border border-dashed border-line-strong bg-panel-2/50 px-4 py-6 text-center hover:border-accent/60"
        onClick={() => input.current?.click()}
        onDragOver={(e) => e.preventDefault()}
        onDrop={(e) => {
          e.preventDefault();
          pick(e.dataTransfer.files?.[0]);
        }}
      >
        <input
          ref={input}
          type="file"
          accept="application/pdf,.pdf"
          className="hidden"
          data-testid="file-input"
          onChange={(e) => pick(e.target.files?.[0])}
        />
        {file ? (
          <>
            <div className="font-mono text-sm">{file.name}</div>
            <div className="text-xs text-muted">{bytes(file.size)}</div>
          </>
        ) : (
          <>
            <div className="text-sm">Drop a PDF or click to choose</div>
            <div className="mt-1 text-xs text-muted">
              PDF only · max {maxMb} MB · max {config?.max_document_pages ?? 100} pages
            </div>
          </>
        )}
      </div>

      <label className="mt-4 block text-xs text-muted" htmlFor="analysis-type">
        Analysis type
      </label>
      <select
        id="analysis-type"
        value={analysisType}
        onChange={(e) => setAnalysisType(e.target.value)}
        className="mt-1 w-full rounded border border-line-strong bg-panel-2 px-3 py-2 text-sm"
      >
        {(config?.analysis_types ?? Object.keys(TYPES)).map((t) => (
          <option key={t} value={t}>
            {TYPES[t] ?? t}
          </option>
        ))}
      </select>

      {config?.failure_injection_allowed ? (
        <label className="mt-4 flex items-start gap-2 text-sm">
          <input
            type="checkbox"
            checked={forceFailure || config.force_inference_failure_globally}
            disabled={config.force_inference_failure_globally}
            onChange={(e) => setForceFailure(e.target.checked)}
            className="mt-0.5 accent-[var(--color-bad)]"
            data-testid="force-failure"
          />
          <span>
            Inject inference failure <span className="text-xs text-muted">(demo: proves the GPU is destroyed anyway)</span>
          </span>
        </label>
      ) : null}

      <button
        type="button"
        disabled={!file || busy}
        onClick={submit}
        className="mt-4 w-full rounded-md bg-accent px-4 py-2.5 font-mono text-sm font-bold tracking-wider text-black uppercase transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-40"
      >
        {busy ? "Submitting…" : "Analyze document"}
      </button>
      {error ? <p className="mt-3 text-sm text-bad">{error}</p> : null}
      <p className="mt-3 text-xs text-dim">
        Target GPU: {config?.gpu_preference ?? "—"} (≥ {config?.gpu_min_vram_gb ?? "—"} GB VRAM, with fallback) · Model:{" "}
        <span className="font-mono">{config?.model_id ?? "—"}</span>
      </p>
    </Panel>
  );
}
