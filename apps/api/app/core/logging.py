"""Structured JSON logging with per-job correlation and secret redaction.

Policy: never log document contents, extracted text, prompts, model outputs,
API keys, tokens or credentials. Callers log identifiers and sizes only; the
redaction filter is a second line of defence for credentials.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_job_id: ContextVar[str | None] = ContextVar("job_id", default=None)
_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)

_REDACTED = "[REDACTED]"
# Well-known credential shapes (Hugging Face, NVIDIA NGC/API, bearer tokens, generic key=value).
_PATTERNS = [
    re.compile(r"hf_[A-Za-z0-9]{10,}"),
    re.compile(r"nvapi-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"),
    re.compile(
        r"(?i)((?:api[_-]?key|token|secret|password|authorization)[\"']?\s*[:=]\s*[\"']?)"
        r"([^\s\"',}]{4,})"
    ),
]

_known_secrets: list[str] = []

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


def register_secrets(values: list[str]) -> None:
    """Register literal secret values that must never appear in logs."""
    for v in values:
        if v and len(v) >= 4 and v not in _known_secrets:
            _known_secrets.append(v)


def redact(text: str) -> str:
    for secret in _known_secrets:
        text = text.replace(secret, _REDACTED)
    for pattern in _PATTERNS:
        if pattern.groups == 2:
            text = pattern.sub(lambda m: m.group(1) + _REDACTED, text)
        else:
            text = pattern.sub(_REDACTED, text)
    return text


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "component": getattr(record, "component", record.name),
            "event": record.getMessage(),
        }
        job_id = getattr(record, "job_id", None) or _job_id.get()
        if job_id:
            payload["job_id"] = str(job_id)
        correlation_id = getattr(record, "correlation_id", None) or _correlation_id.get()
        if correlation_id:
            payload["correlation_id"] = correlation_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED and key not in payload and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            # Exception type and message only; tracebacks can embed local variables' reprs.
            exc = record.exc_info[1]
            payload["error_type"] = type(exc).__name__ if exc else None
            payload["error"] = str(exc) if exc else None
        return redact(json.dumps(_redact_value(payload), default=str))


def configure_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    register_secrets(secrets or [])
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # Uvicorn access logs include query strings; keep them but route through our formatter.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    for noisy in ("sqlalchemy.engine", "asyncio"):
        logging.getLogger(noisy).setLevel("WARNING")


class _MergingAdapter(logging.LoggerAdapter[logging.Logger]):
    """LoggerAdapter that merges per-call ``extra`` instead of replacing it."""

    def process(self, msg: Any, kwargs: Any) -> tuple[Any, Any]:
        kwargs["extra"] = {**(self.extra or {}), **(kwargs.get("extra") or {})}
        return msg, kwargs


def get_logger(component: str) -> logging.LoggerAdapter[logging.Logger]:
    return _MergingAdapter(logging.getLogger(f"ephemera.{component}"), {"component": component})


@contextmanager
def job_context(job_id: str, correlation_id: str | None = None) -> Iterator[None]:
    t1 = _job_id.set(job_id)
    t2 = _correlation_id.set(correlation_id or job_id)
    try:
        yield
    finally:
        _job_id.reset(t1)
        _correlation_id.reset(t2)
