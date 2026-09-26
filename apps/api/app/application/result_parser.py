"""Validate raw model output into a ``DocumentAnalysis``.

Model output is untrusted (it may have been steered by the document). It is
parsed as JSON and validated against a strict, size-bounded schema; anything
else is rejected rather than rendered.
"""

from __future__ import annotations

import json
import re

from pydantic import ValidationError

from app.domain.analysis import DocumentAnalysis

_FENCE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
MAX_RAW_OUTPUT_CHARS = 200_000


class InvalidModelOutput(ValueError):
    """Raised when model output cannot be validated. Message never includes the output."""


def _extract_json_object(raw: str) -> str:
    text = raw.strip()
    fenced = _FENCE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    if text.startswith("{"):
        return text
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise InvalidModelOutput("no JSON object found in model output")
    return text[start : end + 1]


def parse_analysis(raw: str) -> DocumentAnalysis:
    if len(raw) > MAX_RAW_OUTPUT_CHARS:
        raise InvalidModelOutput("model output exceeds size limit")
    try:
        data = json.loads(_extract_json_object(raw))
    except json.JSONDecodeError as exc:
        raise InvalidModelOutput(f"model output is not valid JSON (pos {exc.pos})") from None
    if not isinstance(data, dict):
        raise InvalidModelOutput("model output is not a JSON object")
    try:
        return DocumentAnalysis.model_validate(data)
    except ValidationError as exc:
        # Only field locations and error types — never the offending values.
        where = sorted({".".join(str(p) for p in e["loc"]) or "<root>" for e in exc.errors()})
        raise InvalidModelOutput(
            f"model output failed schema validation at: {', '.join(where)[:300]}"
        ) from None
