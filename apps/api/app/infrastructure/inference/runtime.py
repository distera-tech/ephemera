"""Glue between a ComputeProvider and an InferenceProvider for one job."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

from app.domain.models import ExecResult
from app.domain.ports import ComputeProvider

BOOTSTRAP_SCRIPT = Path(__file__).resolve().parents[5] / "infra" / "gpu" / "bootstrap.sh"


def bootstrap_script_path() -> Path:
    """Location of infra/gpu/bootstrap.sh (repo checkout) or the copy baked into the image."""
    for candidate in (Path("/app/infra/gpu/bootstrap.sh"), BOOTSTRAP_SCRIPT):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("infra/gpu/bootstrap.sh not found")


@dataclass(slots=True)
class BoundExecutor:
    compute: ComputeProvider
    instance_name: str

    async def execute(self, command: str, timeout_s: float) -> ExecResult:
        return await self.compute.execute(self.instance_name, command, timeout_s)

    async def upload(self, local_path: Path, remote_path: str, timeout_s: float) -> None:
        await self.compute.upload(self.instance_name, local_path, remote_path, timeout_s)

    async def download(self, remote_path: str, local_path: Path, timeout_s: float) -> None:
        await self.compute.download(self.instance_name, remote_path, local_path, timeout_s)


@dataclass(slots=True)
class JobRuntimeContext:
    job_id: uuid.UUID
    executor: BoundExecutor
    local_dir: Path
    remote_dir: str
