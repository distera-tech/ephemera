"""TEST 10 (secret leakage) + filename / command-injection / path controls."""

import io
import json
import logging
import uuid

import pytest

from app.api.uploads import sanitize_display_name
from app.core.logging import JsonFormatter, get_logger, redact, register_secrets
from app.domain.models import instance_name_for
from app.infrastructure.brev import commands
from app.infrastructure.brev.exceptions import UnsafeInstanceNameError
from app.infrastructure.inference.vllm import remote_command
from app.infrastructure.storage.workspace import JobWorkspace


def _capture() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger(f"test.{uuid.uuid4().hex}")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, stream


def test_registered_secrets_never_reach_logs() -> None:
    secret = "brev_live_" + uuid.uuid4().hex
    register_secrets([secret])
    logger, stream = _capture()
    logger.info("calling brev with %s", secret, extra={"component": "t", "detail": f"key={secret}"})
    try:
        raise RuntimeError(f"failed with token {secret}")
    except RuntimeError:
        logger.exception("boom")
    out = stream.getvalue()
    assert secret not in out
    assert "[REDACTED]" in out
    for line in out.strip().splitlines():
        json.loads(line)  # structured JSON


@pytest.mark.parametrize(
    "text",
    [
        "HF_TOKEN=hf_abcdefghijklmnopqrstuvwxyz",
        "Authorization: Bearer abcdef1234567890",
        "nvapi-AbCdEf0123456789xyz",
        '{"api_key": "sk-1234567890"}',
    ],
)
def test_pattern_redaction(text: str) -> None:
    out = redact(text)
    assert "[REDACTED]" in out
    for secret in ("abcdefghijklmnop", "abcdef1234567890", "AbCdEf0123456789", "sk-1234567890"):
        assert secret not in out


def test_log_records_include_structured_fields() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    base = logging.getLogger("ephemera.fieldtest")
    base.addHandler(handler)
    base.setLevel(logging.INFO)
    get_logger("fieldtest").info("step.done", extra={"job_id": "j1", "duration_ms": 12})
    payload = json.loads(stream.getvalue())
    assert payload["component"] == "fieldtest"
    assert payload["job_id"] == "j1"
    assert payload["duration_ms"] == 12
    assert {"timestamp", "level", "event"} <= payload.keys()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "passwd"),
        ("C:\\Windows\\system32\\evil.pdf", "evil.pdf"),
        ("contract; rm -rf ~.pdf", "contract_ rm -rf _.pdf"),
        ("$(curl evil).pdf", "_(curl evil).pdf"),
        ("", "document.pdf"),
        ("..", "document.pdf"),
        ("a" * 500 + ".pdf", "a" * 120),
    ],
)
def test_filename_is_sanitized_for_display(raw: str, expected: str) -> None:
    assert sanitize_display_name(raw) == expected


def test_filename_never_reaches_brev_arguments() -> None:
    job_id = uuid.uuid4()
    name = instance_name_for(job_id)
    argv = commands.create(name, ["l40s.1x"], 300)
    assert all("pdf" not in a for a in argv)
    assert commands.ssh_exec(
        name,
        remote_command("infer", f"/tmp/ephemera/jobs/{job_id}"),
        host=True,
        connect_timeout_s=60,
    )[-1] == (f"bash /tmp/ephemera/jobs/{job_id}/bootstrap.sh infer /tmp/ephemera/jobs/{job_id}")


@pytest.mark.parametrize(
    "bad",
    ["prod-database", "ephemera-", "ephemera-zzzzzzzzzzzz", "--all", "ephemera-abc123abc123 x"],
)
def test_brev_mutations_refuse_foreign_instances(bad: str) -> None:
    for build in (
        lambda: commands.delete(bad),
        lambda: commands.ssh_exec(bad, "true", host=True, connect_timeout_s=60),
        lambda: commands.scp_to(bad, "/tmp/a", "/tmp/b", host=True, connect_timeout_s=60),
        lambda: commands.create(bad, ["t"], 60),
    ):
        with pytest.raises(UnsafeInstanceNameError):
            build()


def test_no_delete_all_builder_exists() -> None:
    assert not any(
        "all" in n.lower() for n in dir(commands) if not n.startswith("_") and "delete" in n
    )


@pytest.mark.parametrize(
    "path", ["/tmp/a b", "/tmp/a;b", "relative/path", "/tmp/../etc", "/tmp/x:y", "/tmp/$(id)"]
)
def test_copy_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValueError):
        commands.scp_to(
            "ephemera-abcdefabcdef", path, "/tmp/ephemera/jobs/x", host=True, connect_timeout_s=60
        )


def test_exec_rejects_multiline_commands() -> None:
    with pytest.raises(ValueError):
        commands.ssh_exec(
            "ephemera-abcdefabcdef", "true\nrm -rf /", host=True, connect_timeout_s=60
        )


def test_remote_commands_are_a_fixed_set() -> None:
    with pytest.raises(ValueError):
        remote_command("rm -rf /", "/tmp/ephemera/jobs/x")


def test_workspace_paths_derive_from_uuid_only(tmp_path) -> None:  # type: ignore[no-untyped-def]
    ws = JobWorkspace(tmp_path)
    job_id = uuid.uuid4()
    assert ws.dir_for(job_id) == (tmp_path / str(job_id)).resolve()
    d = ws.create(job_id)
    assert oct(d.stat().st_mode & 0o777) == "0o700"
    assert ws.delete(job_id) and not d.exists()
