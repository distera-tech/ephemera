"""vLLM inference provider.

The model server runs inside a container on the ephemeral instance, published
on ``127.0.0.1`` only. Ephemera never opens a network path to it: requests are
staged as a file (``brev copy``), executed on the instance itself with ``curl``
against localhost (``brev exec``), and the response file is copied back. The
browser never talks to the inference server, and neither does the internet.
"""

from __future__ import annotations

import asyncio
import json
import shlex
import time
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.domain.errors import ErrorCode, InferenceError
from app.domain.models import BootstrapReport, ExecResult, InferenceRequest
from app.domain.ports import RuntimeContext
from app.infrastructure.inference.runtime import bootstrap_script_path
from app.infrastructure.storage.workspace import write_private

log = get_logger("inference.vllm")

SERVED_MODEL_NAME = "ephemera-model"
REMOTE_SCRIPT = "bootstrap.sh"
REQUEST_FILE = "request.json"
RESPONSE_FILE = "response.json"


def remote_command(sub: str, remote_dir: str) -> str:
    """Fixed bootstrap sub-commands only. ``remote_dir`` is built from a UUID."""
    if sub not in {"prepare", "start-model", "health", "infer", "cleanup"}:
        raise ValueError("unknown bootstrap sub-command")
    return f"bash {shlex.quote(remote_dir + '/' + REMOTE_SCRIPT)} {sub} {shlex.quote(remote_dir)}"


def parse_gpu_line(stdout: str) -> BootstrapReport:
    for line in stdout.splitlines():
        if line.startswith("EPHEMERA_GPU="):
            parts = [p.strip() for p in line.split("=", 1)[1].split(",")]
            name = parts[0] if parts else None
            vram = (
                int(float(parts[1]))
                if len(parts) > 1 and parts[1].replace(".", "").isdigit()
                else None
            )
            driver = parts[2] if len(parts) > 2 else None
            return BootstrapReport(gpu_name=name, vram_mb=vram, driver_version=driver)
    return BootstrapReport(
        gpu_name=None, vram_mb=None, driver_version=None, notes=("gpu line missing",)
    )


@dataclass(frozen=True, slots=True)
class RemoteOutcome:
    ok: bool
    reason: str
    message: str


def parse_outcome(result: ExecResult) -> RemoteOutcome:
    """Parse the bootstrap script's ``EPHEMERA_RESULT=`` line (see infra/gpu/bootstrap.sh).

    A non-zero exit or a missing result line means the command did not complete
    (SSH/transport failure), never a handled remote error.
    """
    if result.exit_code != 0:
        return RemoteOutcome(
            False, "transport", f"remote command did not complete (exit {result.exit_code})"
        )
    for line in reversed(result.stdout.splitlines()):
        if line.startswith("EPHEMERA_RESULT="):
            value = line.split("=", 1)[1].strip()
            if value == "ok":
                return RemoteOutcome(True, "ok", "")
            _, reason, message = [*value.split(":", 2), "", ""][:3]
            return RemoteOutcome(False, reason or "unknown", message[:300])
    return RemoteOutcome(False, "transport", "no result line from remote command")


