"""BrevComputeProvider — implements the ``ComputeProvider`` port on top of BrevClient."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.application.gpu_selection import rank_offers
from app.core.logging import get_logger
from app.domain.errors import ComputeError, ErrorCode
from app.domain.models import (
    ExecResult,
    GpuOffer,
    GpuRequirements,
    GpuSelection,
    InstanceInfo,
    is_ephemera_instance_name,
)
from app.infrastructure.brev.client import BrevClient
from app.infrastructure.brev.commands import require_ephemera_name
from app.infrastructure.brev.exceptions import BrevError, BrevTimeoutError

log = get_logger("brev.lifecycle")

# ssh stderr fragments meaning "no TCP route to the instance's SSH endpoint" (as opposed to
# an auth, proxy or remote-command problem).
_UNREACHABLE_HINTS = (
    "connection timed out",
    "operation timed out",
    "no route to host",
    "network is unreachable",
    "connection refused",
)


async def _backoff_sleep(attempt: int, base: float = 2.0, cap: float = 15.0) -> None:
    await asyncio.sleep(min(cap, base * (1.6**attempt)))


@dataclass
class _Attempts:
    """Per-instance provisioning history, used to fall back to another provider."""

    selection: GpuSelection
    current: GpuOffer | None = None
    current_type: str | None = None
    excluded: set[str] = field(default_factory=set)
    switches: int = 0

    def exclude_current(self) -> None:
        if self.current_type:
            self.excluded.add(f"type:{self.current_type}")
        if self.current is not None:
            self.excluded.add(f"type:{self.current.instance_type}")
            if self.current.provider:
                self.excluded.add(f"provider:{self.current.provider.lower()}")

    def remaining(self) -> list[GpuOffer]:
        return [
            c
            for c in self.selection.candidates
            if f"type:{c.instance_type}" not in self.excluded
            and f"provider:{(c.provider or '').lower()}" not in self.excluded
        ]


class BrevComputeProvider:
    name = "brev"
    is_simulated = False

    def __init__(
        self,
        client: BrevClient,
        *,
        ssh_probe_timeout_s: float = 120,
        ssh_unreachable_timeout_s: float = 300,
        max_provider_switches: int = 2,
        min_time_for_new_instance_s: float = 480,
        excluded_providers: tuple[str, ...] = (),
    ) -> None:
        self.client = client
        self.ssh_probe_timeout_s = ssh_probe_timeout_s
        # Give up early (and destroy the paid GPU) when the SSH endpoint stays unreachable at
        # the TCP level after Brev reports setup COMPLETED; waiting longer has never helped.
        self.ssh_unreachable_timeout_s = ssh_unreachable_timeout_s
        self.max_provider_switches = max_provider_switches
        self.min_time_for_new_instance_s = min_time_for_new_instance_s
        self._attempts: dict[str, _Attempts] = {}
        self.excluded_providers = {p.strip().lower() for p in excluded_providers if p.strip()}

    async def select_gpu(self, requirements: GpuRequirements) -> GpuSelection:
        offers = (
            []
            if requirements.explicit_instance_types
            else await self.client.search_gpu(requirements)
        )
        if self.excluded_providers:
            offers = [
                o for o in offers if (o.provider or "").lower() not in self.excluded_providers
            ]
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
        self._attempts[name] = _Attempts(selection)
        return await self._create(name, list(selection.candidates), timeout_s)

    async def _create(
        self, name: str, candidates: list[GpuOffer], timeout_s: float
    ) -> InstanceInfo:
        types = [c.instance_type for c in candidates]
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
        attempts = self._attempts.get(name)
        offer = attempts.selection.offer_for(info.instance_type) if attempts else None
        if attempts is not None:
            attempts.current, attempts.current_type = offer, info.instance_type
        log.info(
            "brev.instance_created",
            extra={
                "instance_type": info.instance_type,
                "provider": offer.provider if offer else None,
                "price_per_hour": offer.price_per_hour if offer else None,
            },
        )
        return info

    async def _switch_provider(self, name: str, deadline: float, reason: str) -> bool:
        """Destroy an instance whose SSH endpoint is unreachable and re-create it with the
        remaining candidates from *other* providers. Returns False when there is no
        alternative or not enough time left (the caller then fails the job; teardown
        destroys whatever exists)."""
        attempts = self._attempts.get(name)
        if attempts is None or attempts.switches >= self.max_provider_switches:
            return False
        attempts.exclude_current()
        candidates = attempts.remaining()
        # A new instance needs create (~3 min) + setup + SSH; don't start one we can't finish.
        if not candidates or deadline - time.monotonic() < self.min_time_for_new_instance_s:
            return False
        log.warning(
            "brev.provider_unreachable_switching",
            extra={
                "reason": reason,
                "excluded": sorted(attempts.excluded),
                "next_types": [c.instance_type for c in candidates],
            },
        )
        await self.client.delete(name, timeout_s=300)
        if not await self.verify_destroyed(name, timeout_s=max(60.0, deadline - time.monotonic())):
            return False
        attempts.switches += 1
        await self._create(name, candidates, max(60.0, deadline - time.monotonic()))
        return True

    async def wait_until_ready(self, name: str, timeout_s: float) -> InstanceInfo:
        deadline = time.monotonic() + timeout_s
        attempt = 0
        info: InstanceInfo | None = None
        failed_probes = 0
        unreachable_since: float | None = None
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
            if info is not None and info.is_setup_failed:
                raise ComputeError(
                    "Brev instance setup failed (build status CREATE_FAILED)",
                    code=ErrorCode.PROVISIONING_FAILED,
                )
            if info is not None and info.is_running and not info.is_setup_complete:
                # RUNNING is reported before Brev finishes setting the machine up (Docker,
                # NVIDIA runtime). Brev's own tooling waits for build COMPLETED; so do we.
                last_error = f"waiting for instance setup (build status {info.build_status})"
                log.info("brev.waiting_for_setup", extra={"build_status": info.build_status})
            elif info is not None and info.is_running:
                if failed_probes % 3 == 0:
                    # Write/refresh the `<instance>-host` SSH alias (and cloudflared) before
                    # probing; redone every few failures in case the route changed after setup.
                    try:
                        await self.client.refresh_ssh_config()
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
                    detail = probe.stderr.strip().splitlines()[-1:] or [""]
                    last_error = f"ssh probe exit {probe.exit_code}: {detail[0][:300]}".rstrip(": ")
                except BrevError as exc:
                    last_error = exc.message
                failed_probes += 1
                if any(h in last_error.lower() for h in _UNREACHABLE_HINTS):
                    unreachable_since = unreachable_since or time.monotonic()
                    if time.monotonic() - unreachable_since >= self.ssh_unreachable_timeout_s:
                        # Seen on real Brev: one provider's SSH port forward never answered
                        # while the network was fine. Another provider usually works.
                        if await self._switch_provider(name, deadline, last_error):
                            failed_probes = 0
                            unreachable_since = None
                            attempt = 0
                            continue
                        raise ComputeError(
                            "the instance's SSH endpoint was unreachable from the worker for "
                            f"{self.ssh_unreachable_timeout_s:.0f}s after setup completed "
                            f"({last_error}) and no other provider could be tried in time. "
                            "Check outbound ports with `make net-check`; if they are fine, the "
                            "provider's port forwarding is broken: exclude it with "
                            "BREV_EXCLUDED_PROVIDERS or pin types with BREV_INSTANCE_TYPES",
                            code=ErrorCode.PROVISIONING_FAILED,
                        )
                else:
                    unreachable_since = None
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
        self._attempts.pop(name, None)
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
