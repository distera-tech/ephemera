from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import text

from app.api.schemas import (
    JobCreated,
    JobDetail,
    JobEventOut,
    JobSummary,
    PublicConfig,
    Stats,
    detail_for,
    display_id,
    summary_for,
)
from app.api.uploads import UploadRejected, store_pdf_upload
from app.core.config import Settings
from app.core.container import effective_model_id
from app.core.logging import get_logger
from app.domain.models import AnalysisType, Job, instance_name_for
from app.domain.status import JobStatus
from app.infrastructure.database.repository import PostgresJobRepository
from app.infrastructure.storage.workspace import DOCUMENT_FILENAME, JobWorkspace

log = get_logger("api")
router = APIRouter(prefix="/api")


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_repo(request: Request) -> PostgresJobRepository:
    return request.app.state.repo  # type: ignore[no-any-return]


def get_workspace(request: Request) -> JobWorkspace:
    return request.app.state.workspace  # type: ignore[no-any-return]


def current_owner() -> str:
    """Single-user demo. Replace with real authentication; every query already checks ownership."""
    return "demo"


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
RepoDep = Annotated[PostgresJobRepository, Depends(get_repo)]
WorkspaceDep = Annotated[JobWorkspace, Depends(get_workspace)]
OwnerDep = Annotated[str, Depends(current_owner)]


async def _owned_job(repo: PostgresJobRepository, job_id: uuid.UUID, owner: str) -> tuple[Job, int]:
    item = await repo.get_with_seq(job_id)
    if item is None or item.job.owner_id != owner:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "job not found")
    return item.job, item.seq


# ---------------------------------------------------------------------------- jobs
@router.post("/jobs", status_code=status.HTTP_202_ACCEPTED, response_model=JobCreated)
async def create_job(
    settings: SettingsDep,
    repo: RepoDep,
    workspace: WorkspaceDep,
    owner: OwnerDep,
    file: Annotated[UploadFile, File(description="PDF document (demo data only)")],
    analysis_type: Annotated[AnalysisType, Form()] = AnalysisType.GENERAL,
    force_inference_failure: Annotated[bool, Form()] = False,
) -> JobCreated:
    if force_inference_failure and not settings.demo_allow_failure_injection:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "failure injection is disabled on this deployment"
        )
    if await repo.count_queued() >= settings.max_queued_jobs:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "job queue is full; try again later")

    job_id = uuid.uuid4()
    job_dir = workspace.create(job_id)
    try:
        stored = await store_pdf_upload(
            file, job_dir / DOCUMENT_FILENAME, settings.max_document_bytes
        )
        job = await repo.create(
            job_id=job_id,
            filename=stored.display_name,
            content_type="application/pdf",
            file_size=stored.size,
            sha256=stored.sha256,
            analysis_type=analysis_type,
            mode=settings.ephemera_mode.value,
            model_id=effective_model_id(settings),
            requested_gpu=settings.gpu_preference,
            instance_name=instance_name_for(job_id),
            force_inference_failure=force_inference_failure,
            owner_id=owner,
        )
    except UploadRejected as exc:
        workspace.delete(job_id)
        raise HTTPException(exc.status_code, {"code": exc.code, "message": exc.message}) from None
    except Exception:
        workspace.delete(job_id)
        raise
    item = await repo.get_with_seq(job.id)
    assert item is not None
    log.info("job.created", extra={"job_id": str(job_id), "file_size": stored.size})
    return JobCreated(
        job_id=job_id, display_id=display_id(item.seq), status=job.status, mode=job.mode
    )


