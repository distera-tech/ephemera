"""Public API schemas. Nothing here may carry secrets or document content."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from app.domain.analysis import DocumentAnalysis
from app.domain.models import Job
from app.domain.status import JobStatus


class JobCreated(BaseModel):
    job_id: uuid.UUID
    display_id: str
    status: JobStatus
    mode: str


class Durations(BaseModel):
    queue_seconds: float | None = None
    provisioning_seconds: float | None = None
    bootstrap_seconds: float | None = None
    model_loading_seconds: float | None = None
    inference_seconds: float | None = None
    cleanup_seconds: float | None = None
    destruction_seconds: float | None = None
    gpu_runtime_seconds: float | None = None
    total_seconds: float | None = None


class JobSummary(BaseModel):
    id: uuid.UUID
    display_id: str
    status: JobStatus
    mode: str
    filename: str
    analysis_type: str
    model_id: str
    requested_gpu: str | None
    actual_gpu: str | None
    instance_name: str
    instance_state: str
    created_at: datetime
    completed_at: datetime | None
    error_code: str | None
    gpu_runtime_seconds: float | None


class JobDetail(JobSummary):
    content_type: str
    file_size: int
    sha256: str
    page_count: int | None
    input_truncated: bool
    document_state: str
    gpu_vram_mb: int | None
    instance_type: str | None
    compute_provider: str | None
    price_per_hour: float | None
    estimated_cost_usd: float | None
    cost_note: str
    force_inference_failure: bool
    cancel_requested: bool
    error_message: str | None
    started_at: datetime | None
    provisioning_started_at: datetime | None
    provisioning_completed_at: datetime | None
    model_loading_started_at: datetime | None
    model_ready_at: datetime | None
    inference_started_at: datetime | None
    inference_completed_at: datetime | None
    cleanup_started_at: datetime | None
    cleanup_completed_at: datetime | None
    destruction_started_at: datetime | None
    destruction_completed_at: datetime | None
    durations: Durations
    cleanup_report: dict[str, Any] | None
    result: DocumentAnalysis | None
    result_expires_at: datetime | None


class JobEventOut(BaseModel):
    id: int
    event_type: str
    level: str
    status: str | None
    message: str
    metadata: dict[str, Any] | None
    timestamp: datetime


class Stats(BaseModel):
    mode: str
    active_jobs: int
    queued_jobs: int
    completed_jobs: int
    failed_jobs: int
    cleanup_failed_jobs: int
    cancelled_jobs: int
    active_gpu_instances: int
    provider_observed_instances: int | None
    provider_observed_at: datetime | None
    provider_observation_error: str | None
    compute_to_zero: bool
    compute_to_zero_verified_by_provider: bool
    worker_online: bool
    total_gpu_runtime_seconds: float
    success_rate: float | None
    failure_rate: float | None
    avg_provisioning_seconds: float | None
    avg_inference_seconds: float | None
    avg_cleanup_seconds: float | None
    avg_total_runtime_seconds: float | None


class PublicConfig(BaseModel):
    mode: str
    model_id: str
    inference_engine: str
    gpu_preference: str
    gpu_min_vram_gb: float
    max_document_size_mb: float
    max_document_pages: int
    max_active_jobs: int
    max_gpu_instances: int
    max_job_runtime_seconds: int
    result_retention_seconds: int
    failure_injection_allowed: bool
    force_inference_failure_globally: bool
    analysis_types: list[str]


def display_id(seq: int) -> str:
    return f"EPH-{seq:06d}"


def _secs(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return round((b - a).total_seconds(), 1)


def durations_for(job: Job) -> Durations:
    now = datetime.now(UTC)
    gpu_end = job.destruction_completed_at or (None if job.status.is_terminal else now)
    return Durations(
        queue_seconds=_secs(job.created_at, job.started_at),
        provisioning_seconds=_secs(job.provisioning_started_at, job.provisioning_completed_at),
        bootstrap_seconds=_secs(job.provisioning_completed_at, job.model_loading_started_at),
        model_loading_seconds=_secs(job.model_loading_started_at, job.model_ready_at),
        inference_seconds=_secs(job.inference_started_at, job.inference_completed_at),
        cleanup_seconds=_secs(job.cleanup_started_at, job.cleanup_completed_at),
        destruction_seconds=_secs(job.destruction_started_at, job.destruction_completed_at),
        gpu_runtime_seconds=_secs(job.provisioning_started_at, gpu_end),
        total_seconds=_secs(job.created_at, job.completed_at or now),
    )


def summary_for(job: Job, seq: int) -> JobSummary:
    d = durations_for(job)
    return JobSummary(
        id=job.id,
        display_id=display_id(seq),
        status=job.status,
        mode=job.mode,
        filename=job.filename,
        analysis_type=job.analysis_type.value,
        model_id=job.model_id,
        requested_gpu=job.requested_gpu,
        actual_gpu=job.actual_gpu,
        instance_name=job.instance_name,
        instance_state=job.instance_state.value,
        created_at=job.created_at,
        completed_at=job.completed_at,
        error_code=job.error_code,
        gpu_runtime_seconds=d.gpu_runtime_seconds,
    )


def detail_for(
    job: Job, seq: int, result: DocumentAnalysis | None, result_expires_at: datetime | None
) -> JobDetail:
    d = durations_for(job)
    cost: float | None = None
    if job.price_per_hour is not None and d.gpu_runtime_seconds is not None:
        cost = round(job.price_per_hour * d.gpu_runtime_seconds / 3600, 4)
        note = "Estimate: provider list price per hour x observed GPU runtime (excludes storage/egress)."
    elif job.mode == "simulation":
        note = "Cost estimation unavailable in simulation mode."
    else:
        note = "Cost estimation unavailable (provider did not report a price)."
    base = summary_for(job, seq).model_dump()
    return JobDetail(
        **base,
        content_type=job.content_type,
        file_size=job.file_size,
        sha256=job.sha256,
        page_count=job.page_count,
        input_truncated=job.input_truncated,
        document_state=job.document_state.value,
        gpu_vram_mb=job.gpu_vram_mb,
        instance_type=job.instance_type,
        compute_provider=job.compute_provider,
        price_per_hour=job.price_per_hour,
        estimated_cost_usd=cost,
        cost_note=note,
        force_inference_failure=job.force_inference_failure,
        cancel_requested=job.cancel_requested,
        error_message=job.error_message,
        started_at=job.started_at,
        provisioning_started_at=job.provisioning_started_at,
        provisioning_completed_at=job.provisioning_completed_at,
        model_loading_started_at=job.model_loading_started_at,
        model_ready_at=job.model_ready_at,
        inference_started_at=job.inference_started_at,
        inference_completed_at=job.inference_completed_at,
        cleanup_started_at=job.cleanup_started_at,
        cleanup_completed_at=job.cleanup_completed_at,
        destruction_started_at=job.destruction_started_at,
        destruction_completed_at=job.destruction_completed_at,
        durations=d,
        cleanup_report=job.cleanup_report,
        result=result,
        result_expires_at=result_expires_at,
    )
