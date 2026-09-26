"""Ports (hexagonal architecture). Adapters in ``app.infrastructure`` implement these."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from app.domain.analysis import DocumentAnalysis
from app.domain.models import (
    BootstrapReport,
    ExecResult,
    ExtractedDocument,
    GpuRequirements,
    GpuSelection,
    InferenceRequest,
    InstanceInfo,
    Job,
)
from app.domain.status import JobStatus


@runtime_checkable
class ComputeProvider(Protocol):
    """Provisions and destroys GPU instances. Implemented by Brev and the simulator."""

    name: str
    is_simulated: bool

    async def select_gpu(self, requirements: GpuRequirements) -> GpuSelection: ...

    async def provision(self, name: str, selection: GpuSelection, timeout_s: float) -> InstanceInfo:
        """Create the instance. May raise after partially creating it; callers must
        always call :meth:`destroy` for ``name`` once this was invoked."""
        ...

    async def wait_until_ready(self, name: str, timeout_s: float) -> InstanceInfo: ...

    async def execute(self, name: str, command: str, timeout_s: float) -> ExecResult:
        """Run a *fixed, trusted* command on the instance. Never pass user data."""
        ...

    async def upload(
        self, name: str, local_path: Path, remote_path: str, timeout_s: float
    ) -> None: ...

    async def download(
        self, name: str, remote_path: str, local_path: Path, timeout_s: float
    ) -> None: ...

    async def destroy(self, name: str, timeout_s: float) -> None: ...

    async def get_instance(self, name: str) -> InstanceInfo | None: ...

    async def verify_destroyed(self, name: str, timeout_s: float) -> bool: ...

    async def list_instances(self) -> list[InstanceInfo]:
        """Only instances whose name matches Ephemera's naming scheme."""
        ...


class RemoteExecutor(Protocol):
    """A compute instance, bound by name, as seen by an inference runtime."""

    @property
    def instance_name(self) -> str: ...

    async def execute(self, command: str, timeout_s: float) -> ExecResult: ...

    async def upload(self, local_path: Path, remote_path: str, timeout_s: float) -> None: ...

    async def download(self, remote_path: str, local_path: Path, timeout_s: float) -> None: ...


class RuntimeContext(Protocol):
    @property
    def job_id(self) -> uuid.UUID: ...

    @property
    def executor(self) -> RemoteExecutor: ...

    @property
    def local_dir(self) -> Path: ...

    @property
    def remote_dir(self) -> str: ...


class InferenceProvider(Protocol):
    """Model-serving runtime on an ephemeral instance (vLLM today, NIM later)."""

    name: str
    is_simulated: bool

    async def bootstrap(self, ctx: RuntimeContext, timeout_s: float) -> BootstrapReport: ...

    async def start(self, ctx: RuntimeContext, timeout_s: float) -> None: ...

    async def wait_ready(self, ctx: RuntimeContext, timeout_s: float) -> None: ...

    async def transfer(
        self, ctx: RuntimeContext, request: InferenceRequest, timeout_s: float
    ) -> None:
        """Move the request payload (contains document text) into the isolated environment."""
        ...

    async def infer(self, ctx: RuntimeContext, timeout_s: float) -> str:
        """Run the staged request; return the raw assistant content (validated by the caller).
        Implementations delete the staged payload from the instance afterwards."""
        ...

    async def cleanup(self, ctx: RuntimeContext, timeout_s: float) -> None:
        """Remove job data from the instance and stop the model server. Idempotent."""
        ...


class DocumentProcessor(Protocol):
    async def extract(self, pdf_path: Path, work_dir: Path) -> ExtractedDocument: ...


class JobRepository(Protocol):
    async def get(self, job_id: uuid.UUID) -> Job | None: ...

    async def transition(
        self,
        job_id: uuid.UUID,
        target: JobStatus,
        message: str,
        *,
        metadata: dict[str, Any] | None = None,
        fields: dict[str, Any] | None = None,
    ) -> Job:
        """Validate + apply a state transition, update fields and append an event atomically."""
        ...

    async def update_fields(self, job_id: uuid.UUID, **fields: Any) -> None: ...

    async def is_cancel_requested(self, job_id: uuid.UUID) -> bool: ...

    async def heartbeat(self, job_id: uuid.UUID, worker_id: str) -> None: ...

    async def save_result(
        self, job_id: uuid.UUID, analysis: DocumentAnalysis, expires_at: datetime | None
    ) -> None: ...

    async def list_stale_in_flight(self, stale_before: datetime) -> Sequence[Job]: ...


class EventRepository(Protocol):
    async def record(
        self,
        job_id: uuid.UUID,
        event_type: str,
        message: str,
        metadata: dict[str, Any] | None = None,
        level: str = "info",
    ) -> None: ...
