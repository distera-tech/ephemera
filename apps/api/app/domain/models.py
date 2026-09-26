"""Domain value objects. No knowledge of Brev, FastAPI, SQLAlchemy or subprocess."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any

from app.domain.status import DocumentState, InstanceState, JobStatus

INSTANCE_PREFIX = "ephemera-"


class AnalysisType(StrEnum):
    GENERAL = "general"
    CONTRACT = "contract"
    RISK = "risk"


def instance_name_for(job_id: uuid.UUID) -> str:
    """Deterministic, non-sensitive instance name: ``ephemera-<first 12 hex of job id>``.

    Never derived from the filename or any user input, so it is safe as a CLI
    argument and makes orphan reconciliation possible.
    """
    return f"{INSTANCE_PREFIX}{job_id.hex[:12]}"


def is_ephemera_instance_name(name: str) -> bool:
    suffix = name.removeprefix(INSTANCE_PREFIX)
    return (
        name.startswith(INSTANCE_PREFIX)
        and len(suffix) == 12
        and all(c in "0123456789abcdef" for c in suffix)
    )


@dataclass(frozen=True, slots=True)
class GpuRequirements:
    preferred_gpu: str
    min_vram_gb: float
    fallback_gpus: tuple[str, ...] = ()
    min_compute_capability: float | None = None
    min_disk_gb: float | None = None
    max_gpu_count: int = 1
    max_candidates: int = 5
    explicit_instance_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GpuOffer:
    """One provisionable instance type as reported by the compute provider."""

    instance_type: str
    gpu_name: str
    gpu_count: int
    vram_per_gpu_gb: float
    provider: str | None = None
    compute_capability: float | None = None
    price_per_hour: float | None = None
    boot_time_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class GpuSelection:
    requested_gpu: str
    candidates: tuple[GpuOffer, ...]

    def offer_for(self, instance_type: str | None) -> GpuOffer | None:
        return next((c for c in self.candidates if c.instance_type == instance_type), None)


@dataclass(frozen=True, slots=True)
class InstanceInfo:
    name: str
    status: str
    instance_id: str | None = None
    instance_type: str | None = None
    gpu_name: str | None = None
    shell_ready: bool = False

    @property
    def is_running(self) -> bool:
        return self.status.upper() == "RUNNING"

    @property
    def is_failed(self) -> bool:
        return self.status.upper() == "FAILURE"


@dataclass(frozen=True, slots=True)
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


@dataclass(frozen=True, slots=True)
class BootstrapReport:
    gpu_name: str | None
    vram_mb: int | None
    driver_version: str | None
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class InferenceRequest:
    """A fully-built chat request. Holds document text: never log or persist it."""

    job_id: uuid.UUID
    model_id: str
    system_prompt: str
    user_prompt: str
    json_schema: dict[str, Any]
    max_tokens: int
    temperature: float = 0.0

    def __repr__(self) -> str:  # never leak prompts through repr / tracebacks
        return f"InferenceRequest(job_id={self.job_id}, model_id={self.model_id!r}, <redacted>)"


@dataclass(frozen=True, slots=True)
class ExtractedDocument:
    """Text extracted from an uploaded document. Never logged or persisted."""

    text: str
    page_count: int
    char_count: int
    truncated: bool

    def __repr__(self) -> str:
        return (
            f"ExtractedDocument(pages={self.page_count}, chars={self.char_count}, "
            f"truncated={self.truncated}, text=<redacted>)"
        )


@dataclass(slots=True)
class CleanupResult:
    local_deleted: bool = False
    remote_deleted: bool | None = None  # None = not applicable (no instance reached)
    destroy_attempted: bool = False
    destroyed: bool | None = None
    destruction_verified: bool | None = None
    errors: list[str] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        """Cleanup is successful when local data is gone and — if compute was
        requested — its destruction has been verified.

        A failed *remote* data wipe is recorded but not fatal when the instance
        (including its disk) was subsequently destroyed and verified gone.
        """
        if not self.local_deleted:
            return False
        if self.destroy_attempted:
            return bool(self.destroyed and self.destruction_verified)
        return True

    def as_dict(self) -> dict[str, Any]:
        return {
            "local_deleted": self.local_deleted,
            "remote_deleted": self.remote_deleted,
            "destroy_attempted": self.destroy_attempted,
            "destroyed": self.destroyed,
            "destruction_verified": self.destruction_verified,
            "errors": list(self.errors),
            "succeeded": self.succeeded,
        }


@dataclass(slots=True)
class Job:
    id: uuid.UUID
    status: JobStatus
    filename: str
    content_type: str
    file_size: int
    sha256: str
    analysis_type: AnalysisType
    mode: str
    model_id: str
    requested_gpu: str | None
    instance_name: str
    instance_state: InstanceState
    document_state: DocumentState
    force_inference_failure: bool
    cancel_requested: bool
    created_at: datetime
    actual_gpu: str | None = None
    gpu_vram_mb: int | None = None
    instance_type: str | None = None
    compute_provider: str | None = None
    price_per_hour: float | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    provisioning_started_at: datetime | None = None
    provisioning_completed_at: datetime | None = None
    model_loading_started_at: datetime | None = None
    model_ready_at: datetime | None = None
    inference_started_at: datetime | None = None
    inference_completed_at: datetime | None = None
    cleanup_started_at: datetime | None = None
    cleanup_completed_at: datetime | None = None
    destruction_started_at: datetime | None = None
    destruction_completed_at: datetime | None = None
    cleanup_report: dict[str, Any] | None = None
    worker_id: str | None = None
    heartbeat_at: datetime | None = None
    page_count: int | None = None
    input_truncated: bool = False
    owner_id: str = "demo"
