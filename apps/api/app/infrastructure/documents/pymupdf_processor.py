"""PyMuPDF document processor (runs extraction in a sandboxed child process)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sys
from pathlib import Path

from app.domain.errors import DocumentError, ErrorCode
from app.domain.models import ExtractedDocument

PDF_MAGIC = b"%PDF-"
EXTRACTED_FILENAME = "extracted.txt"
_API_ROOT = str(Path(__file__).resolve().parents[3])


def looks_like_pdf(head: bytes) -> bool:
    """Signature check. PDF headers may be preceded by a little junk (spec allows 1024 bytes)."""
    return PDF_MAGIC in head[:1024]


def _read_head(path: Path) -> bytes:
    with path.open("rb") as fh:
        return fh.read(1024)


class PyMuPDFDocumentProcessor:
    def __init__(self, *, max_pages: int, max_chars: int, timeout_s: float) -> None:
        self.max_pages = max_pages
        self.max_chars = max_chars
        self.timeout_s = timeout_s

    async def extract(self, pdf_path: Path, work_dir: Path) -> ExtractedDocument:
        head = await asyncio.to_thread(_read_head, pdf_path)
        if not looks_like_pdf(head):
            raise DocumentError("file signature is not PDF", code=ErrorCode.DOCUMENT_INVALID)

        out_path = work_dir / EXTRACTED_FILENAME
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "app.infrastructure.documents.extract_cli",
            str(pdf_path),
            str(out_path),
            str(self.max_pages),
            str(self.max_chars),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env={"PYTHONPATH": _API_ROOT, "PATH": "/usr/bin:/bin"},
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise DocumentError(
                f"text extraction exceeded {self.timeout_s:.0f}s",
                code=ErrorCode.DOCUMENT_EXTRACTION_TIMEOUT,
            ) from None

        try:
            status = json.loads(stdout.decode() or "{}")
        except json.JSONDecodeError:
            status = {}
        if proc.returncode != 0 or "error" in status:
            code = status.get("error", "DOCUMENT_INVALID")
            message = status.get("message", "document extraction failed")
            try:
                error_code = ErrorCode(code)
            except ValueError:
                error_code = ErrorCode.DOCUMENT_INVALID
            raise DocumentError(str(message), code=error_code)

        text = out_path.read_text(encoding="utf-8")
        out_path.unlink(missing_ok=True)  # keep the text in memory only for as long as needed
        return ExtractedDocument(
            text=text,
            page_count=int(status["pages"]),
            char_count=int(status["chars"]),
            truncated=bool(status["truncated"]),
        )
