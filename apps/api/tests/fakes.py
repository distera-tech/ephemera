"""Instrumented fakes for orchestrator tests: record every call, fail on demand."""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.application.orchestrator import Orchestrator, OrchestratorConfig
from app.domain.errors import ComputeError, ErrorCode, InferenceError
from app.domain.models import (
    AnalysisType,
    BootstrapReport,
    ExecResult,
    GpuOffer,
    GpuRequirements,
    GpuSelection,
    InferenceRequest,
    InstanceInfo,
    instance_name_for,
)
from app.domain.ports import RuntimeContext
from app.infrastructure.database.repository import PostgresJobRepository
from app.infrastructure.documents.pymupdf_processor import PyMuPDFDocumentProcessor
from app.infrastructure.storage.workspace import DOCUMENT_FILENAME, JobWorkspace

VALID = {
    "document_type": "Contract",
    "summary": "Synthetic summary",
    "key_points": ["one"],
    "potential_risks": [],
    "entities": [],
    "requires_human_review": True,
}


@dataclass
class FakeCompute:
    name: str = "fake"
    is_simulated: bool = True
    fail_at: set[str] = field(default_factory=set)
    instances: dict[str, InstanceInfo] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)
    hang_provision: bool = False

    async def select_gpu(self, requirements: GpuRequirements) -> GpuSelection:
        self.calls.append("select_gpu")
        if "select" in self.fail_at:
            raise ComputeError("no gpu", code=ErrorCode.NO_GPU_AVAILABLE)
        return GpuSelection(
            requirements.preferred_gpu, (GpuOffer("l40s.1x", "L40S", 1, 48, "fake", 8.9, 1.5, 60),)
        )

    async def provision(self, name: str, selection: GpuSelection, timeout_s: float) -> InstanceInfo:
        self.calls.append("provision")
        if "provision_partial" in self.fail_at:
            self.instances[name] = InstanceInfo(name, "DEPLOYING")
            raise ComputeError(
                "create failed after partial creation", code=ErrorCode.PROVISIONING_FAILED
            )
        if "provision" in self.fail_at:
            raise ComputeError("no capacity", code=ErrorCode.PROVISIONING_FAILED)
        if self.hang_provision:
            self.instances[name] = InstanceInfo(name, "DEPLOYING")
            await asyncio.sleep(3600)
        info = InstanceInfo(name, "RUNNING", "id", "l40s.1x", "L40S", True)
        self.instances[name] = info
        return info

    async def wait_until_ready(self, name: str, timeout_s: float) -> InstanceInfo:
        self.calls.append("wait_until_ready")
        return self.instances[name]

    async def execute(self, name: str, command: str, timeout_s: float) -> ExecResult:
        return ExecResult(0, "", "", 1)

    async def upload(self, name: str, local_path: Path, remote_path: str, timeout_s: float) -> None:
        return None

    async def download(
        self, name: str, remote_path: str, local_path: Path, timeout_s: float
    ) -> None:
        return None

    async def destroy(self, name: str, timeout_s: float) -> None:
        self.calls.append("destroy")
        if "destroy" in self.fail_at:
            raise ComputeError("delete failed", code=ErrorCode.DESTROY_FAILED)
        self.instances.pop(name, None)

    async def get_instance(self, name: str) -> InstanceInfo | None:
        return self.instances.get(name)

    async def verify_destroyed(self, name: str, timeout_s: float) -> bool:
        self.calls.append("verify_destroyed")
        return name not in self.instances

    async def list_instances(self) -> list[InstanceInfo]:
        return list(self.instances.values())


@dataclass
class FakeInference:
    name: str = "fake-llm"
    is_simulated: bool = True
    fail_at: set[str] = field(default_factory=set)
    outputs: list[str] = field(default_factory=lambda: [json.dumps(VALID)])
    calls: list[str] = field(default_factory=list)
    requests: list[InferenceRequest] = field(default_factory=list)

    def _maybe_fail(self, step: str, code: ErrorCode) -> None:
        self.calls.append(step)
        if step in self.fail_at:
            raise InferenceError(f"{step} failed", code=code)

    async def bootstrap(self, ctx: RuntimeContext, timeout_s: float) -> BootstrapReport:
        self._maybe_fail("bootstrap", ErrorCode.BOOTSTRAP_FAILED)
        return BootstrapReport("NVIDIA L40S", 46068, "550")

    async def start(self, ctx: RuntimeContext, timeout_s: float) -> None:
        self._maybe_fail("start", ErrorCode.MODEL_STARTUP_FAILED)

    async def wait_ready(self, ctx: RuntimeContext, timeout_s: float) -> None:
        self._maybe_fail("wait_ready", ErrorCode.MODEL_STARTUP_FAILED)

    async def transfer(
        self, ctx: RuntimeContext, request: InferenceRequest, timeout_s: float
    ) -> None:
        self.requests.append(request)
        self._maybe_fail("transfer", ErrorCode.TRANSFER_FAILED)

    async def infer(self, ctx: RuntimeContext, timeout_s: float) -> str:
        self._maybe_fail("infer", ErrorCode.INFERENCE_FAILED)
        return self.outputs.pop(0) if len(self.outputs) > 1 else self.outputs[0]

    async def cleanup(self, ctx: RuntimeContext, timeout_s: float) -> None:
        self._maybe_fail("cleanup", ErrorCode.CLEANUP_FAILED)


def config(**overrides: object) -> OrchestratorConfig:
    base: dict[str, object] = dict(
        model_id="test/model",
        gpu_requirements=GpuRequirements("L40S", 40),
        remote_data_dir="/tmp/ephemera/jobs",
        max_job_runtime_s=30,
        max_provisioning_s=10,
        bootstrap_timeout_s=10,
        model_ready_timeout_s=10,
        transfer_timeout_s=10,
        inference_timeout_s=10,
        cleanup_timeout_s=10,
        destroy_timeout_s=10,
        destroy_verify_timeout_s=5,
        max_output_tokens=500,
        result_retention_s=3600,
        heartbeat_interval_s=0.1,
        destroy_attempts=2,
    )
    base.update(overrides)
    return OrchestratorConfig(**base)  # type: ignore[arg-type]


def build_orchestrator(
    repo: PostgresJobRepository,
    workspace: JobWorkspace,
    compute: object,
    inference: object,
    **cfg: object,
) -> Orchestrator:
    return Orchestrator(
        repo=repo,
        compute=compute,
        inference=inference,  # type: ignore[arg-type]
        documents=PyMuPDFDocumentProcessor(max_pages=10, max_chars=20000, timeout_s=20),
        workspace=workspace,
        config=config(**cfg),
        worker_id="test-worker",
    )


async def submit(
    repo: PostgresJobRepository, workspace: JobWorkspace, pdf: bytes, *, force_failure: bool = False
) -> uuid.UUID:
    job_id = uuid.uuid4()
    d = workspace.create(job_id)
    (d / DOCUMENT_FILENAME).write_bytes(pdf)
    await repo.create(
        job_id=job_id,
        filename="t.pdf",
        content_type="application/pdf",
        file_size=len(pdf),
        sha256="0" * 64,
        analysis_type=AnalysisType.GENERAL,
        mode="simulation",
        model_id="test/model",
        requested_gpu="L40S",
        instance_name=instance_name_for(job_id),
        force_inference_failure=force_failure,
    )
    return job_id
