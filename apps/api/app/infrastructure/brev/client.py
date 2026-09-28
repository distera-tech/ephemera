"""BrevClient — the single gateway to the Brev CLI and to the instances' SSH.

* ``brev`` for search/create/ls/refresh/delete; OpenSSH ``ssh``/``scp`` with the
  config ``brev refresh`` writes for remote commands and file transfer (see
  ``commands`` for why not ``brev exec``/``brev copy``);
* argument arrays only (``create_subprocess_exec``), never ``shell=True``;
* explicit timeout on every call; on timeout the whole process group is killed
  (``ssh`` spawns Brev's ``cloudflared`` proxy and ``brev mint-cert``);
* sanitized environment: only PATH, an isolated HOME, proxy settings and the
  Brev credentials — the worker's other environment (e.g. ``HF_TOKEN``,
  ``DATABASE_URL``) is not inherited;
* stdin is closed so the CLI can never block on an interactive prompt;
* stdout/stderr captured and redacted before they reach logs or exceptions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError

from app.core.logging import get_logger, redact
from app.domain.models import ExecResult, GpuOffer, GpuRequirements, InstanceInfo
from app.infrastructure.brev import commands
from app.infrastructure.brev.exceptions import (
    BrevAuthError,
    BrevCLINotFoundError,
    BrevCommandError,
    BrevOutputError,
    BrevTimeoutError,
)
from app.infrastructure.brev.models import BrevGpuInstanceType, BrevLsOutput

log = get_logger("brev")

_PASSTHROUGH_ENV = (
    "PATH",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
    "https_proxy",
    "http_proxy",
    "no_proxy",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "LANG",
    "TZ",
)
_AUTH_HINTS = ("not logged in", "unauthorized", "unauthenticated", "brev login", "401")
_NOT_FOUND_HINTS = (
    "not found",
    "no workspaces found",
    "no instance",
    "does not exist",
    "could not find",
)


@dataclass(frozen=True, slots=True)
class CommandResult:
    args: tuple[str, ...]
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    label: str = ""


def _parse_json(stdout: str) -> Any:
    """Parse JSON output, tolerating stray log lines some CLI versions print first."""
    text = stdout.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        decoder = json.JSONDecoder()
        for index, ch in enumerate(text):
            if ch in "[{":
                with contextlib.suppress(json.JSONDecodeError):
                    return decoder.raw_decode(text[index:])[0]
        raise BrevOutputError("could not parse JSON from brev output") from None


class BrevClient:
    def __init__(
        self,
        *,
        cli_path: str,
        home: Path,
        api_key: SecretStr | None,
        org: str | None,
        exec_on_host: bool | None = None,
        ssh_path: str = "ssh",
        scp_path: str = "scp",
        ssh_connect_timeout_s: int = 60,
    ) -> None:
        self.cli_path = cli_path
        self.home = home
        self._api_key = api_key
        self.org = org
        # None = auto: probe `<name>` (Brev's SSH-access endpoint, the VM itself in the default
        # VM mode) first, then the legacy `<name>-host` alias; the one that answers is pinned.
        self.exec_on_host = exec_on_host
        self._pinned_target: dict[str, bool] = {}
        self.ssh_path = ssh_path
        self.scp_path = scp_path
        self.ssh_connect_timeout_s = ssh_connect_timeout_s
        self._org_set = False
        self._org_lock = asyncio.Lock()

    # ------------------------------------------------------------------ plumbing
    def _env(self, *, for_ssh: bool = False) -> dict[str, str]:
        env = {k: v for k in _PASSTHROUGH_ENV if (v := os.environ.get(k))}
        env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
        env["HOME"] = str(self.home)
        if not for_ssh:
            # The CLI starts a daemonised ssh-agent whenever SSH_AUTH_SOCK is empty, leaking one
            # agent per call. Brev's ssh config uses IdentityFile, so no agent is needed.
            env["SSH_AUTH_SOCK"] = "/dev/null"
        # ssh also needs the API key: Brev's config may run `brev mint-cert` via `Match exec`.
        env["BREV_NO_ANALYTICS"] = "1"
        env["DO_NOT_TRACK"] = "1"
        if self._api_key is not None:
            env["BREV_API_KEY"] = self._api_key.get_secret_value()
        return env

    async def run(
        self,
        args: list[str],
        *,
        timeout_s: float,
        check: bool = True,
        binary: str | None = None,
        label: str | None = None,
    ) -> CommandResult:
        """Run ``brev <args>`` (or ``binary <args>``, e.g. ssh/scp) with the sanitized env."""
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        exe = binary or self.cli_path
        label = label or args[0]
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                exe,
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=self._env(for_ssh=binary is not None),
                start_new_session=True,  # own process group → killable as a unit
            )
        except FileNotFoundError:
            if binary is not None:
                raise BrevCLINotFoundError(f"{exe!r} not found; install openssh-client") from None
            raise BrevCLINotFoundError(
                f"Brev CLI not found at {self.cli_path!r}; install it or set BREV_CLI_PATH"
            ) from None
        try:
            out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except (TimeoutError, asyncio.CancelledError) as exc:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=5)
            log.warning(
                "brev.command_aborted",
                extra={
                    "subcommand": label,
                    "reason": "cancelled" if isinstance(exc, asyncio.CancelledError) else "timeout",
                    "duration_ms": int((time.monotonic() - started) * 1000),
                },
            )
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise BrevTimeoutError(
                f"brev {label} timed out after {timeout_s:.0f}s", timeout_s=timeout_s
            ) from None
        duration_ms = int((time.monotonic() - started) * 1000)
        result = CommandResult(
            args=tuple(args),
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=out_b.decode(errors="replace"),
            stderr=err_b.decode(errors="replace"),
            duration_ms=duration_ms,
            label=label,
        )
        log.info(
            "brev.command",
            extra={
                "subcommand": label,
                "exit_code": result.exit_code,
                "duration_ms": duration_ms,
            },
        )
        if check and result.exit_code != 0:
            raise self._error_for(result)
        return result

    @staticmethod
    def _error_for(result: CommandResult) -> BrevCommandError | BrevAuthError:
        tail = redact((result.stderr or result.stdout).strip()[-500:])
        lowered = tail.lower()
        if any(h in lowered for h in _AUTH_HINTS):
            return BrevAuthError(f"brev authentication failed: {tail}")
        return BrevCommandError(
            f"brev {result.label or result.args[0]} exited with {result.exit_code}: {tail}",
            exit_code=result.exit_code,
            stderr_tail=tail,
        )

    async def ensure_org(self) -> None:
        if not self.org or self._org_set:
            return
        async with self._org_lock:
            if not self._org_set:
                await self.run(commands.set_org(self.org), timeout_s=60)
                self._org_set = True

    # ------------------------------------------------------------------ operations
    async def search_gpu(self, req: GpuRequirements, timeout_s: float = 90) -> list[GpuOffer]:
        await self.ensure_org()
        result = await self.run(commands.search_gpu(req), timeout_s=timeout_s)
        raw = _parse_json(result.stdout) if result.stdout.strip() else []
        if isinstance(raw, dict):  # empty result shape varies between versions
            raw = raw.get("items") or raw.get("instances") or []
        if not isinstance(raw, list):
            raise BrevOutputError("unexpected `brev search --json` shape")
        offers: list[GpuOffer] = []
        for item in raw:
            try:
                offers.append(BrevGpuInstanceType.model_validate(item).to_offer())
            except ValidationError:
                continue
        return offers

    async def create(self, name: str, instance_types: list[str], timeout_s: float) -> CommandResult:
        await self.ensure_org()
        # The CLI's own readiness timeout is slightly shorter than ours so it can exit cleanly.
        cli_timeout = max(60, int(timeout_s) - 30)
        return await self.run(
            commands.create(name, instance_types, cli_timeout), timeout_s=timeout_s
        )

    async def list_instances(self, timeout_s: float = 60) -> list[InstanceInfo]:
        await self.ensure_org()
        result = await self.run(commands.list_json(), timeout_s=timeout_s)
        if not result.stdout.strip():
            return []
        try:
            parsed = BrevLsOutput.model_validate(_parse_json(result.stdout))
        except ValidationError:
            raise BrevOutputError("unexpected `brev ls --json` shape") from None
        return [w.to_info() for w in parsed.workspaces or []]

    async def get_instance(self, name: str, timeout_s: float = 60) -> InstanceInfo | None:
        return next((i for i in await self.list_instances(timeout_s) if i.name == name), None)

    async def refresh_ssh_config(self, timeout_s: float = 120) -> None:
        await self.ensure_org()
        await self.run(commands.refresh(), timeout_s=timeout_s)

    def ssh_targets(self) -> list[bool]:
        """Aliases to probe, as `host` flags: False = `<name>`, True = `<name>-host`."""
        return [False, True] if self.exec_on_host is None else [self.exec_on_host]

    def pin_target(self, name: str, host: bool) -> None:
        self._pinned_target[name] = host

    def forget_target(self, name: str) -> None:
        self._pinned_target.pop(name, None)

    def _host(self, name: str, host: bool | None) -> bool:
        if host is not None:
            return host
        return self._pinned_target.get(name, self.ssh_targets()[0])

    def _connect_timeout(self, timeout_s: float) -> int:
        return int(max(5, min(self.ssh_connect_timeout_s, timeout_s - 1)))

    async def exec(
        self, name: str, command: str, timeout_s: float, *, host: bool | None = None
    ) -> ExecResult:
        """Run one command over SSH. Exit 255 means ssh itself failed (transport)."""
        args = commands.ssh_exec(
            name,
            command,
            host=self._host(name, host),
            connect_timeout_s=self._connect_timeout(timeout_s),
        )
        result = await self.run(
            args, timeout_s=timeout_s, check=False, binary=self.ssh_path, label="exec"
        )
        return ExecResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=redact(result.stderr[-2000:]),
            duration_ms=result.duration_ms,
        )

    async def copy_to(
        self, name: str, local_path: Path, remote_path: str, timeout_s: float
    ) -> None:
        args = commands.scp_to(
            name,
            str(local_path),
            remote_path,
            host=self._host(name, None),
            connect_timeout_s=self._connect_timeout(timeout_s),
        )
        await self.run(args, timeout_s=timeout_s, binary=self.scp_path, label="copy")

    async def copy_from(
        self, name: str, remote_path: str, local_path: Path, timeout_s: float
    ) -> None:
        args = commands.scp_from(
            name,
            remote_path,
            str(local_path),
            host=self._host(name, None),
            connect_timeout_s=self._connect_timeout(timeout_s),
        )
        await self.run(args, timeout_s=timeout_s, binary=self.scp_path, label="copy")

    async def delete(self, name: str, timeout_s: float) -> bool:
        """Request deletion. Returns False if the instance was already absent."""
        await self.ensure_org()
        result = await self.run(commands.delete(name), timeout_s=timeout_s, check=False)
        if result.exit_code == 0:
            return True
        text = (result.stderr + result.stdout).lower()
        # On a "not found" message, confirm with an authoritative listing before trusting it.
        if any(h in text for h in _NOT_FOUND_HINTS) and await self.get_instance(name) is None:
            return False
        raise self._error_for(result)
