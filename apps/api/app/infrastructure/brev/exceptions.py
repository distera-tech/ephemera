from __future__ import annotations

from app.domain.errors import ComputeError, ErrorCode


class BrevError(ComputeError):
    """Base class for Brev CLI failures. Messages are redacted and never contain secrets."""


class BrevCLINotFoundError(BrevError):
    code = ErrorCode.CONFIGURATION_ERROR


class BrevAuthError(BrevError):
    code = ErrorCode.CONFIGURATION_ERROR


class BrevTimeoutError(BrevError):
    def __init__(self, message: str, *, timeout_s: float) -> None:
        super().__init__(message)
        self.timeout_s = timeout_s


class BrevCommandError(BrevError):
    def __init__(self, message: str, *, exit_code: int, stderr_tail: str) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.stderr_tail = stderr_tail


class BrevOutputError(BrevError):
    """CLI output did not match the documented JSON shape."""


class UnsafeInstanceNameError(BrevError):
    """Refused to act on an instance that does not follow Ephemera's naming scheme."""

    code = ErrorCode.INTERNAL_ERROR
