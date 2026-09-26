"""Isolated PDF text extraction entry point.

Run as ``python -m app.infrastructure.documents.extract_cli PDF OUT MAX_PAGES MAX_CHARS``.
Executed in a child process with CPU/memory limits so a malicious or
pathological PDF cannot hang or exhaust the worker. Writes the extracted text
to OUT (mode 0600) and a small JSON status object to stdout. Never prints
document text.
"""

from __future__ import annotations

import json
import os
import re
import sys

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _limit_resources() -> None:
    try:
        import resource

        mem = 1024 * 1024 * 1024  # 1 GiB address space
        resource.setrlimit(resource.RLIMIT_AS, (mem, mem))
        resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    except (ImportError, ValueError, OSError):  # pragma: no cover - platform dependent
        pass


def _emit(obj: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(obj))
    sys.stdout.flush()


def main(argv: list[str]) -> int:
    if len(argv) != 5:
        _emit({"error": "DOCUMENT_INVALID", "message": "bad extractor invocation"})
        return 2
    pdf_path, out_path, max_pages_s, max_chars_s = argv[1:]
    max_pages, max_chars = int(max_pages_s), int(max_chars_s)
    _limit_resources()

    import pymupdf

    try:
        doc = pymupdf.open(pdf_path, filetype="pdf")
    except Exception:
        _emit({"error": "DOCUMENT_INVALID", "message": "file could not be parsed as PDF"})
        return 1
    try:
        if doc.needs_pass:
            _emit({"error": "DOCUMENT_INVALID", "message": "encrypted PDFs are not supported"})
            return 1
        pages = doc.page_count
        if pages == 0:
            _emit({"error": "DOCUMENT_EMPTY", "message": "PDF has no pages"})
            return 1
        if pages > max_pages:
            _emit(
                {
                    "error": "DOCUMENT_TOO_MANY_PAGES",
                    "message": f"PDF has {pages} pages; limit is {max_pages}",
                }
            )
            return 1
        parts: list[str] = []
        total = 0
        truncated = False
        for index in range(pages):
            text = _CONTROL.sub("", doc.load_page(index).get_text("text"))
            parts.append(text)
            total += len(text)
            if total > max_chars:
                truncated = True
                break
        joined = "\n".join(parts).strip()
        if len(joined) > max_chars:
            joined = joined[:max_chars]
            truncated = True
        if not joined:
            _emit(
                {
                    "error": "DOCUMENT_EMPTY",
                    "message": "no extractable text (scanned PDFs / OCR are not supported)",
                }
            )
            return 1
    finally:
        doc.close()

    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(joined)
    _emit({"pages": pages, "chars": len(joined), "truncated": truncated})
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