class VLLMProvider:
    name = "vllm"
    is_simulated = False

    def __init__(
        self,
        *,
        model_id: str,
        image: str,
        max_model_len: int,
        gpu_memory_utilization: float,
        hf_token: str | None,
        transfer_timeout_s: float,
        port: int = 8000,
    ) -> None:
        self.model_id = model_id
        self.image = image
        self.max_model_len = max_model_len
        self.gpu_memory_utilization = gpu_memory_utilization
        self._hf_token = hf_token
        self.transfer_timeout_s = transfer_timeout_s
        self.port = port

    # ------------------------------------------------------------------ lifecycle
    async def bootstrap(self, ctx: RuntimeContext, timeout_s: float) -> BootstrapReport:
        deadline = time.monotonic() + timeout_s
        mk = await ctx.executor.execute(
            f"mkdir -p -m 700 {shlex.quote(ctx.remote_dir)}", timeout_s=min(120, timeout_s)
        )
        if mk.exit_code != 0:
            raise InferenceError(
                "could not create remote job directory", code=ErrorCode.BOOTSTRAP_FAILED
            )

        runtime_env = ctx.local_dir / "runtime.env"
        write_private(
            runtime_env,
            "\n".join(
                [
                    f"MODEL_ID={shlex.quote(self.model_id)}",
                    f"VLLM_IMAGE={shlex.quote(self.image)}",
                    f"MAX_MODEL_LEN={int(self.max_model_len)}",
                    f"GPU_MEMORY_UTILIZATION={float(self.gpu_memory_utilization)}",
                    f"SERVED_MODEL_NAME={SERVED_MODEL_NAME}",
                    f"PORT={int(self.port)}",
                ]
            )
            + "\n",
        )
        uploads = [(bootstrap_script_path(), REMOTE_SCRIPT), (runtime_env, "runtime.env")]
        secrets_env: Path | None = None
        if self._hf_token:
            # Secret travels as a 0600 file over SSH, never on a command line; the
            # bootstrap script deletes it right after the model container starts.
            secrets_env = ctx.local_dir / "secrets.env"
            write_private(secrets_env, f"HF_TOKEN={self._hf_token}\n")
            uploads.append((secrets_env, "secrets.env"))
        try:
            for local, remote_name in uploads:
                await ctx.executor.upload(
                    local,
                    f"{ctx.remote_dir}/{remote_name}",
                    timeout_s=max(10, deadline - time.monotonic()),
                )
        finally:
            if secrets_env is not None:
                secrets_env.unlink(missing_ok=True)

        result = await ctx.executor.execute(
            remote_command("prepare", ctx.remote_dir),
            timeout_s=max(10, deadline - time.monotonic()),
        )
        outcome = parse_outcome(result)
        if not outcome.ok:
            raise InferenceError(
                f"environment verification failed: {outcome.message}",
                code=ErrorCode.BOOTSTRAP_FAILED,
            )
        return parse_gpu_line(result.stdout)

    async def start(self, ctx: RuntimeContext, timeout_s: float) -> None:
        result = await ctx.executor.execute(
            remote_command("start-model", ctx.remote_dir), timeout_s=timeout_s
        )
        outcome = parse_outcome(result)
        if not outcome.ok:
            raise InferenceError(
                f"model server failed to start: {outcome.message}",
                code=ErrorCode.MODEL_STARTUP_FAILED,
            )

    async def wait_ready(self, ctx: RuntimeContext, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        attempt = 0
        while time.monotonic() < deadline:
            result = await ctx.executor.execute(
                remote_command("health", ctx.remote_dir), timeout_s=60
            )
            outcome = parse_outcome(result)
            if outcome.ok:
                return
            if outcome.reason == "container_exited":
                raise InferenceError(
                    f"{outcome.message} (check model id, licence acceptance / HF_TOKEN, VRAM)",
                    code=ErrorCode.MODEL_STARTUP_FAILED,
                )
            # not_ready or a transient transport failure: keep polling until the deadline
            await asyncio.sleep(min(20.0, 3.0 * (1.5**attempt)))
            attempt += 1
        raise InferenceError(
            f"model not ready within {timeout_s:.0f}s", code=ErrorCode.MODEL_STARTUP_TIMEOUT
        )

    async def transfer(
        self, ctx: RuntimeContext, request: InferenceRequest, timeout_s: float
    ) -> None:
        payload = {
            "model": SERVED_MODEL_NAME,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "document_analysis",
                    "schema": request.json_schema,
                    "strict": True,
                },
            },
        }
        local = ctx.local_dir / REQUEST_FILE
        write_private(local, json.dumps(payload))
        try:
            await ctx.executor.upload(
                local, f"{ctx.remote_dir}/{REQUEST_FILE}", timeout_s=timeout_s
            )
        finally:
            local.unlink(missing_ok=True)

    async def infer(self, ctx: RuntimeContext, timeout_s: float) -> str:
        result = await ctx.executor.execute(
            remote_command("infer", ctx.remote_dir), timeout_s=timeout_s
        )
        outcome = parse_outcome(result)
        if not outcome.ok:
            raise InferenceError(
                f"inference failed: {outcome.message}", code=ErrorCode.INFERENCE_FAILED
            )
        local = ctx.local_dir / RESPONSE_FILE
        try:
            await ctx.executor.download(
                f"{ctx.remote_dir}/{RESPONSE_FILE}", local, timeout_s=self.transfer_timeout_s
            )
            body = json.loads(local.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise InferenceError(
                "could not read inference response", code=ErrorCode.INFERENCE_FAILED
            ) from None
        finally:
            local.unlink(missing_ok=True)
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise InferenceError(
                "unexpected response shape from model server", code=ErrorCode.INFERENCE_FAILED
            ) from None
        if not isinstance(content, str):
            raise InferenceError("empty model response", code=ErrorCode.INFERENCE_OUTPUT_INVALID)
        return content

    async def cleanup(self, ctx: RuntimeContext, timeout_s: float) -> None:
        result = await ctx.executor.execute(
            remote_command("cleanup", ctx.remote_dir), timeout_s=timeout_s
        )
        outcome = parse_outcome(result)
        if not outcome.ok:
            raise InferenceError(
                f"remote cleanup failed: {outcome.message}", code=ErrorCode.CLEANUP_FAILED
            )
