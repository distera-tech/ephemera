"""Real-mode pipeline (Brev adapter + vLLM provider + bootstrap protocol) end-to-end,
against the fake `brev` CLI. This exercises every argument vector, file transfer and
remote sub-command Ephemera would issue to a real Brev instance — only the cloud
itself is emulated. Real-cloud validation is tracked separately (docs/deployment.md)."""

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.application.orchestrator import Orchestrator
from app.domain.errors import ErrorCode
from app.domain.models import GpuRequirements, instance_name_for
from app.domain.status import InstanceState, JobStatus
from app.infrastructure.brev.client import BrevClient
from app.infrastructure.brev.lifecycle import BrevComputeProvider
from app.infrastructure.documents.pymupdf_processor import PyMuPDFDocumentProcessor
from app.infrastructure.inference.vllm import VLLMProvider
from tests.conftest import make_pdf, set_fake_mode
from tests.fakes import config, submit

pytestmark = pytest.mark.integration


def real_stack(repo, workspace, state: Path, hf_token: str | None = "hf_testtoken_abcdefghijkl"):  # type: ignore[no-untyped-def]
    client = BrevClient(
        cli_path=str(state / "bin" / "brev"),
        home=state / "home",
        api_key=SecretStr("brev-key-xyz"),
        org=None,
        ssh_path=str(state / "bin" / "ssh"),
        scp_path=str(state / "bin" / "scp"),
    )
    compute = BrevComputeProvider(client)
    inference = VLLMProvider(
        model_id="meta-llama/Llama-3.1-8B-Instruct",
        image="vllm/vllm-openai:v0.30.0",
        max_model_len=16384,
        gpu_memory_utilization=0.9,
        hf_token=hf_token,
        transfer_timeout_s=30,
    )
    orch = Orchestrator(
        repo=repo,
        compute=compute,
        inference=inference,
        documents=PyMuPDFDocumentProcessor(max_pages=10, max_chars=20000, timeout_s=20),
        workspace=workspace,
        worker_id="real-test",
        config=config(
            gpu_requirements=GpuRequirements("L40S", 40, ("A100",), 8.0),
            max_job_runtime_s=60,
            destroy_verify_timeout_s=10,
        ),
    )
    return compute, orch


def calls(state: Path) -> list[list[str]]:
    return [json.loads(x) for x in (state / "calls.log").read_text().splitlines()]


async def run(repo, workspace, state, **kw):  # type: ignore[no-untyped-def]
    job_id = await submit(
        repo, workspace, make_pdf("Synthetic agreement between fictional parties."), **kw
    )
    job, _ = await repo.claim_next("real-test", 1, 1)
    compute, orch = real_stack(repo, workspace, state)
    status = await orch.run(job)
    return job_id, status, await repo.get(job_id), compute


async def test_real_mode_success_path(repo, workspace, fake_brev: Path) -> None:  # type: ignore[no-untyped-def]
    job_id, status, job, compute = await run(repo, workspace, fake_brev)
    assert status is JobStatus.COMPLETED, job.error_message
    name = instance_name_for(job_id)
    assert (
        job.instance_type == "l40s.1x"
        and job.actual_gpu == "NVIDIA L40S"
        and job.gpu_vram_mb == 46068
    )
    assert job.price_per_hour == 1.5
    assert job.instance_state is InstanceState.DESTROYED
    assert await compute.list_instances() == []

    argv = calls(fake_brev)
    verbs = [a[0] for a in argv]
    assert verbs.index("search") < verbs.index("create") < verbs.index("delete")
    execs = [a[-1] for a in argv if a[0] == "exec"]
    assert all(a[1] == "--host" for a in argv if a[0] in ("exec", "copy"))
    subs = [e.split()[2] for e in execs if "bootstrap.sh" in e]
    assert subs == ["prepare", "start-model", "health", "infer", "cleanup"]
    # Only fixed, trusted commands ever reach the instance.
    for cmd in execs:
        assert (
            cmd == "true"
            or cmd.startswith("mkdir -p -m 700 /tmp/ephemera/jobs/")
            or "/bootstrap.sh " in cmd
        )
    uploads = [
        a[-1].split(":", 1)[1].rsplit("/", 1)[1] for a in argv if a[0] == "copy" and ":" in a[-1]
    ]
    assert uploads == ["bootstrap.sh", "runtime.env", "secrets.env", "request.json"]

    # Structured-output request was sent; remote payload and secrets were removed.
    assert "response_format" in json.loads((fake_brev / "last_request_keys.json").read_text())
    assert not (fake_brev / "remote" / name).exists() or not any(
        (fake_brev / "remote" / name).rglob("*.json")
    )
    assert not workspace.exists(job_id)
    result = await repo.get_result(job_id)
    assert result is not None and result[0].requires_human_review


