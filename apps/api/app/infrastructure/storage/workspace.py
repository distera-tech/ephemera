"""Job-scoped temporary directories: ``<data_dir>/<job-id>/``.

The user's filename is never used as a path. Uploads are always stored as
``document.pdf`` inside a directory named after the server-generated UUID.
"""

from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

DOCUMENT_FILENAME = "document.pdf"


class JobWorkspace:
    def __init__(self, root: Path) -> None:
        self.root = root

    def ensure_root(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def dir_for(self, job_id: uuid.UUID) -> Path:
        # job_id is a UUID object, so str(job_id) cannot contain path separators.
        path = (self.root / str(job_id)).resolve()
        if path.parent != self.root.resolve():
            raise ValueError("job directory escaped workspace root")
        return path

    def create(self, job_id: uuid.UUID) -> Path:
        self.ensure_root()
        path = self.dir_for(job_id)
        path.mkdir(mode=0o700, exist_ok=False)
        return path

    def document_path(self, job_id: uuid.UUID) -> Path:
        return self.dir_for(job_id) / DOCUMENT_FILENAME

    def exists(self, job_id: uuid.UUID) -> bool:
        return self.dir_for(job_id).exists()

    def delete(self, job_id: uuid.UUID) -> bool:
        """Delete the job directory. Returns True if nothing remains afterwards.

        This is *application-level* deletion (unlink). It does not guarantee the
        bytes are unrecoverable from the underlying storage medium.
        """
        path = self.dir_for(job_id)
        if path.exists():
            shutil.rmtree(path)
        return not path.exists()


def write_private(path: Path, data: bytes | str) -> None:
    """Write a file readable only by the current user."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data.encode() if isinstance(data, str) else data)
