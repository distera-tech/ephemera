// Typed client for the Ephemera control-plane API.
// Types mirror the Pydantic response models in apps/api/app/api/schemas.py
// (see GET /openapi.json). All calls go to same-origin /api/* which Next.js
// proxies server-side to the API — the browser never talks to GPUs or models.

export type JobStatus =
  | "QUEUED"
  | "PROVISIONING"
  | "BOOTSTRAPPING"
  | "MODEL_LOADING"
  | "READY"
  | "TRANSFERRING"
  | "INFERENCING"
  | "CLEANING"
  | "DESTROYING"
  | "VERIFYING_DESTRUCTION"
  | "COMPLETED"
  | "FAILED"
  | "CLEANUP_FAILED"
  | "CANCELLED";

export const TERMINAL: JobStatus[] = ["COMPLETED", "FAILED", "CLEANUP_FAILED", "CANCELLED"];

export type InstanceState = "NONE" | "PROVISIONING" | "ACTIVE" | "DESTROYING" | "DESTROYED" | "DESTROY_FAILED";

export interface JobSummary {
  id: string;
  display_id: string;
  status: JobStatus;
  mode: string;
  filename: string;
  analysis_type: string;
  model_id: string;
  requested_gpu: string | null;
  actual_gpu: string | null;
  instance_name: string;
  instance_state: InstanceState;
  created_at: string;
  completed_at: string | null;
  error_code: string | null;
  gpu_runtime_seconds: number | null;
}

export interface Durations {
  queue_seconds: number | null;
  provisioning_seconds: number | null;
  bootstrap_seconds: number | null;
  model_loading_seconds: number | null;
  inference_seconds: number | null;
  cleanup_seconds: number | null;
  destruction_seconds: number | null;
  gpu_runtime_seconds: number | null;
  total_seconds: number | null;
}

export interface RiskItem {
  description: string;
  severity: "low" | "medium" | "high";
}

export interface DocumentAnalysis {
  document_type: string;
  summary: string;
  key_points: string[];
  potential_risks: RiskItem[];
  entities: { name: string; type: string }[];
  requires_human_review: boolean;
}

export interface CleanupReport {
  local_deleted: boolean;
  remote_deleted: boolean | null;
  destroy_attempted: boolean;
  destroyed: boolean | null;
  destruction_verified: boolean | null;
  errors: string[];
  succeeded: boolean;
}

export interface JobDetail extends JobSummary {
  content_type: string;
  file_size: number;
  sha256: string;
  page_count: number | null;
  input_truncated: boolean;
  document_state: string;
  gpu_vram_mb: number | null;
  instance_type: string | null;
  compute_provider: string | null;
  price_per_hour: number | null;
  estimated_cost_usd: number | null;
  cost_note: string;
  force_inference_failure: boolean;
  cancel_requested: boolean;
  error_message: string | null;
  started_at: string | null;
  provisioning_started_at: string | null;
  provisioning_completed_at: string | null;
  model_loading_started_at: string | null;
  model_ready_at: string | null;
  inference_started_at: string | null;
  inference_completed_at: string | null;
  cleanup_started_at: string | null;
  cleanup_completed_at: string | null;
  destruction_started_at: string | null;
  destruction_completed_at: string | null;
  durations: Durations;
  cleanup_report: CleanupReport | null;
  result: DocumentAnalysis | null;
  result_expires_at: string | null;
}

export interface JobEvent {
  id: number;
  event_type: string;
  level: "info" | "warning" | "error";
  status: JobStatus | null;
  message: string;
  metadata: Record<string, unknown> | null;
  timestamp: string;
}

export interface Stats {
  mode: string;
  active_jobs: number;
  queued_jobs: number;
  completed_jobs: number;
  failed_jobs: number;
  cleanup_failed_jobs: number;
  cancelled_jobs: number;
  active_gpu_instances: number;
  provider_observed_instances: number | null;
  provider_observed_at: string | null;
  provider_observation_error: string | null;
  compute_to_zero: boolean;
  compute_to_zero_verified_by_provider: boolean;
  worker_online: boolean;
  total_gpu_runtime_seconds: number;
  success_rate: number | null;
  failure_rate: number | null;
  avg_provisioning_seconds: number | null;
  avg_inference_seconds: number | null;
  avg_cleanup_seconds: number | null;
  avg_total_runtime_seconds: number | null;
}

export interface PublicConfig {
  mode: "simulation" | "real";
  model_id: string;
  inference_engine: string;
  gpu_preference: string;
  gpu_min_vram_gb: number;
  max_document_size_mb: number;
  max_document_pages: number;
  max_active_jobs: number;
  max_gpu_instances: number;
  max_job_runtime_seconds: number;
  result_retention_seconds: number;
  failure_injection_allowed: boolean;
  force_inference_failure_globally: boolean;
  analysis_types: string[];
}

export interface JobCreated {
  job_id: string;
  display_id: string;
  status: JobStatus;
  mode: string;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, { cache: "no-store", ...init });
  if (!res.ok) {
    let message = `${res.status} ${res.statusText}`;
    try {
      const body = await res.json();
      const detail = body?.detail;
      message = typeof detail === "string" ? detail : (detail?.message ?? message);
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, message);
  }
  return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
}

export const api = {
  config: () => request<PublicConfig>("/api/config"),
  stats: () => request<Stats>("/api/stats"),
  jobs: () => request<JobSummary[]>("/api/jobs?limit=25"),
  job: (id: string) => request<JobDetail>(`/api/jobs/${encodeURIComponent(id)}`),
  events: (id: string) => request<JobEvent[]>(`/api/jobs/${encodeURIComponent(id)}/events`),
  cancel: (id: string) => request<JobSummary>(`/api/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  deleteResult: (id: string) =>
    request<void>(`/api/jobs/${encodeURIComponent(id)}/result`, { method: "DELETE" }),
  createJob: (file: File, analysisType: string, forceFailure: boolean) => {
    const form = new FormData();
    form.append("file", file);
    form.append("analysis_type", analysisType);
    form.append("force_inference_failure", String(forceFailure));
    return request<JobCreated>("/api/jobs", { method: "POST", body: form });
  },
};
