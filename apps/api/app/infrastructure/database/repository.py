"""PostgreSQL implementation of the job/event repositories."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, fields
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.logging import get_logger
from app.domain.analysis import DocumentAnalysis
from app.domain.models import AnalysisType, Job
from app.domain.status import (
    TERMINAL_STATES,
    DocumentState,
    InstanceState,
    JobStatus,
    assert_transition,
)
from app.infrastructure.database.models import (
    JobEventRow,
    JobResultRow,
    JobRow,
    WorkerHeartbeatRow,
)

log = get_logger("repository")

_TERMINAL = [s.value for s in TERMINAL_STATES]
_ACTIVE_INSTANCE_STATES = [s.value for s in InstanceState if s.counts_as_active]
_JOB_FIELDS = {f.name for f in fields(Job)}
# Serialises job claiming across workers so MAX_ACTIVE_JOBS / MAX_GPU_INSTANCES hold.
_CLAIM_LOCK_KEY = 0x45504845  # "EPHE"


def utcnow() -> datetime:
    return datetime.now(UTC)


def _to_domain(row: JobRow) -> Job:
    data = {name: getattr(row, name) for name in _JOB_FIELDS if hasattr(row, name)}
    data["status"] = JobStatus(row.status)
    data["instance_state"] = InstanceState(row.instance_state)
    data["document_state"] = DocumentState(row.document_state)
    data["analysis_type"] = AnalysisType(row.analysis_type)
    return Job(**data)


def _serialisable(fields_: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in fields_.items():
        if key not in _JOB_FIELDS or key in {"id", "created_at", "status"}:
            # status changes only through transition(), which validates and records an event
            raise ValueError(f"unknown or immutable job field: {key}")
        out[key] = value.value if hasattr(value, "value") else value
    return out


@dataclass(frozen=True, slots=True)
class JobListItem:
    job: Job
    seq: int


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: int
    job_id: uuid.UUID
    event_type: str
    level: str
    status: str | None
    message: str
    metadata: dict[str, Any] | None
    timestamp: datetime


class PostgresJobRepository:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession]) -> None:
        self._sm = sessionmaker

    # ------------------------------------------------------------------ creation / reads
    async def create(
        self,
        *,
        job_id: uuid.UUID,
        filename: str,
        content_type: str,
        file_size: int,
        sha256: str,
        analysis_type: AnalysisType,
        mode: str,
        model_id: str,
        requested_gpu: str | None,
        instance_name: str,
        force_inference_failure: bool,
        owner_id: str = "demo",
    ) -> Job:
        async with self._sm.begin() as s:
            row = JobRow(
                id=job_id,
                owner_id=owner_id,
                status=JobStatus.QUEUED.value,
                mode=mode,
                analysis_type=analysis_type.value,
                filename=filename,
                content_type=content_type,
                file_size=file_size,
                sha256=sha256,
                document_state=DocumentState.RECEIVED.value,
                model_id=model_id,
                requested_gpu=requested_gpu,
                instance_name=instance_name,
                instance_state=InstanceState.NONE.value,
                force_inference_failure=force_inference_failure,
                cancel_requested=False,
                input_truncated=False,
            )
            s.add(row)
            await s.flush()
            s.add(
                JobEventRow(
                    job_id=job_id,
                    event_type="job.created",
                    status=JobStatus.QUEUED.value,
                    message="Job accepted and queued",
                    metadata_json={"file_size": file_size, "analysis_type": analysis_type.value},
                )
            )
            await s.flush()
            await s.refresh(row)
            return _to_domain(row)

    async def get(self, job_id: uuid.UUID) -> Job | None:
        async with self._sm() as s:
            row = await s.get(JobRow, job_id)
            return _to_domain(row) if row else None

    async def get_with_seq(self, job_id: uuid.UUID) -> JobListItem | None:
        async with self._sm() as s:
            row = await s.get(JobRow, job_id)
            return JobListItem(_to_domain(row), row.seq) if row else None

    async def list_jobs(self, limit: int = 50, offset: int = 0) -> list[JobListItem]:
        async with self._sm() as s:
            rows = (
                await s.execute(
                    select(JobRow).order_by(JobRow.created_at.desc()).limit(limit).offset(offset)
                )
            ).scalars()
            return [JobListItem(_to_domain(r), r.seq) for r in rows]

    async def list_events(self, job_id: uuid.UUID, after_id: int = 0) -> list[EventRecord]:
        async with self._sm() as s:
            rows = (
                await s.execute(
                    select(JobEventRow)
                    .where(JobEventRow.job_id == job_id, JobEventRow.id > after_id)
                    .order_by(JobEventRow.id)
                )
            ).scalars()
            return [
                EventRecord(
                    id=r.id,
                    job_id=r.job_id,
                    event_type=r.event_type,
                    level=r.level,
                    status=r.status,
                    message=r.message,
                    metadata=r.metadata_json,
                    timestamp=r.timestamp,
                )
                for r in rows
            ]

    # ------------------------------------------------------------------ state machine
    async def transition(
        self,
        job_id: uuid.UUID,
        target: JobStatus,
        message: str,
        *,
        metadata: dict[str, Any] | None = None,
        fields: dict[str, Any] | None = None,
    ) -> Job:
        async with self._sm.begin() as s:
            row = await s.get(JobRow, job_id, with_for_update=True)
            if row is None:
                raise LookupError(f"job {job_id} not found")
            current = JobStatus(row.status)
            assert_transition(current, target)
            row.status = target.value
            for key, value in _serialisable(fields or {}).items():
                setattr(row, key, value)
            if target.is_terminal and row.completed_at is None:
                row.completed_at = utcnow()
            s.add(
                JobEventRow(
                    job_id=job_id,
                    event_type="state.transition",
                    status=target.value,
                    message=message,
                    metadata_json={"from": current.value, "to": target.value, **(metadata or {})},
                    level="error"
                    if target in (JobStatus.FAILED, JobStatus.CLEANUP_FAILED)
                    else "info",
                )
            )
            await s.flush()
            log.info(
                "job.transition",
                extra={
                    "job_id": str(job_id),
                    "from_status": current.value,
                    "to_status": target.value,
                },
            )
            return _to_domain(row)

    async def update_fields(self, job_id: uuid.UUID, **fields_: Any) -> None:
        if not fields_:
            return
        async with self._sm.begin() as s:
            await s.execute(
                update(JobRow).where(JobRow.id == job_id).values(**_serialisable(fields_))
            )

    async def record_event(
        self,
        job_id: uuid.UUID,
        event_type: str,
        message: str,
        metadata: dict[str, Any] | None = None,
        level: str = "info",
    ) -> None:
        async with self._sm.begin() as s:
            status = (await s.execute(select(JobRow.status).where(JobRow.id == job_id))).scalar()
            s.add(
                JobEventRow(
                    job_id=job_id,
                    event_type=event_type,
                    level=level,
                    status=status,
                    message=message,
                    metadata_json=metadata,
                )
            )

    # ------------------------------------------------------------------ worker coordination
    async def claim_next(
        self, worker_id: str, max_active_jobs: int, max_gpu_instances: int
    ) -> tuple[Job | None, str | None]:
        """Claim the oldest queued job if limits allow. Returns (job, reason_if_blocked)."""
        async with self._sm.begin() as s:
            await s.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": _CLAIM_LOCK_KEY})
            active_jobs = (
                await s.execute(
                    select(func.count())
                    .select_from(JobRow)
                    .where(
                        JobRow.status.not_in(_TERMINAL),
                        or_(JobRow.status != JobStatus.QUEUED.value, JobRow.worker_id.is_not(None)),
                    )
                )
            ).scalar_one()
            if active_jobs >= max_active_jobs:
                return None, "max_active_jobs"
            active_instances = (
                await s.execute(
                    select(func.count())
                    .select_from(JobRow)
                    .where(JobRow.instance_state.in_(_ACTIVE_INSTANCE_STATES))
                )
            ).scalar_one()
            if active_instances >= max_gpu_instances:
                return None, "max_gpu_instances"
            row = (
                await s.execute(
                    select(JobRow)
                    .where(
                        JobRow.status == JobStatus.QUEUED.value,
                        JobRow.worker_id.is_(None),
                        JobRow.cancel_requested.is_(False),
                    )
                    .order_by(JobRow.created_at)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
            ).scalar_one_or_none()
            if row is None:
                return None, None
            now = utcnow()
            row.worker_id = worker_id
            row.heartbeat_at = now
            row.started_at = now
            s.add(
                JobEventRow(
                    job_id=row.id,
                    event_type="job.claimed",
                    status=row.status,
                    message="Worker picked up job",
                    metadata_json={"worker_id": worker_id},
                )
            )
            await s.flush()
            return _to_domain(row), None

    async def heartbeat(self, job_id: uuid.UUID, worker_id: str) -> None:
        async with self._sm.begin() as s:
            await s.execute(
                update(JobRow)
                .where(JobRow.id == job_id, JobRow.worker_id == worker_id)
                .values(heartbeat_at=utcnow())
            )

    async def is_cancel_requested(self, job_id: uuid.UUID) -> bool:
        async with self._sm() as s:
            return bool(
                (
                    await s.execute(select(JobRow.cancel_requested).where(JobRow.id == job_id))
                ).scalar()
            )

    async def request_cancel(self, job_id: uuid.UUID) -> Job | None:
        async with self._sm.begin() as s:
            row = await s.get(JobRow, job_id, with_for_update=True)
            if row is None:
                return None
            if JobStatus(row.status).is_terminal:
                return _to_domain(row)
            if not row.cancel_requested:
                row.cancel_requested = True
                s.add(
                    JobEventRow(
                        job_id=job_id,
                        event_type="job.cancel_requested",
                        status=row.status,
                        message="Cancellation requested",
                    )
                )
            return _to_domain(row)

    async def list_cancellable_unclaimed(self) -> Sequence[Job]:
        async with self._sm() as s:
            rows = (
                await s.execute(
                    select(JobRow).where(
                        JobRow.status == JobStatus.QUEUED.value,
                        JobRow.worker_id.is_(None),
                        JobRow.cancel_requested.is_(True),
                    )
                )
            ).scalars()
            return [_to_domain(r) for r in rows]

    async def list_stale_in_flight(self, stale_before: datetime) -> Sequence[Job]:
        """Jobs a worker claimed but stopped heart-beating for (crash / SIGKILL / restart)."""
        async with self._sm() as s:
            rows = (
                await s.execute(
                    select(JobRow).where(
                        JobRow.status.not_in(_TERMINAL),
                        JobRow.worker_id.is_not(None),
                        or_(JobRow.heartbeat_at.is_(None), JobRow.heartbeat_at < stale_before),
                    )
                )
            ).scalars()
            return [_to_domain(r) for r in rows]

    async def list_undestroyed_terminal(self) -> Sequence[Job]:
        """Terminal jobs whose instance was never confirmed destroyed — retry destruction."""
        async with self._sm() as s:
            rows = (
                await s.execute(
                    select(JobRow).where(
                        JobRow.status.in_(_TERMINAL),
                        JobRow.instance_state.in_(_ACTIVE_INSTANCE_STATES),
                    )
                )
            ).scalars()
            return [_to_domain(r) for r in rows]

    async def jobs_by_instance_names(self, names: Sequence[str]) -> dict[str, Job]:
        if not names:
            return {}
        async with self._sm() as s:
            rows = (
                await s.execute(select(JobRow).where(JobRow.instance_name.in_(names)))
            ).scalars()
            return {r.instance_name: _to_domain(r) for r in rows}

    # ------------------------------------------------------------------ results
    async def save_result(
        self, job_id: uuid.UUID, analysis: DocumentAnalysis, expires_at: datetime | None
    ) -> None:
        async with self._sm.begin() as s:
            stmt = pg_insert(JobResultRow).values(
                job_id=job_id, analysis=analysis.model_dump(mode="json"), expires_at=expires_at
            )
            await s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[JobResultRow.job_id],
                    set_={
                        "analysis": stmt.excluded.analysis,
                        "expires_at": stmt.excluded.expires_at,
                    },
                )
            )

    async def get_result(
        self, job_id: uuid.UUID
    ) -> tuple[DocumentAnalysis, datetime | None] | None:
        async with self._sm() as s:
            row = await s.get(JobResultRow, job_id)
            if row is None or (row.expires_at is not None and row.expires_at <= utcnow()):
                return None
            return DocumentAnalysis.model_validate(row.analysis), row.expires_at

    async def delete_result(self, job_id: uuid.UUID) -> bool:
        async with self._sm.begin() as s:
            res = await s.execute(delete(JobResultRow).where(JobResultRow.job_id == job_id))
            return bool(res.rowcount)  # type: ignore[attr-defined]

    async def purge_expired_results(self) -> int:
        async with self._sm.begin() as s:
            res = await s.execute(
                delete(JobResultRow).where(
                    and_(JobResultRow.expires_at.is_not(None), JobResultRow.expires_at <= utcnow())
                )
            )
            return int(res.rowcount or 0)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ stats / heartbeat
    async def count_queued(self) -> int:
        async with self._sm() as s:
            return int(
                (
                    await s.execute(
                        select(func.count())
                        .select_from(JobRow)
                        .where(JobRow.status == JobStatus.QUEUED.value)
                    )
                ).scalar_one()
            )

    async def stats(self) -> dict[str, Any]:
        async with self._sm() as s:
            by_status = dict(
                (await s.execute(select(JobRow.status, func.count()).group_by(JobRow.status))).all()
            )
            active_instances = (
                await s.execute(
                    select(func.count())
                    .select_from(JobRow)
                    .where(JobRow.instance_state.in_(_ACTIVE_INSTANCE_STATES))
                )
            ).scalar_one()
            # GPU runtime = provisioning start → destruction confirmed (or now, if still alive).
            runtime = (
                await s.execute(
                    select(
                        func.coalesce(
                            func.sum(
                                func.extract(
                                    "epoch",
                                    func.coalesce(JobRow.destruction_completed_at, func.now())
                                    - JobRow.provisioning_started_at,
                                )
                            ),
                            0,
                        )
                    ).where(JobRow.provisioning_started_at.is_not(None))
                )
            ).scalar_one()
            averages = (
                await s.execute(
                    select(
                        func.avg(
                            func.extract(
                                "epoch",
                                JobRow.provisioning_completed_at - JobRow.provisioning_started_at,
                            )
                        ),
                        func.avg(
                            func.extract(
                                "epoch", JobRow.inference_completed_at - JobRow.inference_started_at
                            )
                        ),
                        func.avg(
                            func.extract(
                                "epoch", JobRow.cleanup_completed_at - JobRow.cleanup_started_at
                            )
                        ),
                        func.avg(func.extract("epoch", JobRow.completed_at - JobRow.created_at)),
                    ).where(JobRow.status.in_(_TERMINAL))
                )
            ).one()
        return {
            "by_status": by_status,
            "active_gpu_instances": int(active_instances),
            "total_gpu_runtime_seconds": float(runtime or 0),
            "avg_provisioning_seconds": _f(averages[0]),
            "avg_inference_seconds": _f(averages[1]),
            "avg_cleanup_seconds": _f(averages[2]),
            "avg_total_runtime_seconds": _f(averages[3]),
        }

    async def upsert_worker_heartbeat(
        self,
        *,
        worker_id: str,
        mode: str,
        compute_provider: str,
        started_at: datetime,
        observed_instances: list[str] | None = None,
        observation_error: str | None = None,
        observed: bool = False,
    ) -> None:
        now = utcnow()
        values: dict[str, Any] = {
            "worker_id": worker_id,
            "mode": mode,
            "compute_provider": compute_provider,
            "started_at": started_at,
            "last_seen_at": now,
        }
        update_set: dict[str, Any] = {"last_seen_at": now, "mode": mode}
        if observed:
            values |= {
                "observed_instances": observed_instances,
                "observed_at": now,
                "observation_error": observation_error,
            }
            update_set |= {
                "observed_instances": observed_instances,
                "observed_at": now,
                "observation_error": observation_error,
            }
        async with self._sm.begin() as s:
            stmt = pg_insert(WorkerHeartbeatRow).values(**values)
            await s.execute(
                stmt.on_conflict_do_update(index_elements=["worker_id"], set_=update_set)
            )

    async def latest_worker(self) -> WorkerHeartbeatRow | None:
        async with self._sm() as s:
            return (
                await s.execute(
                    select(WorkerHeartbeatRow)
                    .order_by(WorkerHeartbeatRow.last_seen_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()


def _f(v: Any) -> float | None:
    return None if v is None else round(float(v), 2)
