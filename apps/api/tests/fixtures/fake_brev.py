#!/usr/bin/env python3
"""A fake ``brev`` CLI for tests. Emulates the subset of brev-cli v0.6.335 that
Ephemera uses, with the same argument syntax and JSON output shapes.

State lives in $FAKE_BREV_STATE (a directory). The "remote" filesystem of each
instance is $FAKE_BREV_STATE/remote/<instance>/... . Behaviour switches are
read from $FAKE_BREV_STATE/mode (comma-separated flags), so a test can change
them between calls.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

STATE = Path(os.environ["FAKE_BREV_STATE"])
INSTANCES = STATE / "instances.json"
REMOTE = STATE / "remote"

CATALOG = [
    {
        "type": "a10g.1x",
        "provider": "aws",
        "cloud": "aws",
        "gpu_name": "A10G",
        "gpu_count": 1,
        "vram_per_gpu_gb": 24,
        "total_vram_gb": 24,
        "capability": 8.6,
        "price_per_hour": 1.1,
        "boot_time_seconds": 120,
    },
    {
        "type": "l40s.1x",
        "provider": "shadeform",
        "cloud": "hyperstack",
        "gpu_name": "L40S",
        "gpu_count": 1,
        "vram_per_gpu_gb": 48,
        "total_vram_gb": 48,
        "capability": 8.9,
        "price_per_hour": 1.5,
        "boot_time_seconds": 240,
    },
    {
        "type": "a100.1x",
        "provider": "gcp",
        "cloud": "gcp",
        "gpu_name": "A100",
        "gpu_count": 1,
        "vram_per_gpu_gb": 80,
        "total_vram_gb": 80,
        "capability": 8.0,
        "price_per_hour": 2.9,
        "boot_time_seconds": 300,
    },
]

VALID_ANALYSIS = {
    "document_type": "Contract",
    "summary": "A synthetic services agreement between two fictional companies.",
    "key_points": ["24 month term", "Automatic renewal"],
    "potential_risks": [{"description": "Uncapped customer indemnity", "severity": "high"}],
    "entities": [{"name": "Northwind Analytics Ltd.", "type": "organization"}],
    "requires_human_review": True,
}


def modes() -> set[str]:
    p = STATE / "mode"
    return {m.strip() for m in p.read_text().split(",")} if p.exists() else set()


def load() -> dict[str, dict[str, str]]:
    return json.loads(INSTANCES.read_text()) if INSTANCES.exists() else {}


def save(data: dict[str, dict[str, str]]) -> None:
    INSTANCES.write_text(json.dumps(data))


def remote_path(name: str, path: str) -> Path:
    return REMOTE / name / path.lstrip("/")


def log_call(argv: list[str]) -> None:
    with (STATE / "calls.log").open("a") as fh:
        fh.write(json.dumps(argv) + "\n")
    (STATE / "last_env.json").write_text(json.dumps(dict(os.environ)))


def cmd_search(args: list[str]) -> int:
    min_total = float(args[args.index("--min-total-vram") + 1]) if "--min-total-vram" in args else 0
    print(json.dumps([c for c in CATALOG if c["total_vram_gb"] >= min_total], indent=2))
    return 0


def cmd_create(args: list[str]) -> int:
    name = args[0]
    types = args[args.index("--type") + 1].split(",")
    m = modes()
    if "create_hang" in m:
        time.sleep(3600)
    data = load()
    if "create_fail" in m:
        print("Error: no capacity for requested instance types", file=sys.stderr)
        return 1
    chosen = next((c for c in CATALOG if c["type"] in types), None)
    data[name] = {
        "name": name,
        "id": f"id-{name}",
        "status": "RUNNING",
        "build_status": "COMPLETED",
        "shell_status": "READY",
        "health_status": "HEALTHY",
        "instance_type": chosen["type"] if chosen else types[0],
        "instance_kind": "gpu",
        "gpu": chosen["gpu_name"] if chosen else "-",
    }
    save(data)
    (REMOTE / name).mkdir(parents=True, exist_ok=True)
    if "create_fail_after_partial" in m:
        print("Error: instance failed to become ready", file=sys.stderr)
        return 1
    print(name)
    return 0


def cmd_ls(args: list[str]) -> int:
    m = modes()
    data = load()
    if "build_pending" in m or "build_failed" in m:
        counter = STATE / "ls_count"
        n = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(n + 1))
        for ws in data.values():
            if "build_failed" in m:
                ws["build_status"] = "CREATE_FAILED"
            elif n < 2:  # real Brev: RUNNING while setup is still BUILDING
                ws["build_status"] = "BUILDING"
            else:
                ws["build_status"] = "COMPLETED"
        print(json.dumps({"workspaces": list(data.values())}, indent=2))
        return 0
    print(json.dumps({"workspaces": list(load().values())}, indent=2))
    return 0


def cmd_delete(args: list[str]) -> int:
    m = modes()
    data = load()
    name = args[0]
    if "delete_fail" in m:
        print("Error: internal server error", file=sys.stderr)
        return 1
    if name not in data:
        print(f"Error: workspace {name} not found", file=sys.stderr)
        return 1
    del data[name]
    save(data)
    shutil.rmtree(REMOTE / name, ignore_errors=True)
    print(f"Deleting workspace {name}. This can take a few minutes.")
    return 0


def cmd_copy(args: list[str]) -> int:
    args = [a for a in args if a != "--host"]
    src, dst = args
    if ":" in src:
        name, path = src.split(":", 1)
        shutil.copyfile(remote_path(name, path), dst)
    else:
        name, path = dst.split(":", 1)
        target = remote_path(name, path)
        if not target.parent.exists():
            print("scp: No such file or directory", file=sys.stderr)
            return 1
        shutil.copyfile(src, target)
    return 0


def ok() -> int:
    print("EPHEMERA_RESULT=ok")
    return 0


def err(reason: str, message: str) -> int:
    # Mirrors infra/gpu/bootstrap.sh: handled errors exit 0 with a result line.
    print(f"EPHEMERA_RESULT=error:{reason}:{message}")
    return 0


def bootstrap(name: str, sub: str, job_dir: str) -> int:
    m = modes()
    d = remote_path(name, job_dir)
    if sub == "prepare":
        if "prepare_fail" in m:
            return err("no_gpu", "nvidia-smi not found (no NVIDIA driver?)")
        if not (d / "runtime.env").exists() or not (d / "bootstrap.sh").exists():
            return err("config", "runtime.env missing")
        print("EPHEMERA_GPU=NVIDIA L40S, 46068, 550.54.15")
        return ok()
    if sub == "start-model":
        (d / "secrets.env").unlink(missing_ok=True)
        (STATE / "model_started").write_text("1")
        return ok()
    if sub == "health":
        if "start_failed" in m:
            return err("start_failed", "pull:failed to pull vllm/vllm-openai:v0.30.0-cu129")
        if "model_crash" in m:
            return err("container_exited", "model container not running (exit code 1)")
        return ok()
    if sub == "infer":
        req = d / "request.json"
        if not req.exists():
            return err("no_request", "request.json missing (already consumed?)")
        payload = json.loads(req.read_text())
        (STATE / "last_request_keys.json").write_text(json.dumps(sorted(payload)))
        req.unlink()
        if "schema_400_once" in m and not (STATE / "schema_400").exists():
            (STATE / "schema_400").write_text("1")
            return err("http", "HTTP 400 unsupported JSON schema keyword")
        if "infer_http_error" in m:
            return err("http", "inference endpoint returned HTTP 500")
        content = "not json at all" if "infer_bad_json" in m else json.dumps(VALID_ANALYSIS)
        if "infer_bad_json_once" in m and not (STATE / "bad_once").exists():
            (STATE / "bad_once").write_text("1")
            content = "Sure! Here is the analysis you asked for."
        (d / "response.json").write_text(
            json.dumps({"choices": [{"message": {"content": content}}]})
        )
        return ok()
    if sub == "cleanup":
        if "remote_cleanup_fail" in m:
            return err("cleanup", "job dir still present")
        shutil.rmtree(d, ignore_errors=True)
        return ok()
    return 2


def cmd_exec(args: list[str]) -> int:
    args = [a for a in args if a != "--host"]
    name, command = args[0], args[1]
    if name not in load():
        print(f"instance {name!r} not found", file=sys.stderr)
        return 1
    parts = command.split()
    if command == "true":
        # Reproduces the real failure: first probe hangs until the caller's timeout.
        if "probe_hang_once" in modes() and not (STATE / "probe_hung").exists():
            (STATE / "probe_hung").write_text("1")
            time.sleep(3600)
        return 0
    if parts[:4] == ["mkdir", "-p", "-m", "700"]:
        remote_path(name, parts[4]).mkdir(parents=True, exist_ok=True)
        return 0
    if parts[0] == "bash" and parts[1].endswith("/bootstrap.sh"):
        return bootstrap(name, parts[2], parts[3])
    print(f"fake brev: unsupported remote command {command!r}", file=sys.stderr)
    return 127


def main(argv: list[str]) -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    REMOTE.mkdir(exist_ok=True)
    log_call(argv)
    cmd, rest = argv[0], argv[1:]
    if cmd == "search":
        return cmd_search(rest[1:] if rest and rest[0] == "gpu" else rest)
    handlers = {
        "create": cmd_create,
        "ls": cmd_ls,
        "delete": cmd_delete,
        "copy": cmd_copy,
        "exec": cmd_exec,
        "refresh": lambda a: 0,
        "set": lambda a: 0,
    }
    return handlers[cmd](rest)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
