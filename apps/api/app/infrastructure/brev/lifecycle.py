"""BrevComputeProvider — implements the ``ComputeProvider`` port on top of BrevClient."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from app.application.gpu_selection import rank_offers
from app.core.logging import get_logger
from app.domain.errors import ComputeError, ErrorCode
from app.domain.models import (
    ExecResult,
    GpuRequirements,
    GpuSelection,
    InstanceInfo,
    is_ephemera_instance_name,
)
from app.infrastructure.brev.client import BrevClient
from app.infrastructure.brev.commands import require_ephemera_name
from app.infrastructure.brev.exceptions import BrevError, BrevTimeoutError

log = get_logger("brev.lifecycle")


async def _backoff_sleep(attempt: int, base: float = 2.0, cap: float = 15.0) -> None:
    await asyncio.sleep(min(cap, base * (1.6**attempt)))


class BrevComputeProvider:
    name = "brev"
    is_simulated = False

    def __init__(self, client: BrevClient, *, ssh_probe_timeout_s: float = 120) -> None:
        self.client = client
        self.ssh_probe_timeout_s = ssh_probe_timeout_s

    async def select_gpu(self, requirements: GpuRequirements) -> GpuSelection:
        offers = (
            []
            if requirements.explicit_instance_types
            else await self.client.search_gpu(requirements)
        )
        ranked = rank_offers(offers, requirements)
        if not ranked:
            raise ComputeError(
                f"no Brev instance type satisfies >= {requirements.min_vram_gb:g} GB VRAM "
                f"(preferred {requirements.preferred_gpu})",
                code=ErrorCode.NO_GPU_AVAILABLE,
            )
        return GpuSelection(requested_gpu=requirements.preferred_gpu, candidates=tuple(ranked))

    async def provision(self, name: str, selection: GpuSelection, timeout_s: float) -> InstanceInfo:
        require_ephemera_name(name)
        types = [c.instance_type for c in selection.candidates]
        try:
            await self.client.create(name, types, timeout_s=timeout_s)
        except BrevTimeoutError:
            raise ComputeError(
                f"provisioning exceeded {timeout_s:.0f}s", code=ErrorCode.PROVISIONING_TIMEOUT
            ) from None
        except BrevError as exc:
            raise ComputeError(exc.message, code=ErrorCode.PROVISIONING_FAILED) from None
        info = await self.client.get_instance(name)
        if info is None:
            raise ComputeError(
                "brev create returned but the instance is not listed",
                code=ErrorCode.PROVISIONING_FAILED,
            )
        return info

    async def wait_until_ready(self, name: str, timeout_s: float) -> InstanceInfo:
        deadline = time.monotonic() + timeout_s
        attempt = 0
        info: InstanceInfo | None = None
        refreshed = False
        last_error = ""
        while time.monotonic() < deadline:
            try:
                info = await self.client.get_instance(name)
            except BrevError as exc:
                last_error = exc.message
                info = None
            if info is not None and info.is_failed:
                raise ComputeError(
                    f"instance entered status {info.status}", code=ErrorCode.PROVISIONING_FAILED
                )
            if info is not None and info.is_running:
                if not refreshed:
                    # Make sure the `ssh <instance>` alias exists before probing.
                    try:
                        await self.client.refresh_ssh_config()
                        refreshed = True
                    except BrevError as exc:
                        last_error = f"brev refresh: {exc.message}"
                # Brev reports RUNNING before sshd is always reachable; prove we can execute.
                # A slow or failed probe is retried until the deadline, never fatal on its own.
                remaining = deadline - time.monotonic()
                try:
                    probe = await self.client.exec(
                        name, "true", timeout_s=max(10.0, min(self.ssh_probe_timeout_s, remaining))
                    )
                    if probe.exit_code == 0:
                        return info
                    last_error = f"ssh probe exit {probe.exit_code}"
                except BrevError as exc:
                    last_error = exc.message
                log.info("brev.ssh_probe_retry", extra={"attempt": attempt, "error": last_error})
            await _backoff_sleep(attempt)
            attempt += 1
        status = info.status if info else "absent"
        raise ComputeError(
            f"instance not reachable over SSH within {timeout_s:.0f}s "
            f"(last status: {status}; last error: {last_error or 'none'})",
            code=ErrorCode.PROVISIONING_TIMEOUT,
        )

    async def execute(self, name: str, command: str, timeout_s: float) -> ExecResult:
        return await self.client.exec(name, command, timeout_s=timeout_s)

    async def upload(self, name: str, local_path: Path, remote_path: str, timeout_s: float) -> None:
        await self.client.copy_to(name, local_path, remote_path, timeout_s=timeout_s)

    async def download(
        self, name: str, remote_path: str, local_path: Path, timeout_s: float
    ) -> None:
        await self.client.copy_from(name, remote_path, local_path, timeout_s=timeout_s)

    async def destroy(self, name: str, timeout_s: float) -> None:
        require_ephemera_name(name)
        await self.client.delete(name, timeout_s=timeout_s)

    async def get_instance(self, name: str) -> InstanceInfo | None:
        return await self.client.get_instance(name)

    async def verify_destroyed(self, name: str, timeout_s: float) -> bool:
        """Poll ``brev ls`` until the instance is gone. DELETING counts as *not yet* gone."""
        deadline = time.monotonic() + timeout_s
        attempt = 0
        while time.monotonic() < deadline:
            try:
                if await self.client.get_instance(name) is None:
                    return True
            except BrevError as exc:
                log.warning("brev.verify_list_failed", extra={"error": exc.message})
            await _backoff_sleep(attempt, base=3.0, cap=20.0)
            attempt += 1
        return False

    async def list_instances(self) -> list[InstanceInfo]:
        return [i for i in await self.client.list_instances() if is_ephemera_instance_name(i.name)]
