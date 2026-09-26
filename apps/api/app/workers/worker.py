"""Ephemera worker: claims queued jobs from PostgreSQL and runs their lifecycle.

* One job at a time per worker process; global limits (MAX_ACTIVE_JOBS,
  MAX_GPU_INSTANCES) are enforced atomically at claim time in the database.
* SIGTERM/SIGINT cancel the running job, which triggers the orchestrator's
  teardown (cleanup + destroy + verify) before the process exits. Give the
  container a generous stop grace period.
* Reconciliation runs at start-up and every RECONCILE_INTERVAL_SECONDS.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import socket
import time
import uuid
from datetime import UTC, datetime

from app.application.orchestrator import Orchestrator
from app.application.reconciliation import Reconciler
from app.core.config import Settings, get_settings
from app.core.container import (
    build_compute,
    build_database,
    build_documents,
    build_inference,
    build_workspace,
    orchestrator_config,
)
from app.core.logging import configure_logging, get_logger
from app.domain.errors import ConfigurationError, ErrorCode
from app.domain.status import DocumentState, JobStatus
from app.infrastructure.database.repository import PostgresJobRepository
from app.infrastructure.storage.workspace import JobWorkspace

log = get_logger("worker")


class Worker:
    def __init__(
        self,
        *,
        settings: Settings,
        repo: PostgresJobRepository,
        orchestrator: Orchestrator,
        reconciler: Reconciler,
        workspace: JobWorkspace,
        worker_id: str,
        compute_name: str,
    ) -> None:
        self.settings = settings
        self.repo = repo
        self.orchestrator = orchestrator
        self.reconciler = reconciler
        self.workspace = workspace
        self.worker_id = worker_id
        self.compute_name = compute_name
        self.started_at = datetime.now(UTC)
        self._stop = asyncio.Event()
        self._current: asyncio.Task[object] | None = None
        self._current_job_id: str | None = None
        self._last_block_reason: str | None = None

    def request_stop(self) -> None:
        if not self._stop.is_set():
            log.warning("worker.stop_requested", extra={"running_job": self._current_job_id})
            self._stop.set()
            if self._current is not None and not self._current.done():
                self._current.cancel()

    async def reconcile(self, *, startup: bool) -> None:
        busy = {self._current_job_id} if self._current_job_id else set()
        try:
            report = await self.reconciler.reconcile(startup=startup, busy_job_ids=busy)
            await self.repo.upsert_worker_heartbeat(
                worker_id=self.worker_id,
                mode=self.settings.ephemera_mode.value,
                compute_provider=self.compute_name,
                started_at=self.started_at,
                observed_instances=report.observed_instances,
                observation_error=report.observation_error,
                observed=True,
            )
            if (
                report.recovered_jobs
                or report.destroyed_orphans
                or report.unknown_instances
                or report.requeued_jobs
            ):
                log.warning(
                    "reconcile.report",
                    extra={
                        "recovered": report.recovered_jobs,
                        "requeued": report.requeued_jobs,
                        "destroyed": report.destroyed_orphans,
                        "unknown": report.unknown_instances,
                    },
                )
            for job in await self.repo.list_cancellable_unclaimed():
                await self._cancel_unclaimed(job.id)
            purged = await self.repo.purge_expired_results()
            if purged:
                log.info("results.purged", extra={"count": purged})
        except Exception as exc:
            log.error(
                "reconcile.failed", extra={"error": type(exc).__name__, "detail": str(exc)[:200]}
            )

    async def _cancel_unclaimed(self, job_id: uuid.UUID) -> None:
        """Finalize a job cancelled while still queued: no compute was ever requested."""
        self.workspace.delete(job_id)
        await self.repo.transition(
            job_id, JobStatus.CLEANING, "Cancelled before start; deleting upload"
        )
        await self.repo.transition(
            job_id,
            JobStatus.CANCELLED,
            "Cancelled before any compute was provisioned",
            fields={
                "error_code": ErrorCode.CANCELLED,
                "error_message": "cancelled by user",
                "document_state": DocumentState.CLEANED,
            },
        )

    async def _heartbeat_loop(self) -> None:
        while not self._stop.is_set():
            with contextlib.suppress(Exception):
                await self.repo.upsert_worker_heartbeat(
                    worker_id=self.worker_id,
                    mode=self.settings.ephemera_mode.value,
                    compute_provider=self.compute_name,
                    started_at=self.started_at,
                )
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.heartbeat_interval_seconds
                )

    async def run(self) -> None:
        log.info(
            "worker.start",
            extra={
                "worker_id": self.worker_id,
                "mode": self.settings.ephemera_mode.value,
                "compute": self.compute_name,
            },
        )
        self.workspace.ensure_root()
        await self.reconcile(startup=True)
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        last_reconcile = time.monotonic()
        try:
            while not self._stop.is_set():
                if time.monotonic() - last_reconcile >= self.settings.reconcile_interval_seconds:
                    await self.reconcile(startup=False)
                    last_reconcile = time.monotonic()
                job, blocked = await self.repo.claim_next(
                    self.worker_id, self.settings.max_active_jobs, self.settings.max_gpu_instances
                )
                if blocked and blocked != self._last_block_reason:
                    log.info("worker.capacity_blocked", extra={"reason": blocked})
                self._last_block_reason = blocked
                if job is None:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._stop.wait(), timeout=self.settings.worker_poll_interval_seconds
                        )
                    continue
                self._current_job_id = str(job.id)
                self._current = asyncio.create_task(self.orchestrator.run(job))
                try:
                    await self._current
                except asyncio.CancelledError:
                    log.warning("worker.job_interrupted", extra={"job_id": str(job.id)})
                except Exception as exc:  # orchestrator already tore down
                    log.error(
                        "worker.job_crashed",
                        extra={"job_id": str(job.id), "error": type(exc).__name__},
                    )
                finally:
                    self._current = None
                    self._current_job_id = None
                # Reconcile right after each job so the observed instance list is fresh for COMPUTE = 0.
                await self.reconcile(startup=False)
                last_reconcile = time.monotonic()
        finally:
            self._stop.set()
            heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            log.info("worker.stopped")


def default_worker_id() -> str:
    return os.environ.get("WORKER_ID") or socket.gethostname()


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.secret_values())
    try:
        settings.require_valid()
        compute = build_compute(settings)
        inference = build_inference(settings)
    except ConfigurationError as exc:
        log.error("worker.configuration_error", extra={"error": exc.message})
        raise SystemExit(2) from None

    db = build_database(settings)
    workspace = build_workspace(settings)
    worker_id = default_worker_id()
    orchestrator = Orchestrator(
        repo=db.repo,
        compute=compute,
        inference=inference,
        documents=build_documents(settings),
        workspace=workspace,
        config=orchestrator_config(settings),
        worker_id=worker_id,
    )
    reconciler = Reconciler(
        repo=db.repo,
        compute=compute,
        workspace=workspace,
        worker_id=worker_id,
        stale_job_seconds=settings.stale_job_seconds,
        destroy_timeout_s=settings.destroy_timeout_seconds,
        destroy_verify_timeout_s=settings.destroy_verify_timeout_seconds,
        delete_unknown_instances=settings.reconcile_delete_unknown_instances,
    )
    worker = Worker(
        settings=settings,
        repo=db.repo,
        orchestrator=orchestrator,
        reconciler=reconciler,
        workspace=workspace,
        worker_id=worker_id,
        compute_name=compute.name,
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.request_stop)
    try:
        await worker.run()
    finally:
        await db.engine.dispose()
