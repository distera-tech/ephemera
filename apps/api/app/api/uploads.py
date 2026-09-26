"""Upload validation. Never trust the filename, MIME type or extension."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from fastapi import UploadFile

from app.infrastructure.documents.pymupdf_processor import looks_like_pdf
from app.infrastructure.storage.workspace import write_private

ALLOWED_CONTENT_TYPES = {"application/pdf", "application/x-pdf"}
_CHUNK = 1024 * 256
_UNSAFE = re.compile(r"[^\w.\- ()\[\]]+")


class UploadRejected(Exception):
    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class StoredUpload:
    size: int
    sha256: str
    display_name: str


def sanitize_display_name(raw: str | None) -> str:
    """Display-only name (stored as metadata, never used as a path or CLI argument)."""
    name = unicodedata.normalize("NFKC", raw or "document.pdf")
    name = name.replace("\\", "/").rsplit("/", 1)[-1]  # drop any path component
    name = _UNSAFE.sub("_", name).strip(" .")
    return (name or "document.pdf")[:120]


async def store_pdf_upload(upload: UploadFile, destination: Path, max_bytes: int) -> StoredUpload:
    content_type = (upload.content_type or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise UploadRejected(
            415, "UNSUPPORTED_MEDIA_TYPE", "only application/pdf uploads are accepted"
        )

    digest = hashlib.sha256()
    size = 0
    head = b""
    chunks: list[bytes] = []
    while True:
        chunk = await upload.read(_CHUNK)
        if not chunk:
            break
        size += len(chunk)
        if size > max_bytes:
            raise UploadRejected(
                413,
                "DOCUMENT_TOO_LARGE",
                f"document exceeds the {max_bytes // (1024 * 1024)} MB limit",
            )
        if len(head) < 1024:
            head += chunk[: 1024 - len(head)]
        digest.update(chunk)
        chunks.append(chunk)
    if size == 0:
        raise UploadRejected(400, "DOCUMENT_EMPTY", "empty upload")
    if not looks_like_pdf(head):
        raise UploadRejected(415, "DOCUMENT_INVALID", "file signature is not PDF")
    write_private(destination, b"".join(chunks))
    return StoredUpload(
        size=size, sha256=digest.hexdigest(), display_name=sanitize_display_name(upload.filename)
    )
