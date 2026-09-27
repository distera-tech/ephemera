"""Runtime configuration (environment variables only — never hardcode secrets)."""

from __future__ import annotations

import os
import pwd
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.domain.errors import ConfigurationError
from app.domain.models import GpuRequirements


class Mode(StrEnum):
    SIMULATION = "simulation"
    REAL = "real"


class InferenceEngine(StrEnum):
    VLLM = "vllm"
    NIM = "nim"


def _csv(value: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in value.split(",") if item.strip())


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)

    # --- Mode -----------------------------------------------------------------------------
    ephemera_mode: Mode = Mode.SIMULATION

    # --- Storage --------------------------------------------------------------------------
    database_url: str = "postgresql+asyncpg://ephemera:ephemera@localhost:5432/ephemera"
    data_dir: Path = Path("/tmp/ephemera/jobs")
    remote_data_dir: str = "/tmp/ephemera/jobs"

    # --- Brev -----------------------------------------------------------------------------
    brev_api_key: SecretStr | None = None
    brev_org: str | None = None
    brev_cli_path: str = "brev"
    # HOME given to the Brev CLI. It MUST be the OS user's home directory: Brev writes its
    # SSH config to $HOME/.ssh/config, but OpenSSH reads ~/.ssh/config from the passwd entry
    # and ignores $HOME, so any other value makes `brev exec`/`brev copy` hang.
    # Run the worker as a dedicated OS user to keep Brev's files isolated.
    brev_home: Path = Field(default_factory=lambda: Path(pwd.getpwuid(os.getuid()).pw_dir))
    brev_exec_on_host: bool = True
    brev_allow_cli_login: bool = False
    brev_instance_types: str = ""  # explicit override, comma separated, tried in order
    brev_create_timeout_seconds: int = 600

    # --- GPU requirements -----------------------------------------------------------------
    gpu_preference: str = "L40S"
    gpu_min_vram_gb: float = 40
    gpu_fallback_types: str = "L40,A6000,RTX6000,A100"
    gpu_min_compute_capability: float | None = 8.0
    gpu_min_disk_gb: float | None = None
    gpu_max_candidates: int = 5
    gpu_min_free_disk_gb: int = 50  # checked on the instance before pulling image + weights

    # --- Model / inference ----------------------------------------------------------------
    inference_engine: InferenceEngine = InferenceEngine.VLLM
    model_id: str = "meta-llama/Llama-3.1-8B-Instruct"
    hf_token: SecretStr | None = None
    ngc_api_key: SecretStr | None = None
    # CUDA 12.9 build: the default v0.30.0 tag is CUDA 13.0 and needs NVIDIA driver >= 580,
    # which many cloud GPU images do not ship yet.
    vllm_image: str = "vllm/vllm-openai:v0.30.0-cu129"
    vllm_max_model_len: int = 16384
    vllm_gpu_memory_utilization: float = 0.90
    max_output_tokens: int = 1500

    # --- Safety limits --------------------------------------------------------------------
    # Calibrated on real Brev L40S runs: create ~3 min, instance setup a few more minutes,
    # each `brev copy` ~25 s (it refreshes SSH config), then image + weights download.
    max_job_runtime_seconds: int = 3600
    max_provisioning_seconds: int = 1200
    bootstrap_timeout_seconds: int = 600
    model_ready_timeout_seconds: int = 1500
    transfer_timeout_seconds: int = 120
    inference_timeout_seconds: int = 240
    cleanup_timeout_seconds: int = 120
    destroy_timeout_seconds: int = 300
    destroy_verify_timeout_seconds: int = 600
    extraction_timeout_seconds: int = 30
    max_active_jobs: int = 1
    max_gpu_instances: int = 1
    max_queued_jobs: int = 20
    max_document_size_mb: float = 25
    max_document_pages: int = 100
    max_input_chars: int = 32000  # keeps prompt + schema + output inside a 16k context

    # --- Data retention -------------------------------------------------------------------
    result_retention_seconds: int = 3600

    # --- Demo -----------------------------------------------------------------------------
    demo_force_inference_failure: bool = False
    demo_allow_failure_injection: bool = True
    simulation_time_scale: float = Field(default=1.0, gt=0)

    # --- Worker ---------------------------------------------------------------------------
    worker_poll_interval_seconds: float = 2.0
    heartbeat_interval_seconds: float = 5.0
    stale_job_seconds: float = 90.0
    reconcile_interval_seconds: float = 30.0
    reconcile_delete_unknown_instances: bool = False
    worker_stale_seconds: float = 30.0

    # --- API ------------------------------------------------------------------------------
    log_level: str = "INFO"
    cors_origins: str = "http://localhost:3000"

    @field_validator("brev_api_key", "hf_token", "ngc_api_key", mode="before")
    @classmethod
    def _empty_secret_is_none(cls, v: object) -> object:
        return None if v in ("", None) else v

    @field_validator("brev_org", mode="before")
    @classmethod
    def _empty_str_is_none(cls, v: object) -> object:
        return None if v in ("", None) else v

    @property
    def is_simulation(self) -> bool:
        return self.ephemera_mode is Mode.SIMULATION

    @property
    def max_document_bytes(self) -> int:
        return int(self.max_document_size_mb * 1024 * 1024)

    def gpu_requirements(self) -> GpuRequirements:
        return GpuRequirements(
            preferred_gpu=self.gpu_preference,
            min_vram_gb=self.gpu_min_vram_gb,
            fallback_gpus=_csv(self.gpu_fallback_types),
            min_compute_capability=self.gpu_min_compute_capability,
            min_disk_gb=self.gpu_min_disk_gb,
            max_candidates=self.gpu_max_candidates,
            explicit_instance_types=_csv(self.brev_instance_types),
        )

    def secret_values(self) -> list[str]:
        """Every configured secret, for log redaction."""
        secrets = [self.brev_api_key, self.hf_token, self.ngc_api_key]
        return [s.get_secret_value() for s in secrets if s and s.get_secret_value()]

    def configuration_problems(self) -> list[str]:
        """Human-readable reasons the worker cannot run in the configured mode."""
        problems: list[str] = []
        if self.ephemera_mode is Mode.REAL:
            if self.brev_api_key is None and not self.brev_allow_cli_login:
                problems.append(
                    "EPHEMERA_MODE=real requires BREV_API_KEY "
                    "(or BREV_ALLOW_CLI_LOGIN=true with a prior `brev login` in BREV_HOME)"
                )
            if not self.model_id:
                problems.append("MODEL_ID must be set in real mode")
            if self.inference_engine is InferenceEngine.NIM and self.ngc_api_key is None:
                problems.append("INFERENCE_ENGINE=nim requires NGC_API_KEY")
            ssh_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
            if self.brev_home.resolve() != ssh_home.resolve():
                problems.append(
                    f"BREV_HOME={self.brev_home} differs from the OS user's home {ssh_home}: "
                    "OpenSSH would not find Brev's SSH config, so `brev exec` would hang. "
                    "Unset BREV_HOME or set it to the user's home directory"
                )
            if self.max_gpu_instances < 1:
                problems.append("MAX_GPU_INSTANCES must be >= 1")
        if self.demo_force_inference_failure and not self.demo_allow_failure_injection:
            problems.append(
                "DEMO_FORCE_INFERENCE_FAILURE=true conflicts with DEMO_ALLOW_FAILURE_INJECTION=false"
            )
        return problems

    def require_valid(self) -> None:
        problems = self.configuration_problems()
        if problems:
            raise ConfigurationError("; ".join(problems))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
