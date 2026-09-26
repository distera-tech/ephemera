"""NVIDIA NIM provider — architectural placeholder (not implemented in the MVP).

NIM exposes the same OpenAI-compatible ``/v1/chat/completions`` API as vLLM,
so the transfer/infer/cleanup steps of ``VLLMProvider`` can be reused; what
differs is the container image (``nvcr.io/nim/...``), authentication with
``NGC_API_KEY`` (``docker login nvcr.io``) and the model cache path.
Selecting ``INFERENCE_ENGINE=nim`` fails fast with a clear configuration error
instead of silently falling back to another engine.
"""

from __future__ import annotations

from typing import NoReturn

from app.domain.errors import ConfigurationError


def build_nim_provider() -> NoReturn:
    raise ConfigurationError(
        "INFERENCE_ENGINE=nim is not implemented yet; use INFERENCE_ENGINE=vllm "
        "(see docs/decisions/ADR-005-vllm-primary-inference.md)"
    )