@router.get("/jobs", response_model=list[JobSummary])
async def list_jobs(
    repo: RepoDep,
    owner: OwnerDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[JobSummary]:
    return [
        summary_for(i.job, i.seq)
        for i in await repo.list_jobs(limit, offset)
        if i.job.owner_id == owner
    ]


@router.get("/jobs/{job_id}", response_model=JobDetail)
async def get_job(job_id: uuid.UUID, repo: RepoDep, owner: OwnerDep) -> JobDetail:
    job, seq = await _owned_job(repo, job_id, owner)
    res = await repo.get_result(job_id)
    return detail_for(job, seq, res[0] if res else None, res[1] if res else None)


@router.get("/jobs/{job_id}/events", response_model=list[JobEventOut])
async def get_job_events(
    job_id: uuid.UUID, repo: RepoDep, owner: OwnerDep, after: Annotated[int, Query(ge=0)] = 0
) -> list[JobEventOut]:
    await _owned_job(repo, job_id, owner)
    return [
        JobEventOut(
            id=e.id,
            event_type=e.event_type,
            level=e.level,
            status=e.status,
            message=e.message,
            metadata=e.metadata,
            timestamp=e.timestamp,
        )
        for e in await repo.list_events(job_id, after)
    ]


@router.post("/jobs/{job_id}/cancel", response_model=JobSummary)
async def cancel_job(job_id: uuid.UUID, repo: RepoDep, owner: OwnerDep) -> JobSummary:
    job, seq = await _owned_job(repo, job_id, owner)
    if job.status.is_terminal:
        raise HTTPException(status.HTTP_409_CONFLICT, f"job already {job.status.value}")
    updated = await repo.request_cancel(job_id)
    assert updated is not None
    return summary_for(updated, seq)


@router.delete("/jobs/{job_id}/result", status_code=status.HTTP_204_NO_CONTENT)
async def delete_result(job_id: uuid.UUID, repo: RepoDep, owner: OwnerDep) -> None:
    await _owned_job(repo, job_id, owner)
    await repo.delete_result(job_id)
    await repo.record_event(job_id, "result.deleted", "Analysis result deleted on request")


# ---------------------------------------------------------------------------- system
@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/readiness")
async def readiness(request: Request, settings: SettingsDep, repo: RepoDep) -> dict[str, object]:
    checks: dict[str, object] = {}
    try:
        async with request.app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:
        checks["database"] = f"error: {type(exc).__name__}"
    worker = await repo.latest_worker() if checks["database"] == "ok" else None
    fresh = worker is not None and worker.last_seen_at > datetime.now(UTC) - timedelta(
        seconds=settings.worker_stale_seconds
    )
    checks["worker"] = "ok" if fresh else "not running"
    problems = settings.configuration_problems()
    checks["configuration"] = problems if problems else "ok"
    ready = checks["database"] == "ok" and not problems
    if not ready:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, {"ready": False, "checks": checks})
    return {"ready": True, "mode": settings.ephemera_mode.value, "checks": checks}


@router.get("/stats", response_model=Stats)
async def stats(settings: SettingsDep, repo: RepoDep) -> Stats:
    s = await repo.stats()
    by = s["by_status"]
    terminal_ok = by.get(JobStatus.COMPLETED.value, 0)
    failed = by.get(JobStatus.FAILED.value, 0)
    cleanup_failed = by.get(JobStatus.CLEANUP_FAILED.value, 0)
    cancelled = by.get(JobStatus.CANCELLED.value, 0)
    finished = terminal_ok + failed + cleanup_failed
    active = sum(
        v for k, v in by.items() if not JobStatus(k).is_terminal and k != JobStatus.QUEUED.value
    )

    worker = await repo.latest_worker()
    now = datetime.now(UTC)
    worker_online = worker is not None and worker.last_seen_at > now - timedelta(
        seconds=settings.worker_stale_seconds
    )
    observed = None
    observed_at = None
    obs_error = None
    if worker is not None and worker.observed_at is not None:
        observed = (
            len(worker.observed_instances or []) if worker.observed_instances is not None else None
        )
        observed_at = worker.observed_at
        obs_error = worker.observation_error
    db_zero = s["active_gpu_instances"] == 0
    verified = db_zero and observed == 0 and obs_error is None
    return Stats(
        mode=settings.ephemera_mode.value,
        active_jobs=active,
        queued_jobs=by.get(JobStatus.QUEUED.value, 0),
        completed_jobs=terminal_ok,
        failed_jobs=failed,
        cleanup_failed_jobs=cleanup_failed,
        cancelled_jobs=cancelled,
        active_gpu_instances=s["active_gpu_instances"],
        provider_observed_instances=observed,
        provider_observed_at=observed_at,
        provider_observation_error=obs_error,
        compute_to_zero=db_zero and (observed in (None, 0)),
        compute_to_zero_verified_by_provider=verified,
        worker_online=worker_online,
        total_gpu_runtime_seconds=round(s["total_gpu_runtime_seconds"], 1),
        success_rate=round(terminal_ok / finished, 3) if finished else None,
        failure_rate=round((failed + cleanup_failed) / finished, 3) if finished else None,
        avg_provisioning_seconds=s["avg_provisioning_seconds"],
        avg_inference_seconds=s["avg_inference_seconds"],
        avg_cleanup_seconds=s["avg_cleanup_seconds"],
        avg_total_runtime_seconds=s["avg_total_runtime_seconds"],
    )


@router.get("/config", response_model=PublicConfig)
async def public_config(settings: SettingsDep) -> PublicConfig:
    return PublicConfig(
        mode=settings.ephemera_mode.value,
        model_id=effective_model_id(settings),
        inference_engine="simulated" if settings.is_simulation else settings.inference_engine.value,
        gpu_preference=settings.gpu_preference,
        gpu_min_vram_gb=settings.gpu_min_vram_gb,
        max_document_size_mb=settings.max_document_size_mb,
        max_document_pages=settings.max_document_pages,
        max_active_jobs=settings.max_active_jobs,
        max_gpu_instances=settings.max_gpu_instances,
        max_job_runtime_seconds=settings.max_job_runtime_seconds,
        result_retention_seconds=settings.result_retention_seconds,
        failure_injection_allowed=settings.demo_allow_failure_injection,
        force_inference_failure_globally=settings.demo_force_inference_failure,
        analysis_types=[a.value for a in AnalysisType],
    )
