"""BrevClient / BrevComputeProvider against a fake `brev` executable that mirrors
the verified CLI syntax and JSON shapes of brev-cli v0.6.335."""

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.domain.errors import ComputeError, ErrorCode
from app.domain.models import GpuRequirements
from app.infrastructure.brev.client import BrevClient
from app.infrastructure.brev.exceptions import BrevCLINotFoundError, BrevTimeoutError
from app.infrastructure.brev.lifecycle import BrevComputeProvider
from tests.conftest import set_fake_mode

NAME = "ephemera-0123456789ab"
REQ = GpuRequirements(
    preferred_gpu="L40S", min_vram_gb=40, fallback_gpus=("A100",), min_compute_capability=8.0
)


def client(state: Path, **kw: object) -> BrevClient:
    args: dict[str, object] = {
        "cli_path": str(state / "bin" / "brev"),
        "home": state / "home",
        "api_key": SecretStr("brev-secret-key-123"),
        "org": "demo-org",
    }
    args.update(kw)
    return BrevClient(**args)  # type: ignore[arg-type]


def calls(state: Path) -> list[list[str]]:
    return [json.loads(line) for line in (state / "calls.log").read_text().splitlines()]


async def test_search_and_selection_prefers_l40s(fake_brev: Path) -> None:
    provider = BrevComputeProvider(client(fake_brev))
    selection = await provider.select_gpu(REQ)
    assert [c.instance_type for c in selection.candidates] == ["l40s.1x", "a100.1x"]
    assert selection.candidates[0].price_per_hour == 1.5
    search = next(c for c in calls(fake_brev) if c[0] == "search")
    assert search[:3] == ["search", "gpu", "--json"]
    assert "--min-total-vram" in search


async def test_org_is_set_once(fake_brev: Path) -> None:
    c = client(fake_brev)
    await c.list_instances()
    await c.list_instances()
    assert [x for x in calls(fake_brev) if x[0] == "set"] == [["set", "demo-org"]]


async def test_full_instance_lifecycle(fake_brev: Path) -> None:
    provider = BrevComputeProvider(client(fake_brev))
    selection = await provider.select_gpu(REQ)
    info = await provider.provision(NAME, selection, timeout_s=120)
    assert info.is_running and info.instance_type == "l40s.1x" and info.gpu_name == "L40S"
    create = next(c for c in calls(fake_brev) if c[0] == "create")
    assert create[:4] == ["create", NAME, "--type", "l40s.1x,a100.1x"]
    assert "--jupyter=false" in create
    ready = await provider.wait_until_ready(NAME, timeout_s=30)
    assert ready.shell_ready
    assert [i.name for i in await provider.list_instances()] == [NAME]
    await provider.destroy(NAME, timeout_s=30)
    assert await provider.verify_destroyed(NAME, timeout_s=10)
    assert await provider.list_instances() == []


async def test_delete_is_idempotent_when_already_gone(fake_brev: Path) -> None:
    c = client(fake_brev)
    assert await c.delete(NAME, timeout_s=30) is False


async def test_delete_failure_raises(fake_brev: Path) -> None:
    provider = BrevComputeProvider(client(fake_brev))
    await provider.provision(NAME, await provider.select_gpu(REQ), timeout_s=60)
    set_fake_mode(fake_brev, "delete_fail")
    with pytest.raises(ComputeError):
        await provider.destroy(NAME, timeout_s=30)
    assert await provider.verify_destroyed(NAME, timeout_s=0.5) is False


async def test_provisioning_failure_is_structured(fake_brev: Path) -> None:
    set_fake_mode(fake_brev, "create_fail")
    provider = BrevComputeProvider(client(fake_brev))
    with pytest.raises(ComputeError) as err:
        await provider.provision(NAME, await provider.select_gpu(REQ), timeout_s=60)
    assert err.value.code is ErrorCode.PROVISIONING_FAILED
    assert "no capacity" in err.value.message


async def test_timeout_kills_cli_and_is_structured(fake_brev: Path) -> None:
    set_fake_mode(fake_brev, "create_hang")
    provider = BrevComputeProvider(client(fake_brev))
    selection = await provider.select_gpu(REQ)
    with pytest.raises(ComputeError) as err:
        await provider.provision(NAME, selection, timeout_s=1.5)
    assert err.value.code is ErrorCode.PROVISIONING_TIMEOUT


async def test_raw_timeout_error(fake_brev: Path) -> None:
    set_fake_mode(fake_brev, "create_hang")
    with pytest.raises(BrevTimeoutError):
        await client(fake_brev).run(["create", NAME, "--type", "x"], timeout_s=0.5)