async def test_real_mode_hf_token_never_on_command_line(repo, workspace, fake_brev: Path) -> None:  # type: ignore[no-untyped-def]
    await run(repo, workspace, fake_brev)
    log = (fake_brev / "calls.log").read_text()
    assert "hf_testtoken" not in log
    env = json.loads((fake_brev / "last_env.json").read_text())
    assert "HF_TOKEN" not in env


async def test_real_mode_inference_failure_destroys(repo, workspace, fake_brev: Path) -> None:  # type: ignore[no-untyped-def]
    set_fake_mode(fake_brev, "infer_http_error")
    job_id, status, job, compute = await run(repo, workspace, fake_brev)
    assert status is JobStatus.FAILED and job.error_code == ErrorCode.INFERENCE_FAILED
    assert job.instance_state is InstanceState.DESTROYED
    assert await compute.list_instances() == []


async def test_real_mode_forced_failure_demo(repo, workspace, fake_brev: Path) -> None:  # type: ignore[no-untyped-def]
    job_id, status, job, compute = await run(repo, workspace, fake_brev, force_failure=True)
    assert status is JobStatus.FAILED and job.error_code == ErrorCode.INFERENCE_FORCED_FAILURE
    assert job.cleanup_report["succeeded"] and await compute.list_instances() == []


async def test_real_mode_model_crash(repo, workspace, fake_brev: Path) -> None:  # type: ignore[no-untyped-def]
    set_fake_mode(fake_brev, "model_crash")
    _, status, job, compute = await run(repo, workspace, fake_brev)
    assert status is JobStatus.FAILED and job.error_code == ErrorCode.MODEL_STARTUP_FAILED
    assert await compute.list_instances() == []


async def test_real_mode_bootstrap_failure(repo, workspace, fake_brev: Path) -> None:  # type: ignore[no-untyped-def]
    set_fake_mode(fake_brev, "prepare_fail")
    _, status, job, compute = await run(repo, workspace, fake_brev)
    assert status is JobStatus.FAILED and job.error_code == ErrorCode.BOOTSTRAP_FAILED
    assert "nvidia-smi not found" in (job.error_message or "")
    assert await compute.list_instances() == []


async def test_real_mode_partial_create_failure_is_destroyed(
    repo, workspace, fake_brev: Path
) -> None:  # type: ignore[no-untyped-def]
    set_fake_mode(fake_brev, "create_fail_after_partial")
    job_id, status, job, compute = await run(repo, workspace, fake_brev)
    assert status is JobStatus.FAILED and job.error_code == ErrorCode.PROVISIONING_FAILED
    assert ["delete", instance_name_for(job_id)] in calls(fake_brev)
    assert await compute.list_instances() == []


async def test_real_mode_invalid_json_retry_then_fail(repo, workspace, fake_brev: Path) -> None:  # type: ignore[no-untyped-def]
    set_fake_mode(fake_brev, "infer_bad_json")
    _, status, job, compute = await run(repo, workspace, fake_brev)
    assert status is JobStatus.FAILED and job.error_code == ErrorCode.INFERENCE_OUTPUT_INVALID
    infers = [a for a in calls(fake_brev) if a[0] == "exec" and " infer " in a[-1]]
    assert len(infers) == 2
    assert await compute.list_instances() == []


async def test_real_mode_destroy_failure_is_cleanup_failed(
    repo, workspace, fake_brev: Path
) -> None:  # type: ignore[no-untyped-def]
    set_fake_mode(fake_brev, "delete_fail")
    job_id, status, job, compute = await run(repo, workspace, fake_brev)
    assert status is JobStatus.CLEANUP_FAILED
    assert job.instance_state is InstanceState.DESTROY_FAILED
    # The leak is visible and blocks further provisioning until reconciled.
    set_fake_mode(fake_brev)
    await submit(repo, workspace, make_pdf())
    blocked, reason = await repo.claim_next("real-test", 5, 1)
    assert blocked is None and reason == "max_gpu_instances"


async def test_real_mode_model_start_failure_is_reported(repo, workspace, fake_brev: Path) -> None:  # type: ignore[no-untyped-def]
    set_fake_mode(fake_brev, "start_failed")
    _, status, job, compute = await run(repo, workspace, fake_brev)
    assert status is JobStatus.FAILED and job.error_code == ErrorCode.MODEL_STARTUP_FAILED
    assert "failed to pull" in (job.error_message or "")
    assert await compute.list_instances() == []


async def test_real_mode_schema_rejection_falls_back_without_response_format(
    repo, workspace, fake_brev: Path
) -> None:  # type: ignore[no-untyped-def]
    set_fake_mode(fake_brev, "schema_400_once")
    _, status, job, compute = await run(repo, workspace, fake_brev)
    assert status is JobStatus.COMPLETED, job.error_message
    # The retried request no longer carries response_format; output was still validated.
    assert "response_format" not in json.loads((fake_brev / "last_request_keys.json").read_text())
    assert await compute.list_instances() == []
