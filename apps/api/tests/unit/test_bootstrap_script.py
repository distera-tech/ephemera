"""Runs the real infra/gpu/bootstrap.sh with stubbed nvidia-smi / docker / curl."""

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from app.domain.models import ExecResult
from app.infrastructure.inference.runtime import bootstrap_script_path
from app.infrastructure.inference.vllm import parse_gpu_line, parse_outcome

STUBS = {
    "nvidia-smi": 'echo "NVIDIA L40S, 46068, 550.54.15"',
    "docker": r"""
echo "$@" >> "$STUB_LOG"
case "$1" in
  info) [ "$2" = "--format" ] && echo '{"nvidia":{"path":"nvidia-container-runtime"}}'; exit 0 ;;
  inspect) case "$3" in *Running*) echo "${STUB_RUNNING:-true}" ;; *) echo 1 ;; esac ;;
  pull) exit "${STUB_PULL_EXIT:-0}" ;;
  logs) echo "ERROR: gated repo, access to model is restricted" ;;
  *) exit 0 ;;
esac
""",
    "curl": r"""
echo "$@" >> "$STUB_LOG"
out=""; prev=""
for a in "$@"; do [ "$prev" = "-o" ] && out="$a"; prev="$a"; done
case "$*" in
  */health*) [ "${STUB_HEALTH:-ok}" = ok ] && exit 0 || exit 7 ;;
  */v1/chat/completions*) if [ "${STUB_HTTP:-200}" = 200 ]; then echo '{"choices":[{"message":{"content":"{}"}}]}' > "$out"; else echo '{"object":"error","message":"unsupported schema keyword","code":400}' > "$out"; fi; printf "${STUB_HTTP:-200}" ;;
esac
""",
}


@pytest.fixture
def env(tmp_path: Path):  # type: ignore[no-untyped-def]
    if shutil.which("bash") is None:
        pytest.skip("bash required")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in STUBS.items():
        p = bindir / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)
    job_dir = Path(f"/tmp/ephemera/jobs/{uuid.uuid4()}")
    job_dir.mkdir(parents=True)
    (job_dir / "runtime.env").write_text(
        "MODEL_ID=org/model\nVLLM_IMAGE=vllm/vllm-openai:v0.30.0\nMAX_MODEL_LEN=8192\n"
        "GPU_MEMORY_UTILIZATION=0.9\nSERVED_MODEL_NAME=ephemera-model\nPORT=8000\nMIN_FREE_DISK_GB=1\n"
    )
    log = tmp_path / "calls.log"
    log.touch()
    base = {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(tmp_path), "STUB_LOG": str(log)}
    yield job_dir, base, log
    shutil.rmtree(job_dir, ignore_errors=True)


def run(sub: str, job_dir: Path | str, base: dict[str, str], **extra: str) -> ExecResult:
    p = subprocess.run(
        ["bash", str(bootstrap_script_path()), sub, str(job_dir)],
        capture_output=True,
        text=True,
        env={**base, **extra},
        timeout=30,
    )
    return ExecResult(p.returncode, p.stdout, p.stderr, 0)


def test_prepare_reports_gpu(env) -> None:  # type: ignore[no-untyped-def]
    job_dir, base, _ = env
    r = run("prepare", job_dir, base)
    assert parse_outcome(r).ok
    report = parse_gpu_line(r.stdout)
    assert report.gpu_name == "NVIDIA L40S" and report.vram_mb == 46068


def test_prepare_without_gpu_is_handled_error(env) -> None:  # type: ignore[no-untyped-def]
    job_dir, base, _ = env
    os.remove(Path(base["PATH"].split(":")[0]) / "nvidia-smi")
    r = run("prepare", job_dir, {**base, "PATH": base["PATH"].split(":")[0] + ":/usr/bin:/bin"})
    if shutil.which("nvidia-smi", path="/usr/bin:/bin"):
        pytest.skip("host has a real nvidia-smi")
    outcome = parse_outcome(r)
    assert r.exit_code == 0 and not outcome.ok and outcome.reason == "no_gpu"


def wait_status(job_dir: Path, want: str, timeout: float = 10) -> str:
    import time

    deadline = time.monotonic() + timeout
    status = ""
    while time.monotonic() < deadline:
        f = job_dir / "start.status"
        status = f.read_text().strip() if f.exists() else ""
        if status == want or status.startswith("failed"):
            return status
        time.sleep(0.1)
    return status


def test_start_model_runs_in_background_localhost_only_and_deletes_secrets(env) -> None:  # type: ignore[no-untyped-def]
    job_dir, base, log = env
    (job_dir / "secrets.env").write_text("HF_TOKEN=hf_x\n")
    assert parse_outcome(run("start-model", job_dir, base)).ok  # returns immediately
    assert wait_status(job_dir, "started") == "started"
    run_line = next(line for line in log.read_text().splitlines() if line.startswith("run "))
    assert "-p 127.0.0.1:8000:8000" in run_line
    assert "--env-file" in run_line and "hf_x" not in run_line
    assert not (job_dir / "secrets.env").exists()
    assert parse_outcome(run("health", job_dir, base)).ok
    # idempotent: a second start (e.g. a brev exec retry) does not relaunch
    assert parse_outcome(run("start-model", job_dir, base)).ok
    assert sum(1 for line in log.read_text().splitlines() if line.startswith("run ")) == 1