async def test_environment_is_sanitized(fake_brev: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_should_never_reach_brev")
    monkeypatch.setenv("DATABASE_URL", "postgresql://secret")
    await client(fake_brev).list_instances()
    env = json.loads((fake_brev / "last_env.json").read_text())
    assert "HF_TOKEN" not in env
    assert "DATABASE_URL" not in env
    assert env["BREV_API_KEY"] == "brev-secret-key-123"
    assert env["BREV_NO_ANALYTICS"] == "1"
    assert env["HOME"] == str(fake_brev / "home")


async def test_error_messages_are_redacted(fake_brev: Path) -> None:
    from app.core.logging import register_secrets

    register_secrets(["no capacity"])  # pretend the stderr contained a secret
    set_fake_mode(fake_brev, "create_fail")
    provider = BrevComputeProvider(client(fake_brev))
    with pytest.raises(ComputeError) as err:
        await provider.provision(NAME, await provider.select_gpu(REQ), timeout_s=60)
    assert "no capacity" not in err.value.message


async def test_missing_cli_is_configuration_error(tmp_path: Path) -> None:
    c = BrevClient(cli_path=str(tmp_path / "nope"), home=tmp_path, api_key=None, org=None)
    with pytest.raises(BrevCLINotFoundError):
        await c.list_instances()


async def test_list_ignores_foreign_instances(fake_brev: Path) -> None:
    (fake_brev / "instances.json").write_text(
        json.dumps(
            {
                "prod-db": {"name": "prod-db", "status": "RUNNING"},
                NAME: {"name": NAME, "status": "RUNNING"},
            }
        )
    )
    provider = BrevComputeProvider(client(fake_brev))
    assert [i.name for i in await provider.list_instances()] == [NAME]


async def test_ssh_probe_timeout_is_retried_not_fatal(fake_brev: Path) -> None:
    """Regression (real Brev run 2026-09-27): a hanging first `brev exec … true` failed the job."""
    provider = BrevComputeProvider(client(fake_brev), ssh_probe_timeout_s=1)
    await provider.provision(NAME, await provider.select_gpu(REQ), timeout_s=60)
    set_fake_mode(fake_brev, "probe_hang_once")
    info = await provider.wait_until_ready(NAME, timeout_s=30)
    assert info.is_running
    argv = calls(fake_brev)
    assert ["refresh"] in argv  # ssh aliases written before probing
    assert sum(1 for c in argv if c[0] == "exec") >= 2


async def test_brev_env_disables_ssh_agent(fake_brev: Path) -> None:
    await client(fake_brev).list_instances()
    env = json.loads((fake_brev / "last_env.json").read_text())
    assert env["SSH_AUTH_SOCK"] == "/dev/null"


def test_real_mode_rejects_brev_home_that_ssh_cannot_see(tmp_path: Path) -> None:
    from app.core.config import Settings

    bad = Settings(ephemera_mode="real", brev_api_key="bak-x", brev_home=tmp_path, _env_file=None)  # type: ignore[call-arg]
    assert any("BREV_HOME" in p for p in bad.configuration_problems())
    good = Settings(ephemera_mode="real", brev_api_key="bak-x", _env_file=None)  # type: ignore[call-arg]
    assert not any("BREV_HOME" in p for p in good.configuration_problems())


async def test_waits_for_brev_setup_before_probing(fake_brev: Path) -> None:
    """Regression (real run 2026-09-27): bootstrap started while Brev was still setting up."""
    provider = BrevComputeProvider(client(fake_brev))
    await provider.provision(NAME, await provider.select_gpu(REQ), timeout_s=60)
    set_fake_mode(fake_brev, "build_pending")
    (fake_brev / "calls.log").write_text("")
    info = await provider.wait_until_ready(NAME, timeout_s=60)
    assert info.is_setup_complete
    argv = calls(fake_brev)
    first_exec = next(i for i, c in enumerate(argv) if c[0] == "exec")
    assert sum(1 for c in argv[:first_exec] if c[0] == "ls") >= 3  # 2x BUILDING, then COMPLETED


async def test_brev_setup_failure_is_structured(fake_brev: Path) -> None:
    provider = BrevComputeProvider(client(fake_brev))
    await provider.provision(NAME, await provider.select_gpu(REQ), timeout_s=60)
    set_fake_mode(fake_brev, "build_failed")
    with pytest.raises(ComputeError) as err:
        await provider.wait_until_ready(NAME, timeout_s=30)
    assert "CREATE_FAILED" in err.value.message
