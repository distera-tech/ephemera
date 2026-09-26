"""The Ephemera job lifecycle.

    PROVISION → BOOTSTRAP → LOAD MODEL → TRANSFER → INFER → CLEAN → DESTROY → VERIFY

CRITICAL INVARIANT: once ``provision()`` has been *called* for a job, Ephemera
attempts to destroy that instance and verify its absence no matter how the job
ends — success, error, timeout, cancellation, or worker shutdown. That is
implemented by ``run()``: ``_execute()`` is the happy path, ``_teardown()``
always runs afterwards, is shielded from cancellation, and treats every one of
its own steps as best-effort so a failure in one (e.g. the database) can never
skip destruction. Destruction is attempted even if provisioning *failed*,
because a failed or timed-out ``brev create`` can still leave an instance
behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar

from app.application.prompts import build_inference_request
from app.application.result_parser import InvalidModelOutput, parse_analysis
from app.core.logging import get_logger, job_context
from app.domain.analysis import DocumentAnalysis
from app.domain.errors import EphemeraError, ErrorCode, InferenceError
from app.domain.models import (
    CleanupResult,
    ExtractedDocument,
    GpuRequirements,
    GpuSelection,
    Job,
)
from app.domain.ports import ComputeProvider, DocumentProcessor, InferenceProvider
from app.domain.status import DocumentState, InstanceState, JobStatus
from app.infrastructure.database.repository import PostgresJobRepository
from app.infrastructure.inference.runtime import BoundExecutor, JobRuntimeContext
from app.infrastructure.storage.workspace import JobWorkspace

log = get_logger("orchestrator")
T = TypeVar("T")


def now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class OrchestratorConfig:
    model_id: str
    gpu_requirements: GpuRequirements
    remote_data_dir: str
    max_job_runtime_s: float
    max_provisioning_s: float
    bootstrap_timeout_s: float
    model_ready_timeout_s: float
    transfer_timeout_s: float
    inference_timeout_s: float
    cleanup_timeout_s: float
    destroy_timeout_s: float
    destroy_verify_timeout_s: float
    max_output_tokens: int
    result_retention_s: int
    heartbeat_interval_s: float
    force_inference_failure: bool = False
    destroy_attempts: int = 3


class JobCancelled(Exception):
    pass


@dataclass(slots=True)
class JobRun:
    """Mutable facts about one execution, used by teardown to decide what to undo."""

    job: Job
    provision_attempted: bool = False
    instance_reached: bool = False  # SSH-reachable; remote cleanup is meaningful
    error_code: ErrorCode | None = None
    error_message: str | None = None
    cancelled: bool = False
    interrupted: bool = False
    cleanup: CleanupResult = field(default_factory=CleanupResult)
    runtime: JobRuntimeContext | None = None

    def fail(self, code: ErrorCode, message: str) -> None:
        if self.error_code is None:  # keep the first (root-cause) failure
            self.error_code = code
            self.error_message = message


class Orchestrator:
    def __init__(
        self,
        *,
        repo: PostgresJobRepository,
        compute: ComputeProvider,
        inference: InferenceProvider,
        documents: DocumentProcessor,
        workspace: JobWorkspace,
        config: OrchestratorConfig,
        worker_id: str,
    ) -> None:
        self.repo = repo
        self.compute = compute
        self.inference = inference
        self.documents = documents
        self.workspace = workspace
        self.cfg = config
        self.worker_id = worker_id

    # ================================================================== entry point
    async def run(self, job: Job) -> JobStatus:
        run = JobRun(job=job)
        with job_context(str(job.id)):
            log.info(
                "job.start", extra={"mode": "simulation" if self.compute.is_simulated else "real"}
            )
            execute = asyncio.create_task(self._execute(run), name=f"execute-{job.id}")
            monitor = asyncio.create_task(self._monitor(run, execute), name=f"monitor-{job.id}")
            current = asyncio.current_task()
            try:
                await asyncio.wait_for(asyncio.shield(execute), timeout=self.cfg.max_job_runtime_s)
            except TimeoutError:
                execute.cancel()
                await asyncio.gather(execute, return_exceptions=True)
                run.fail(
                    ErrorCode.JOB_TIMEOUT,
                    f"job exceeded MAX_JOB_RUNTIME_SECONDS={self.cfg.max_job_runtime_s:.0f}",
                )
            except asyncio.CancelledError:
                if current is not None and current.cancelling():
                    # Worker shutdown: stop the workload, then still tear down.
                    run.interrupted = True
                    execute.cancel()
                    await asyncio.gather(execute, return_exceptions=True)
                    run.fail(ErrorCode.WORKER_INTERRUPTED, "worker shut down while job was running")
                elif run.cancelled:
                    run.fail(ErrorCode.CANCELLED, "cancelled by user")
                else:
                    run.fail(ErrorCode.INTERNAL_ERROR, "job task cancelled unexpectedly")
            except JobCancelled:
                run.cancelled = True
                run.fail(ErrorCode.CANCELLED, "cancelled by user")
            except EphemeraError as exc:
                run.fail(exc.code, exc.message)
            except Exception as exc:  # never let an unexpected error skip teardown
                log.exception("job.unexpected_error")
                run.fail(ErrorCode.INTERNAL_ERROR, f"unexpected {type(exc).__name__}")
            finally:
                monitor.cancel()
                await asyncio.gather(monitor, return_exceptions=True)
                if run.error_code is not None:
                    log.warning("job.failed", extra={"error_code": run.error_code.value})
                    await self._safe(
                        self.repo.record_event(
                            job.id,
                            "job.error",
                            run.error_message or run.error_code.value,
                            {"error_code": run.error_code.value},
                            level="error",
                        ),
                        "record error event",
                    )
                final = await self._shielded_teardown(run)
            if run.interrupted:
                raise asyncio.CancelledError
            return final

    async def _monitor(self, run: JobRun, execute: asyncio.Task[None]) -> None:
        """Heartbeat + cooperative cancellation for long-running steps."""
        while not execute.done():
            await asyncio.sleep(self.cfg.heartbeat_interval_s)
            with contextlib.suppress(Exception):
                await self.repo.heartbeat(run.job.id, self.worker_id)
                if not run.cancelled and await self.repo.is_cancel_requested(run.job.id):
                    run.cancelled = True
                    log.info("job.cancel_observed")
                    execute.cancel()

    async def _shielded_teardown(self, run: JobRun) -> JobStatus:
        task = asyncio.ensure_future(self._teardown(run))
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # A second shutdown signal must not abort destruction of a live GPU.
                run.interrupted = True
                log.warning("teardown.cancel_ignored")
                if task.done():
                    return task.result()

    # ================================================================== happy path
    async def _execute(self, run: JobRun) -> None:
        job = run.job
        job_dir = self.workspace.dir_for(job.id)

        # --- 0. Validate + extract the document BEFORE any compute exists ------------
        await self.repo.update_fields(job.id, document_state=DocumentState.PROCESSING)
        document = await self.documents.extract(job_dir / "document.pdf", job_dir)
        await self.repo.update_fields(
            job.id, page_count=document.page_count, input_truncated=document.truncated
        )
        await self.repo.record_event(
            job.id,
            "document.validated",
            f"PDF validated: {document.page_count} page(s), {document.char_count} characters extracted"
            + (" (truncated to fit context)" if document.truncated else ""),
            {
                "pages": document.page_count,
                "chars": document.char_count,
                "truncated": document.truncated,
            },
        )
        await self._checkpoint(run)

        # --- 1. PROVISIONING --------------------------------------------------------
        prov_started = time.monotonic()
        await self.repo.transition(
            job.id,
            JobStatus.PROVISIONING,
            "Requesting on-demand GPU",
            fields={
                "provisioning_started_at": now(),
                "requested_gpu": self.cfg.gpu_requirements.preferred_gpu,
            },
        )
        selection = await self.compute.select_gpu(self.cfg.gpu_requirements)
        await self._record_selection(run, selection)
        await self.repo.update_fields(job.id, instance_state=InstanceState.PROVISIONING)
        run.provision_attempted = True  # from here on, teardown MUST destroy
        info = await self.compute.provision(
            job.instance_name, selection, self.cfg.max_provisioning_s
        )
        remaining = max(30.0, self.cfg.max_provisioning_s - (time.monotonic() - prov_started))
        info = await self.compute.wait_until_ready(job.instance_name, remaining)
        run.instance_reached = True
        offer = selection.offer_for(info.instance_type) or selection.candidates[0]
        await self.repo.update_fields(
            job.id,
            instance_state=InstanceState.ACTIVE,
            instance_type=info.instance_type or offer.instance_type,
            actual_gpu=info.gpu_name or offer.gpu_name,
            gpu_vram_mb=int(offer.vram_per_gpu_gb * 1024) if offer.vram_per_gpu_gb else None,
            compute_provider=offer.provider,
            price_per_hour=offer.price_per_hour,
        )
        await self.repo.record_event(
            job.id,
            "instance.active",
            f"GPU instance {job.instance_name} is running",
            {
                "instance": job.instance_name,
                "instance_type": info.instance_type,
                "gpu": info.gpu_name,
                "provisioning_seconds": round(time.monotonic() - prov_started, 1),
            },
        )
        await self._checkpoint(run)

        run.runtime = JobRuntimeContext(
            job_id=job.id,
            executor=BoundExecutor(self.compute, job.instance_name),
            local_dir=job_dir,
            remote_dir=f"{self.cfg.remote_data_dir}/{job.id}",
        )

        # --- 2. BOOTSTRAPPING -------------------------------------------------------
        await self.repo.transition(
            job.id,
            JobStatus.BOOTSTRAPPING,
            "Verifying GPU, container runtime and job directory",
            fields={"provisioning_completed_at": now()},
        )
        report = await self._timed(
            self.inference.bootstrap(run.runtime, self.cfg.bootstrap_timeout_s),
            self.cfg.bootstrap_timeout_s,
            ErrorCode.BOOTSTRAP_TIMEOUT,
            "bootstrap",
        )
        gpu_fields: dict[str, Any] = {}
        if report.gpu_name:
            gpu_fields["actual_gpu"] = report.gpu_name
        if report.vram_mb:
            gpu_fields["gpu_vram_mb"] = report.vram_mb
        await self.repo.update_fields(job.id, **gpu_fields)
        await self.repo.record_event(
            job.id,
            "gpu.verified",
            f"GPU verified: {report.gpu_name or 'unknown'}"
            + (f" ({report.vram_mb} MiB)" if report.vram_mb else ""),
            {"gpu": report.gpu_name, "vram_mb": report.vram_mb, "driver": report.driver_version},
        )
        await self._checkpoint(run)

        # --- 3. MODEL_LOADING → READY -----------------------------------------------
        await self.repo.transition(
            job.id,
            JobStatus.MODEL_LOADING,
            f"Starting model server ({self.inference.name}) with {job.model_id}",
            fields={"model_loading_started_at": now()},
        )
        load_started = time.monotonic()
        await self._timed(
            self.inference.start(run.runtime, self.cfg.model_ready_timeout_s),
            self.cfg.model_ready_timeout_s,
            ErrorCode.MODEL_STARTUP_TIMEOUT,
            "model start",
        )
        remaining = max(30.0, self.cfg.model_ready_timeout_s - (time.monotonic() - load_started))
        await self._timed(
            self.inference.wait_ready(run.runtime, remaining),
            remaining + 30,
            ErrorCode.MODEL_STARTUP_TIMEOUT,
            "model readiness",
        )
        await self.repo.transition(
            job.id,
            JobStatus.READY,
            "Simulated model runtime ready"
            if self.inference.is_simulated
            else "Model server healthy (localhost-only endpoint)",
            fields={"model_ready_at": now()},
            metadata={"model_loading_seconds": round(time.monotonic() - load_started, 1)},
        )
        await self._checkpoint(run)

        # --- 4. TRANSFERRING --------------------------------------------------------
        await self.repo.transition(
            job.id, JobStatus.TRANSFERRING, "Transferring document payload to the instance"
        )
        request = build_inference_request(
            job_id=job.id,
            model_id=job.model_id,
            document=document,
            analysis_type=job.analysis_type,
            max_tokens=self.cfg.max_output_tokens,
        )
        await self._timed(
            self.inference.transfer(run.runtime, request, self.cfg.transfer_timeout_s),
            self.cfg.transfer_timeout_s + 10,
            ErrorCode.TRANSFER_FAILED,
            "transfer",
        )
        await self.repo.update_fields(job.id, document_state=DocumentState.TRANSFERRED)
        await self._checkpoint(run)

        # --- 5. INFERENCING ---------------------------------------------------------
        await self.repo.transition(
            job.id,
            JobStatus.INFERENCING,
            "Running inference",
            fields={"inference_started_at": now()},
        )
        if self.cfg.force_inference_failure or job.force_inference_failure:
            raise InferenceError(
                "Demo failure injected during inference (DEMO_FORCE_INFERENCE_FAILURE)",
                code=ErrorCode.INFERENCE_FORCED_FAILURE,
            )
        analysis = await self._infer_validated(run, document)
        expires = now() + timedelta(seconds=max(60, self.cfg.result_retention_s))
        await self.repo.save_result(job.id, analysis, expires)
        await self.repo.update_fields(
            job.id, inference_completed_at=now(), document_state=DocumentState.PROCESSED
        )
        await self.repo.record_event(
            job.id,
            "inference.completed",
            "Structured result validated and available",
            {
                "risks": len(analysis.potential_risks),
                "key_points": len(analysis.key_points),
                "requires_human_review": analysis.requires_human_review,
            },
        )

    async def _infer_validated(self, run: JobRun, document: ExtractedDocument) -> DocumentAnalysis:
        assert run.runtime is not None
        raw = await self._timed(
            self.inference.infer(run.runtime, self.cfg.inference_timeout_s),
            self.cfg.inference_timeout_s + 15,
            ErrorCode.INFERENCE_TIMEOUT,
            "inference",
        )
        try:
            return parse_analysis(raw)
        except InvalidModelOutput as exc:
            await self.repo.record_event(
                run.job.id,
                "inference.output_invalid",
                f"Model output rejected ({exc}); retrying once with stricter formatting",
                level="warning",
            )
        retry = build_inference_request(
            job_id=run.job.id,
            model_id=run.job.model_id,
            document=document,
            analysis_type=run.job.analysis_type,
            max_tokens=self.cfg.max_output_tokens,
            retry=True,
        )
        await self.inference.transfer(run.runtime, retry, self.cfg.transfer_timeout_s)
        raw = await self._timed(
            self.inference.infer(run.runtime, self.cfg.inference_timeout_s),
            self.cfg.inference_timeout_s + 15,
            ErrorCode.INFERENCE_TIMEOUT,
            "inference retry",
        )
        try:
            return parse_analysis(raw)
        except InvalidModelOutput as exc:
            raise InferenceError(str(exc), code=ErrorCode.INFERENCE_OUTPUT_INVALID) from None

    # ================================================================== teardown
    async def _teardown(self, run: JobRun) -> JobStatus:
        job = run.job
        cleanup = run.cleanup
        await self._safe_transition(
            job.id,
            JobStatus.CLEANING,
            "Cleaning temporary application data",
            fields={"cleanup_started_at": now()},
        )

        # 1. Remote job data (only meaningful if the instance became reachable).
        if run.instance_reached and run.runtime is not None:
            try:
                await asyncio.wait_for(
                    self.inference.cleanup(run.runtime, self.cfg.cleanup_timeout_s),
                    self.cfg.cleanup_timeout_s + 10,
                )
                cleanup.remote_deleted = True
            except Exception as exc:
                cleanup.remote_deleted = False
                cleanup.errors.append(f"remote cleanup: {_describe(exc)}")
                await self._safe(
                    self.repo.record_event(
                        job.id,
                        "cleanup.remote_failed",
                        "Remote data cleanup failed; relying on instance destruction",
                        level="warning",
                    ),
                    "event",
                )
            else:
                await self._safe(
                    self.repo.record_event(
                        job.id, "cleanup.remote", "Remote job directory and model container removed"
                    ),
                    "event",
                )

        # 2. Local job directory (uploaded PDF + any staged payloads).
        try:
            cleanup.local_deleted = self.workspace.delete(job.id)
        except Exception as exc:
            cleanup.local_deleted = False
            cleanup.errors.append(f"local cleanup: {_describe(exc)}")
        await self._safe(
            self.repo.update_fields(
                job.id,
                cleanup_completed_at=now(),
                document_state=DocumentState.CLEANED
                if cleanup.local_deleted
                else DocumentState.FAILED,
            ),
            "update",
        )
        await self._safe(
            self.repo.record_event(
                job.id,
                "cleanup.local",
                "Local temporary files deleted"
                if cleanup.local_deleted
                else "Local temporary file deletion FAILED",
                level="info" if cleanup.local_deleted else "error",
            ),
            "event",
        )

        # 3. Destroy + verify — mandatory whenever provisioning was attempted.
        if run.provision_attempted:
            await self._destroy_and_verify(run)

        return await self._finalize(run)

    async def _destroy_and_verify(self, run: JobRun) -> None:
        job, cleanup = run.job, run.cleanup
        cleanup.destroy_attempted = True
        await self._safe_transition(
            job.id,
            JobStatus.DESTROYING,
            f"Destroying GPU instance {job.instance_name}",
            fields={"destruction_started_at": now(), "instance_state": InstanceState.DESTROYING},
        )
        for attempt in range(1, self.cfg.destroy_attempts + 1):
            try:
                await asyncio.wait_for(
                    self.compute.destroy(job.instance_name, self.cfg.destroy_timeout_s),
                    self.cfg.destroy_timeout_s + 10,
                )
                cleanup.destroyed = True
                break
            except Exception as exc:
                cleanup.destroyed = False
                cleanup.errors.append(f"destroy attempt {attempt}: {_describe(exc)}")
                log.warning(
                    "destroy.attempt_failed", extra={"attempt": attempt, "error": _describe(exc)}
                )
                await asyncio.sleep(min(30, 2**attempt))

        await self._safe_transition(
            job.id, JobStatus.VERIFYING_DESTRUCTION, "Verifying the instance no longer exists"
        )
        try:
            verified = await asyncio.wait_for(
                self.compute.verify_destroyed(job.instance_name, self.cfg.destroy_verify_timeout_s),
                self.cfg.destroy_verify_timeout_s + 30,
            )
        except Exception as exc:
            verified = False
            cleanup.errors.append(f"verify: {_describe(exc)}")
        cleanup.destruction_verified = verified
        if verified:
            await self._safe(
                self.repo.update_fields(
                    job.id, instance_state=InstanceState.DESTROYED, destruction_completed_at=now()
                ),
                "update",
            )
            await self._safe(
                self.repo.record_event(
                    job.id,
                    "instance.destroyed",
                    f"Instance {job.instance_name} confirmed absent from provider — COMPUTE = 0 for this job",
                    {"verified_by": self.compute.name},
                ),
                "event",
            )
        else:
            await self._safe(
                self.repo.update_fields(job.id, instance_state=InstanceState.DESTROY_FAILED),
                "update",
            )
            await self._safe(
                self.repo.record_event(
                    job.id,
                    "instance.destroy_unverified",
                    f"Could not verify destruction of {job.instance_name}; reconciliation will retry",
                    level="error",
                ),
                "event",
            )

    async def _finalize(self, run: JobRun) -> JobStatus:
        job, cleanup = run.job, run.cleanup
        report = cleanup.as_dict()
        fields: dict[str, Any]
        target: JobStatus
        if not cleanup.succeeded:
            code = (
                ErrorCode.DESTROY_VERIFICATION_FAILED
                if cleanup.destroy_attempted and not cleanup.destruction_verified
                else ErrorCode.CLEANUP_FAILED
            )
            original = f"; original failure: {run.error_code.value}" if run.error_code else ""
            target, fields = (
                JobStatus.CLEANUP_FAILED,
                {
                    "error_code": code,
                    "error_message": f"{'; '.join(cleanup.errors)[:900]}{original}",
                },
            )
            message = "Cleanup did not complete — manual attention may be required"
        elif run.cancelled and run.error_code in (None, ErrorCode.CANCELLED):
            target, fields = (
                JobStatus.CANCELLED,
                {"error_code": ErrorCode.CANCELLED, "error_message": "cancelled by user"},
            )
            message = "Job cancelled; cleanup complete" + (
                ", compute destroyed" if cleanup.destroy_attempted else ""
            )
        elif run.error_code is not None:
            target, fields = (
                JobStatus.FAILED,
                {"error_code": run.error_code, "error_message": run.error_message},
            )
            message = f"Job failed ({run.error_code.value}); cleanup successful" + (
                ", GPU destroyed — COMPUTE = 0"
                if cleanup.destroy_attempted
                else ", no GPU was provisioned"
            )
        else:
            target, fields = JobStatus.COMPLETED, {}
            message = "Job completed. GPU destroyed and verified — COMPUTE = 0"
        fields["cleanup_report"] = report
        await self._safe_transition(
            job.id, target, message, fields=fields, metadata={"cleanup": report}
        )
        log.info("job.finished", extra={"status": target.value, "cleanup_ok": cleanup.succeeded})
        return target

    # ================================================================== helpers
    async def _checkpoint(self, run: JobRun) -> None:
        if run.cancelled or await self.repo.is_cancel_requested(run.job.id):
            run.cancelled = True
            raise JobCancelled

    async def _record_selection(self, run: JobRun, selection: GpuSelection) -> None:
        await self.repo.record_event(
            run.job.id,
            "gpu.selected",
            "GPU candidates: "
            + ", ".join(f"{c.gpu_name} ({c.instance_type})" for c in selection.candidates),
            {
                "requested": selection.requested_gpu,
                "candidates": [
                    {
                        "instance_type": c.instance_type,
                        "gpu": c.gpu_name,
                        "gpu_count": c.gpu_count,
                        "vram_gb": c.vram_per_gpu_gb,
                        "price_per_hour": c.price_per_hour,
                        "provider": c.provider,
                    }
                    for c in selection.candidates
                ],
            },
        )

    @staticmethod
    async def _timed(aw: Awaitable[T], timeout_s: float, code: ErrorCode, what: str) -> T:
        try:
            return await asyncio.wait_for(aw, timeout=timeout_s)
        except TimeoutError:
            raise EphemeraError(f"{what} exceeded {timeout_s:.0f}s", code=code) from None

    async def _safe_transition(
        self,
        job_id: uuid.UUID,
        target: JobStatus,
        message: str,
        *,
        fields: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await self._safe(
            self.repo.transition(job_id, target, message, fields=fields, metadata=metadata),
            f"transition to {target}",
        )

    @staticmethod
    async def _safe(aw: Awaitable[Any], what: str) -> None:
        """Best-effort persistence during teardown: log and continue, never abort teardown."""
        try:
            await aw
        except Exception as exc:
            log.error("teardown.persist_failed", extra={"step": what, "error": _describe(exc)})


def _describe(exc: BaseException) -> str:
    if isinstance(exc, EphemeraError):
        return f"{exc.code.value}: {exc.message}"[:300]
    if isinstance(exc, TimeoutError):
        return "timeout"
    return type(exc).__name__


RunCallable = Callable[[Job], Awaitable[JobStatus]]
