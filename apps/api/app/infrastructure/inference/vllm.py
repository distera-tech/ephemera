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
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.core.logging import get_logger
from app.domain.errors import EphemeraError, ErrorCode, InferenceError
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
            return RemoteOutcome(False, reason or "unknown", message[:700])
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
        inference_timeout_s: float = 240,
        min_free_disk_gb: int = 50,
        port: int = 8000,
        image_cuda13: str = "vllm/vllm-openai:v0.30.0",
        image_cuda12: str = "vllm/vllm-openai:v0.19.1",
    ) -> None:
        self.model_id = model_id
        self.image = image  # "auto" = chosen on the instance from the NVIDIA driver version
        self.image_cuda13 = image_cuda13
        self.image_cuda12 = image_cuda12
        self.max_model_len = max_model_len
        self.gpu_memory_utilization = gpu_memory_utilization
        self._hf_token = hf_token
        self.transfer_timeout_s = transfer_timeout_s
        self.inference_timeout_s = inference_timeout_s
        self.min_free_disk_gb = min_free_disk_gb
        self.port = port
        # Flipped off (per worker) if the server rejects `response_format` with HTTP 400; the
        # prompt still demands JSON and the output is validated either way.
        self._structured_output = True
        self._staged: dict[uuid.UUID, InferenceRequest] = {}

    # ------------------------------------------------------------------ lifecycle
    async def bootstrap(self, ctx: RuntimeContext, timeout_s: float) -> BootstrapReport:
        deadline = time.monotonic() + timeout_s
        log.info("remote.step", extra={"step": "mkdir"})
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
                    f"VLLM_IMAGE_CUDA13={shlex.quote(self.image_cuda13)}",
                    f"VLLM_IMAGE_CUDA12={shlex.quote(self.image_cuda12)}",
                    f"MAX_MODEL_LEN={int(self.max_model_len)}",
                    f"GPU_MEMORY_UTILIZATION={float(self.gpu_memory_utilization)}",
                    f"SERVED_MODEL_NAME={SERVED_MODEL_NAME}",
                    f"PORT={int(self.port)}",
                    # curl on the instance must give up before our own inference timeout.
                    f"INFER_TIMEOUT={max(30, int(self.inference_timeout_s) - 15)}",
                    f"MIN_FREE_DISK_GB={int(self.min_free_disk_gb)}",
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
                log.info("remote.step", extra={"step": f"upload {remote_name}"})
                await ctx.executor.upload(
                    local,
                    f"{ctx.remote_dir}/{remote_name}",
                    timeout_s=max(10, deadline - time.monotonic()),
                )
        finally:
            if secrets_env is not None:
                secrets_env.unlink(missing_ok=True)

        log.info("remote.step", extra={"step": "prepare"})
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
        log.info("remote.step", extra={"step": "start-model"})
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
        phase = ""
        while time.monotonic() < deadline:
            try:
                result = await ctx.executor.execute(
                    remote_command("health", ctx.remote_dir), timeout_s=90
                )
                outcome = parse_outcome(result)
            except EphemeraError as exc:  # transient SSH/CLI failure: keep polling
                outcome = RemoteOutcome(False, "transport", exc.message)
            if outcome.ok:
                log.info("model.phase", extra={"phase": "ready"})
                return
            if outcome.reason == "start_failed":
                raise InferenceError(
                    f"model server failed to start: {outcome.message}",
                    code=ErrorCode.MODEL_STARTUP_FAILED,
                )
            if outcome.reason == "container_exited":
                raise InferenceError(
                    f"{outcome.message} (check model id, licence acceptance / HF_TOKEN, VRAM, disk)",
                    code=ErrorCode.MODEL_STARTUP_FAILED,
                )
            if outcome.message != phase:  # pulling image → starting → loading weights
                phase = outcome.message
                log.info("model.phase", extra={"phase": phase, "reason": outcome.reason})
            await asyncio.sleep(min(20.0, 3.0 * (1.5**attempt)))
            attempt += 1
        raise InferenceError(
            f"model not ready within {timeout_s:.0f}s", code=ErrorCode.MODEL_STARTUP_TIMEOUT
        )

    async def transfer(
        self, ctx: RuntimeContext, request: InferenceRequest, timeout_s: float
    ) -> None:
        self._staged[ctx.job_id] = request  # kept in memory for a schema-less retry only
        await self._upload_payload(ctx, request, timeout_s)

    def _payload(self, request: InferenceRequest) -> dict[str, object]:
        payload: dict[str, object] = {
            "model": SERVED_MODEL_NAME,
            "messages": [
                {"role": "system", "content": request.system_prompt},
                {"role": "user", "content": request.user_prompt},
            ],
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
        }
        if self._structured_output:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "document_analysis",
                    "schema": request.json_schema,
                    "strict": True,
                },
            }
        return payload

    async def _upload_payload(
        self, ctx: RuntimeContext, request: InferenceRequest, timeout_s: float
    ) -> None:
        local = ctx.local_dir / REQUEST_FILE
        write_private(local, json.dumps(self._payload(request)))
        try:
            await ctx.executor.upload(
                local, f"{ctx.remote_dir}/{REQUEST_FILE}", timeout_s=timeout_s
            )
        finally:
            local.unlink(missing_ok=True)

    async def infer(self, ctx: RuntimeContext, timeout_s: float) -> str:
        log.info("remote.step", extra={"step": "infer"})
        result = await ctx.executor.execute(
            remote_command("infer", ctx.remote_dir), timeout_s=timeout_s
        )
        outcome = parse_outcome(result)
        staged = self._staged.get(ctx.job_id)
        if (
            not outcome.ok
            and outcome.reason == "http"
            and outcome.message.startswith("HTTP 400")
            and self._structured_output
            and staged is not None
        ):
            # Some schema keywords may be unsupported by the structured-output backend.
            log.warning("vllm.structured_output_rejected", extra={"detail": outcome.message[:200]})
            self._structured_output = False
            await self._upload_payload(ctx, staged, self.transfer_timeout_s)
            log.info("remote.step", extra={"step": "infer (without response_format)"})
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
        self._staged.pop(ctx.job_id, None)
        log.info("remote.step", extra={"step": "cleanup"})
        result = await ctx.executor.execute(
            remote_command("cleanup", ctx.remote_dir), timeout_s=timeout_s
        )
        outcome = parse_outcome(result)
        if not outcome.ok:
            raise InferenceError(
                f"remote cleanup failed: {outcome.message}", code=ErrorCode.CLEANUP_FAILED
            )
