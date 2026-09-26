"""Simulated compute provider (EPHEMERA_MODE=simulation only).

Keeps an in-memory registry of fake instances with realistic delays so the
lifecycle and COMPUTE = 0 accounting can be demonstrated without credentials.
It never claims to be real: its name is ``simulation`` and every instance's
GPU is labelled ``SIMULATED``.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path

from app.domain.errors import ComputeError, ErrorCode
from app.domain.models import (
    ExecResult,
    GpuOffer,
    GpuRequirements,
    GpuSelection,
    InstanceInfo,
    is_ephemera_instance_name,
)

SIMULATED_CATALOG = (
    GpuOffer("sim.l40s.1x", "L40S", 1, 48, "simulation", 8.9, None, 180),
    GpuOffer("sim.a100.1x", "A100", 1, 80, "simulation", 8.0, None, 240),
    GpuOffer("sim.a10g.1x", "A10G", 1, 24, "simulation", 8.6, None, 120),
)


@dataclass
class SimulatedComputeProvider:
    time_scale: float = 1.0
    name: str = "simulation"
    is_simulated: bool = True
    instances: dict[str, InstanceInfo] = field(default_factory=dict)
    fail_provision: bool = False

    async def _sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds * self.time_scale)

    async def select_gpu(self, requirements: GpuRequirements) -> GpuSelection:
        from app.application.gpu_selection import rank_offers

        await self._sleep(0.5)
        ranked = rank_offers(SIMULATED_CATALOG, requirements)
        if not ranked:
            raise ComputeError(
                "no simulated GPU satisfies requirements", code=ErrorCode.NO_GPU_AVAILABLE
            )
        return GpuSelection(requested_gpu=requirements.preferred_gpu, candidates=tuple(ranked))

    async def provision(self, name: str, selection: GpuSelection, timeout_s: float) -> InstanceInfo:
        if not is_ephemera_instance_name(name):
            raise ComputeError("invalid instance name", code=ErrorCode.INTERNAL_ERROR)
        offer = selection.candidates[0]
        self.instances[name] = InstanceInfo(
            name, "DEPLOYING", f"sim-{name}", offer.instance_type, f"SIMULATED {offer.gpu_name}"
        )
        await self._sleep(6.0)
        if self.fail_provision:
            raise ComputeError("simulated provisioning failure", code=ErrorCode.PROVISIONING_FAILED)
        info = InstanceInfo(
            name,
            "RUNNING",
            f"sim-{name}",
            offer.instance_type,
            f"SIMULATED {offer.gpu_name}",
            shell_ready=True,
        )
        self.instances[name] = info
        return info

    async def wait_until_ready(self, name: str, timeout_s: float) -> InstanceInfo:
        await self._sleep(2.0)
        info = self.instances.get(name)
        if info is None:
            raise ComputeError("instance vanished", code=ErrorCode.PROVISIONING_FAILED)
        return info

    async def execute(self, name: str, command: str, timeout_s: float) -> ExecResult:
        return ExecResult(0, "", "", 0)

    async def upload(self, name: str, local_path: Path, remote_path: str, timeout_s: float) -> None:
        return None

    async def download(
        self, name: str, remote_path: str, local_path: Path, timeout_s: float
    ) -> None:
        return None

    async def destroy(self, name: str, timeout_s: float) -> None:
        if name in self.instances:
            info = self.instances[name]
            self.instances[name] = InstanceInfo(
                name, "DELETING", info.instance_id, info.instance_type, info.gpu_name
            )
        await self._sleep(3.0)
        self.instances.pop(name, None)

    async def get_instance(self, name: str) -> InstanceInfo | None:
        return self.instances.get(name)

    async def verify_destroyed(self, name: str, timeout_s: float) -> bool:
        await self._sleep(1.5)
        return name not in self.instances

    async def list_instances(self) -> list[InstanceInfo]:
        return list(self.instances.values())
