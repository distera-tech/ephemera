"""Argument-vector builders for the Brev CLI.

Pure functions, unit-tested, and the only place CLI syntax lives. Every
builder returns a list passed to ``create_subprocess_exec`` (never a shell).
Instance names are validated against Ephemera's naming scheme so a bug can
never target a non-Ephemera instance; paths are validated against a strict
character set because ``scp`` parses ``alias:path`` and the remote command is run
by the instance's login shell.

Remote execution and file transfer use OpenSSH (``ssh``/``scp``) directly with the
config ``brev refresh`` writes, not ``brev exec``/``brev copy``: those give each SSH
connection attempt 5 s (20 tries), re-run a failed command, and refresh the whole
SSH config on every copy. Over Brev's cloudflared tunnel a first connection after
setup can take longer than 5 s, which made every ``brev exec`` fail.
"""

from __future__ import annotations

import re

from app.domain.models import GpuRequirements, is_ephemera_instance_name
from app.infrastructure.brev.exceptions import UnsafeInstanceNameError

_SAFE_PATH = re.compile(r"^/[A-Za-z0-9._/\-]+$")
_SAFE_ORG = re.compile(r"^[A-Za-z0-9._\-]{1,100}$")
_SAFE_TYPE = re.compile(r"^[A-Za-z0-9._:\-]{1,120}$")


def require_ephemera_name(name: str) -> str:
    if not is_ephemera_instance_name(name):
        raise UnsafeInstanceNameError(f"refusing to operate on non-Ephemera instance name {name!r}")
    return name


def _require_path(path: str) -> str:
    if not _SAFE_PATH.match(path) or ".." in path.split("/"):
        raise ValueError("unsafe path for brev copy")
    return path


def set_org(org: str) -> list[str]:
    if not _SAFE_ORG.match(org):
        raise ValueError("invalid BREV_ORG")
    return ["set", org]


def search_gpu(req: GpuRequirements) -> list[str]:
    args = ["search", "gpu", "--json", "--sort", "price", "--min-total-vram", _num(req.min_vram_gb)]
    if req.min_compute_capability is not None:
        args += ["--min-capability", _num(req.min_compute_capability)]
    if req.min_disk_gb is not None:
        args += ["--min-disk", _num(req.min_disk_gb)]
    return args


def create(name: str, instance_types: list[str], timeout_s: int) -> list[str]:
    require_ephemera_name(name)
    if not instance_types:
        raise ValueError("at least one instance type is required")
    for t in instance_types:
        if not _SAFE_TYPE.match(t):
            raise ValueError("invalid instance type identifier")
    return [
        "create",
        name,
        "--type",
        ",".join(instance_types),
        "--count",
        "1",
        "--timeout",
        str(int(timeout_s)),
        # No Jupyter: it is an extra network-facing service the workload does not need.
        "--jupyter=false",
    ]


def refresh() -> list[str]:
    """Rewrite Brev's SSH config so `ssh <instance>` aliases resolve."""
    return ["refresh"]


def list_json() -> list[str]:
    return ["ls", "--json"]


# Options that override Brev's generated host entry where it does not suit a
# non-interactive orchestrator. `-o` values take precedence over the config file.
_SSH_OPTIONS = (
    "BatchMode=yes",  # never prompt
    "StrictHostKeyChecking=no",  # fresh, per-job instance; Brev's own config does the same
    "UserKnownHostsFile=/dev/null",
    "ServerAliveInterval=15",
    "ServerAliveCountMax=4",
    "LogLevel=ERROR",
    "RemoteCommand=none",
    "RequestTTY=no",  # Brev sets `RequestTTY yes`
    "ForwardAgent=no",  # Brev sets `ForwardAgent yes`
    "ControlMaster=no",  # no background master process outliving the call
    "ControlPath=none",
)


def ssh_alias(name: str, *, host: bool) -> str:
    """The `Host` alias `brev refresh` writes: ``<name>`` (container) or ``<name>-host`` (VM)."""
    require_ephemera_name(name)
    return f"{name}-host" if host else name


def _ssh_options(connect_timeout_s: int) -> list[str]:
    opts: list[str] = []
    for o in (*_SSH_OPTIONS, f"ConnectTimeout={max(1, int(connect_timeout_s))}"):
        opts += ["-o", o]
    return opts


def ssh_exec(name: str, command: str, *, host: bool, connect_timeout_s: int) -> list[str]:
    """Arguments for ``ssh``: run one fixed, single-line command on the instance."""
    if "\n" in command or "\x00" in command or len(command) > 4096:
        raise ValueError("remote command must be a single short line")
    return ["-T", *_ssh_options(connect_timeout_s), ssh_alias(name, host=host), command]


def scp_to(
    name: str, local_path: str, remote_path: str, *, host: bool, connect_timeout_s: int
) -> list[str]:
    alias = ssh_alias(name, host=host)
    return [
        "-q",
        *_ssh_options(connect_timeout_s),
        _require_path(local_path),
        f"{alias}:{_require_path(remote_path)}",
    ]


def scp_from(
    name: str, remote_path: str, local_path: str, *, host: bool, connect_timeout_s: int
) -> list[str]:
    alias = ssh_alias(name, host=host)
    return [
        "-q",
        *_ssh_options(connect_timeout_s),
        f"{alias}:{_require_path(remote_path)}",
        _require_path(local_path),
    ]


def delete(name: str) -> list[str]:
    # Exactly one, validated name. There is intentionally no "delete all" builder.
    return ["delete", require_ephemera_name(name)]


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)
