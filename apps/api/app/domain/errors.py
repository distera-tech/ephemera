"""Structured error codes. Every failure a job can end with maps to one of these."""

from __future__ import annotations

from enum import StrEnum


class ErrorCode(StrEnum):
    # Document / input
    DOCUMENT_INVALID = "DOCUMENT_INVALID"
    DOCUMENT_TOO_LARGE = "DOCUMENT_TOO_LARGE"
    DOCUMENT_TOO_MANY_PAGES = "DOCUMENT_TOO_MANY_PAGES"
    DOCUMENT_EMPTY = "DOCUMENT_EMPTY"
    DOCUMENT_EXTRACTION_TIMEOUT = "DOCUMENT_EXTRACTION_TIMEOUT"
    # Capacity / provisioning
    CAPACITY_EXCEEDED = "CAPACITY_EXCEEDED"
    NO_GPU_AVAILABLE = "NO_GPU_AVAILABLE"
    PROVISIONING_FAILED = "PROVISIONING_FAILED"
    PROVISIONING_TIMEOUT = "PROVISIONING_TIMEOUT"
    # Environment / model
    BOOTSTRAP_FAILED = "BOOTSTRAP_FAILED"
    BOOTSTRAP_TIMEOUT = "BOOTSTRAP_TIMEOUT"
    MODEL_STARTUP_FAILED = "MODEL_STARTUP_FAILED"
    MODEL_STARTUP_TIMEOUT = "MODEL_STARTUP_TIMEOUT"
    # Workload
    TRANSFER_FAILED = "TRANSFER_FAILED"
    INFERENCE_FAILED = "INFERENCE_FAILED"
    INFERENCE_TIMEOUT = "INFERENCE_TIMEOUT"
    INFERENCE_OUTPUT_INVALID = "INFERENCE_OUTPUT_INVALID"
    INFERENCE_FORCED_FAILURE = "INFERENCE_FORCED_FAILURE"
    JOB_TIMEOUT = "JOB_TIMEOUT"
    CANCELLED = "CANCELLED"
    WORKER_INTERRUPTED = "WORKER_INTERRUPTED"
    # Teardown
    CLEANUP_FAILED = "CLEANUP_FAILED"
    DESTROY_FAILED = "DESTROY_FAILED"
    DESTROY_VERIFICATION_FAILED = "DESTROY_VERIFICATION_FAILED"
    # Generic
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class EphemeraError(Exception):
    """Base class for failures that carry a structured, user-safe error code.

    ``message`` must never contain document content, prompts, model output or
    credentials: it is persisted and shown in the UI.
    """

    code: ErrorCode = ErrorCode.INTERNAL_ERROR

    def __init__(self, message: str, *, code: ErrorCode | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code
        self.message = message


class DocumentError(EphemeraError):
    code = ErrorCode.DOCUMENT_INVALID


class ComputeError(EphemeraError):
    code = ErrorCode.PROVISIONING_FAILED


class InferenceError(EphemeraError):
    code = ErrorCode.INFERENCE_FAILED


class ConfigurationError(EphemeraError):
    code = ErrorCode.CONFIGURATION_ERROR
