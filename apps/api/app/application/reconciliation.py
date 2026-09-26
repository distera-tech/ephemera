"""Orphan reconciliation: make the database and the compute provider agree.

Runs at worker start-up and periodically. It handles the cases the in-process
``finally`` cannot: a worker that was SIGKILLed, lost power, or crashed.

1. Claimed jobs of *this* worker id from a previous incarnation, or jobs of any
   worker whose heartbeat is stale → recovered: local cleanup, destroy + verify
   if provisioning was ever attempted, then FAILED (WORKER_INTERRUPTED) or
   CLEANUP_FAILED. Claimed-but-unstarted jobs are simply re-queued.
2. Terminal jobs whose instance was never verified destroyed → destroy retried.
3. ``ephemera-*`` instances reported by the provider that belong to a terminal
   job → destroyed. Instances with no job in this database are only reported
   unless RECONCILE_DELETE_UNKNOWN_INSTANCES=true (another deployment may own
   them). Nothing outside the ``ephemera-<12 hex>`` naming scheme is ever touched.
4. The provider-observed instance list is recorded for the dashboard.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from app.core.logging import get_logger, job_context
from app.domain.errors import ErrorCode
from app.domain.models import Job, is_ephemera_instance_name
from app.domain.ports import ComputeProvider
from app.domain.status import DocumentState, InstanceState, JobStatus
from app.infrastructure.database.repository import PostgresJobRepository
from app.infrastructure.storage.workspace import JobWorkspace

log = get_logger("reconciler")


@dataclass(slots=True)
class ReconcileReport:
    recovered_jobs: list[str] = field(default_factory=list)
    requeued_jobs: list[str] = field(default_factory=list)
    destroyed_orphans: list[str] = field(default_factory=list)
    unknown_instances: list[str] = field(default_factory=list)
    observed_instances: list[str] | None = None
    observation_error: str | None = None


class Reconciler:
    def __init__(
        self,
        *,
        repo: PostgresJobRepository,
        compute: ComputeProvider,
        workspace: JobWorkspace,
        worker_id: str,
        stale_job_seconds: float,
        destroy_timeout_s: float,
        destroy_verify_timeout_s: float,
        delete_unknown_instances: bool = False,
    ) -> None:
        self.repo = repo
        self.compute = compute
        self.workspace = workspace
        self.worker_id = worker_id
        self.stale_job_seconds = stale_job_seconds
        self.destroy_timeout_s = destroy_timeout_s
        self.destroy_verify_timeout_s = destroy_verify_timeout_s
        self.delete_unknown = delete_unknown_instances

    async def reconcile(
        self, *, startup: bool, busy_job_ids: set[str] | None = None
    ) -> ReconcileReport:
        report = ReconcileReport()
        busy = busy_job_ids or set()
        now = datetime.now(UTC)

        # 1. Interrupted jobs.
        stale_before = now if startup else now - timedelta(seconds=self.stale_job_seconds)
        candidates = await self.repo.list_stale_in_flight(stale_before)
        for job in candidates:
            if str(job.id) in busy:
                continue
            is_stale = job.heartbeat_at is None or job.heartbeat_at < now - timedelta(
                seconds=self.stale_job_seconds
            )
            if not (is_stale or (startup and job.worker_id == self.worker_id)):
                continue  # another live worker owns it
            with job_context(str(job.id)):
                if job.status is JobStatus.QUEUED and job.instance_state is InstanceState.NONE:
                    await self.repo.update_fields(
                        job.id, worker_id=None, heartbeat_at=None, started_at=None
                    )
                    await self.repo.record_event(
                        job.id,
                        "reconciliation.requeued",
                        "Worker restarted before the job started; re-queued",
                    )
                    report.requeued_jobs.append(str(job.id))
                else:
                    await self._recover(job)
                    report.recovered_jobs.append(str(job.id))

        # 2. Terminal jobs whose destruction was never verified.
        for job in await self.repo.list_undestroyed_terminal():
            with job_context(str(job.id)):
                if await self._destroy_and_verify(job, reason="retrying unverified destruction"):
                    report.destroyed_orphans.append(job.instance_name)

        # 3. Provider-side orphans.
        try:
            instances = await self.compute.list_instances()
            report.observed_instances = sorted(i.name for i in instances)
        except Exception as exc:
            report.observation_error = (
                type(exc).__name__ + (f": {getattr(exc, 'message', '')}"[:200])
            )
            log.warning("reconcile.list_failed", extra={"error": report.observation_error})
            return report

        by_name = await self.repo.jobs_by_instance_names([i.name for i in instances])
        for inst in instances:
            if not is_ephemera_instance_name(inst.name):
                continue
            owner = by_name.get(inst.name)
            if owner is None:
                report.unknown_instances.append(inst.name)
                if self.delete_unknown:
                    log.warning("reconcile.destroy_unknown", extra={"instance": inst.name})
                    await self._destroy_instance_only(inst.name)
                    report.destroyed_orphans.append(inst.name)
                else:
                    log.warning("reconcile.unknown_instance", extra={"instance": inst.name})
                continue
            if owner.status.is_terminal:
                with job_context(str(owner.id)):
                    if await self._destroy_and_verify(
                        owner, reason="instance still exists after job ended"
                    ):
                        report.destroyed_orphans.append(inst.name)
        if report.destroyed_orphans:
            try:
                report.observed_instances = sorted(
                    i.name for i in await self.compute.list_instances()
                )
            except Exception:
                report.observed_instances = None
        return report

    # ------------------------------------------------------------------ recovery
    async def _recover(self, job: Job) -> None:
        log.warning("reconcile.recover", extra={"status": job.status.value})
        await self.repo.update_fields(
            job.id, worker_id=self.worker_id, heartbeat_at=datetime.now(UTC)
        )
        await self.repo.record_event(
            job.id,
            "reconciliation.recovering",
            f"Worker interruption detected in state {job.status.value}; running cleanup and destruction",
            {"previous_worker": job.worker_id},
            level="warning",
        )
        status = job.status
        if status is JobStatus.QUEUED or status.is_compute_phase:
            await self.repo.transition(
                job.id,
                JobStatus.CLEANING,
                "Reconciliation: cleaning up interrupted job",
                fields={"cleanup_started_at": datetime.now(UTC)},
            )
            status = JobStatus.CLEANING
        local_ok = self.workspace.delete(job.id)
        await self.repo.update_fields(
            job.id,
            document_state=DocumentState.CLEANED if local_ok else DocumentState.FAILED,
            cleanup_completed_at=datetime.now(UTC),
        )

        provisioned = job.instance_state is not InstanceState.NONE
        verified = True
        if provisioned:
            if status is JobStatus.CLEANING:
                await self.repo.transition(
                    job.id,
                    JobStatus.DESTROYING,
                    "Reconciliation: destroying instance",
                    fields={
                        "destruction_started_at": datetime.now(UTC),
                        "instance_state": InstanceState.DESTROYING,
                    },
                )
                status = JobStatus.DESTROYING
            # Idempotent: also re-issued when the job died while already verifying.
            await self._try_destroy(job.instance_name)
            if status is JobStatus.DESTROYING:
                await self.repo.transition(
                    job.id, JobStatus.VERIFYING_DESTRUCTION, "Reconciliation: verifying destruction"
                )
            verified = await self._verify(job)

        ok = local_ok and verified
        target = JobStatus.FAILED if ok else JobStatus.CLEANUP_FAILED
        await self.repo.transition(
            job.id,
            target,
            "Recovered after worker interruption; cleanup complete"
            + (", compute destroyed" if provisioned else "")
            if ok
            else "Recovered after worker interruption; cleanup INCOMPLETE",
            fields={
                "error_code": ErrorCode.WORKER_INTERRUPTED
                if ok
                else ErrorCode.DESTROY_VERIFICATION_FAILED,
                "error_message": "worker stopped while the job was running; recovered by reconciliation",
            },
        )

    async def _destroy_and_verify(self, job: Job, *, reason: str) -> bool:
        await self.repo.record_event(
            job.id,
            "reconciliation.destroy",
            f"Reconciliation: {reason}",
            {"instance": job.instance_name},
            level="warning",
        )
        await self._try_destroy(job.instance_name)
        return await self._verify(job)

    async def _verify(self, job: Job) -> bool:
        try:
            verified = await self.compute.verify_destroyed(
                job.instance_name, self.destroy_verify_timeout_s
            )
        except Exception:
            verified = False
        if verified:
            await self.repo.update_fields(
                job.id,
                instance_state=InstanceState.DESTROYED,
                destruction_completed_at=datetime.now(UTC),
            )
            await self.repo.record_event(
                job.id,
                "instance.destroyed",
                f"Instance {job.instance_name} confirmed absent (reconciliation)",
            )
        else:
            await self.repo.update_fields(job.id, instance_state=InstanceState.DESTROY_FAILED)
        return verified

    async def _try_destroy(self, name: str) -> None:
        for attempt in range(3):
            try:
                await asyncio.wait_for(
                    self.compute.destroy(name, self.destroy_timeout_s), self.destroy_timeout_s + 10
                )
                return
            except Exception as exc:
                log.warning(
                    "reconcile.destroy_failed",
                    extra={"instance": name, "error": type(exc).__name__},
                )
                await asyncio.sleep(2 ** (attempt + 1))

    async def _destroy_instance_only(self, name: str) -> None:
        await self._try_destroy(name)
