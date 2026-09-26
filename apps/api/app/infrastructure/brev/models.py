"""Typed views of Brev CLI JSON output (verified against brev-cli v0.6.335 source).

* ``brev search gpu --json`` → list of ``GPUInstanceInfo`` (pkg/cmd/gpusearch/gpusearch.go)
* ``brev ls --json``        → ``{"workspaces": [WorkspaceInfo, ...]}`` (pkg/cmd/ls/ls.go)
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import GpuOffer, InstanceInfo


class BrevGpuInstanceType(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: str
    cloud: str | None = None
    provider: str | None = None
    gpu_name: str = ""
    gpu_count: int = 0
    vram_per_gpu_gb: float = 0
    total_vram_gb: float = 0
    capability: float | None = None
    price_per_hour: float | None = None
    boot_time_seconds: int | None = None

    def to_offer(self) -> GpuOffer:
        return GpuOffer(
            instance_type=self.type,
            gpu_name=self.gpu_name,
            gpu_count=self.gpu_count,
            vram_per_gpu_gb=self.vram_per_gpu_gb,
            provider=self.cloud or self.provider,
            compute_capability=self.capability,
            price_per_hour=self.price_per_hour,
            boot_time_seconds=self.boot_time_seconds,
        )


class BrevWorkspace(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    id: str = ""
    status: str = ""
    build_status: str = ""
    shell_status: str = ""
    health_status: str = ""
    instance_type: str = ""
    instance_kind: str = ""
    gpu: str = ""

    def to_info(self) -> InstanceInfo:
        return InstanceInfo(
            name=self.name,
            status=self.status or "UNKNOWN",
            instance_id=self.id or None,
            instance_type=self.instance_type or None,
            gpu_name=self.gpu if self.gpu and self.gpu != "-" else None,
            shell_ready=self.shell_status.upper() == "READY",
        )


class BrevLsOutput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    workspaces: list[BrevWorkspace] | None = Field(default=None)