def test_pull_failure_surfaces_through_health(env) -> None:  # type: ignore[no-untyped-def]
    job_dir, base, _ = env
    (job_dir / "secrets.env").write_text("HF_TOKEN=hf_x\n")
    assert parse_outcome(run("start-model", job_dir, base, STUB_PULL_EXIT="1")).ok
    assert wait_status(job_dir, "started").startswith("failed:pull")
    outcome = parse_outcome(run("health", job_dir, base))
    assert not outcome.ok and outcome.reason == "start_failed"
    assert "failed to pull" in outcome.message
    assert not (job_dir / "secrets.env").exists()  # secret removed even on failure


def test_health_before_start_is_an_error_not_a_hang(env) -> None:  # type: ignore[no-untyped-def]
    job_dir, base, _ = env
    assert parse_outcome(run("health", job_dir, base)).reason == "start_failed"


def test_prepare_refuses_too_small_disk(env) -> None:  # type: ignore[no-untyped-def]
    job_dir, base, _ = env
    with (job_dir / "runtime.env").open("a") as fh:
        fh.write("MIN_FREE_DISK_GB=999999\n")
    outcome = parse_outcome(run("prepare", job_dir, base))
    assert not outcome.ok and outcome.reason == "no_disk"


def test_health_states(env) -> None:  # type: ignore[no-untyped-def]
    job_dir, base, _ = env
    status = job_dir / "start.status"
    status.write_text("pulling")
    assert parse_outcome(run("health", job_dir, base)).reason == "not_ready"
    status.write_text("started")
    assert parse_outcome(run("health", job_dir, base)).ok
    not_ready = parse_outcome(run("health", job_dir, base, STUB_HEALTH="down"))
    assert not not_ready.ok and not_ready.reason == "not_ready"
    dead = parse_outcome(run("health", job_dir, base, STUB_RUNNING="false"))
    assert not dead.ok and dead.reason == "container_exited"
    assert "gated repo" in dead.message  # container log hint included
    # all handled outcomes exit 0, so `brev exec` never retries them
    assert run("health", job_dir, base, STUB_HEALTH="down").exit_code == 0


def test_infer_consumes_payload(env) -> None:  # type: ignore[no-untyped-def]
    job_dir, base, _ = env
    (job_dir / "request.json").write_text(json.dumps({"model": "x"}))
    assert parse_outcome(run("infer", job_dir, base)).ok
    assert not (job_dir / "request.json").exists()
    assert json.loads((job_dir / "response.json").read_text())["choices"]
    again = parse_outcome(run("infer", job_dir, base))  # a brev retry cannot re-send the document
    assert not again.ok and again.reason == "no_request"


def test_infer_http_error(env) -> None:  # type: ignore[no-untyped-def]
    job_dir, base, _ = env
    (job_dir / "request.json").write_text("{}")
    outcome = parse_outcome(run("infer", job_dir, base, STUB_HTTP="400"))
    assert not outcome.ok and outcome.reason == "http"
    assert (
        outcome.message.startswith("HTTP 400") and "unsupported schema keyword" in outcome.message
    )
    assert not (job_dir / "request.json").exists()


def test_cleanup_removes_job_dir(env) -> None:  # type: ignore[no-untyped-def]
    job_dir, base, _ = env
    assert parse_outcome(run("cleanup", job_dir, base)).ok
    assert not job_dir.exists()


@pytest.mark.parametrize(
    "bad", ["/etc", "/tmp/ephemera/jobs/../../etc", "/tmp/ephemera/jobs/not-a-uuid", ""]
)
def test_rejects_job_dirs_outside_scheme(env, bad: str) -> None:  # type: ignore[no-untyped-def]
    _, base, _ = env
    outcome = parse_outcome(run("cleanup", bad, base))
    assert not outcome.ok and outcome.reason == "usage"


def test_unknown_subcommand(env) -> None:  # type: ignore[no-untyped-def]
    job_dir, base, _ = env
    assert parse_outcome(run("rm-everything", job_dir, base)).reason == "usage"


def test_transport_failure_is_distinguished() -> None:
    assert parse_outcome(ExecResult(255, "", "ssh: connect refused", 0)).reason == "transport"
    assert parse_outcome(ExecResult(0, "garbage", "", 0)).reason == "transport"


def test_prepare_reports_unreachable_docker_instead_of_hanging(env) -> None:  # type: ignore[no-untyped-def]
    job_dir, base, _ = env
    bindir = Path(base["PATH"].split(":")[0])
    (bindir / "docker").write_text("#!/bin/sh\nexit 1\n")
    (bindir / "sudo").write_text("#!/bin/sh\nexit 1\n")
    (bindir / "sudo").chmod(0o755)
    r = run(
        "prepare", job_dir, base, EPHEMERA_DOCKER_WAIT_TRIES="1", EPHEMERA_DOCKER_WAIT_SLEEP="0"
    )
    outcome = parse_outcome(r)
    assert r.exit_code == 0 and not outcome.ok and outcome.reason == "no_docker"
