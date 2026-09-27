from __future__ import annotations

import os
import stat
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pymupdf
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

API_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://ephemera:ephemera@localhost:5432/ephemera_test"
)


def make_pdf(
    text: str = "Hello Ephemera. This is a synthetic test document.", pages: int = 1
) -> bytes:
    doc = pymupdf.open()
    for i in range(pages):
        page = doc.new_page()
        page.insert_text((50, 72), f"{text} (page {i + 1})", fontsize=10)
    data: bytes = doc.tobytes()
    doc.close()
    return data


@pytest.fixture
def pdf_bytes() -> bytes:
    return make_pdf()


# ------------------------------------------------------------------------- database
def _db_available() -> bool:
    import asyncio

    async def probe() -> bool:
        engine = create_async_engine(TEST_DATABASE_URL)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    return asyncio.run(probe())


_DB_OK: bool | None = None


def db_available() -> bool:
    global _DB_OK
    if _DB_OK is None:
        _DB_OK = _db_available()
    return _DB_OK


@pytest.fixture(scope="session")
def migrated_db() -> str:
    if not db_available():
        pytest.skip("PostgreSQL not available (set TEST_DATABASE_URL)")
    import asyncio

    async def reset_schema() -> None:
        eng = create_async_engine(TEST_DATABASE_URL)
        async with eng.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await eng.dispose()

    asyncio.run(reset_schema())
    env = {**os.environ, "DATABASE_URL": TEST_DATABASE_URL}
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"], check=True, env=env, cwd=API_ROOT
    )
    return TEST_DATABASE_URL


@pytest_asyncio.fixture
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    async with eng.begin() as conn:
        await conn.execute(
            text("TRUNCATE jobs, job_events, job_results, worker_heartbeats CASCADE")
        )
    yield eng
    await eng.dispose()


@pytest_asyncio.fixture
async def repo(engine: AsyncEngine):  # type: ignore[no-untyped-def]
    from app.infrastructure.database.repository import PostgresJobRepository
    from app.infrastructure.database.session import create_sessionmaker

    return PostgresJobRepository(create_sessionmaker(engine))


@pytest.fixture
def workspace(tmp_path: Path):  # type: ignore[no-untyped-def]
    from app.infrastructure.storage.workspace import JobWorkspace

    ws = JobWorkspace(tmp_path / "jobs")
    ws.ensure_root()
    return ws


# ------------------------------------------------------------------------- fake brev
@pytest.fixture
def fake_brev(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Returns the fake CLI's state dir. Executables: <state>/bin/{brev,ssh,scp}."""
    state = tmp_path / "fake-brev"
    (state / "bin").mkdir(parents=True)
    exe = state / "bin" / "brev"
    # BrevClient passes a sanitized environment, so the state path is baked into the wrapper.
    exe.write_text(
        f'#!/bin/sh\nFAKE_BREV_STATE={state} exec {sys.executable} {FIXTURES / "fake_brev.py"} "$@"\n'
    )
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    # ssh/scp to `<instance>-host` are emulated by the same fake (see to_brev_argv).
    for tool in ("ssh", "scp"):
        w = state / "bin" / tool
        w.write_text(
            f"#!/bin/sh\nFAKE_BREV_STATE={state} exec {sys.executable} "
            f'{FIXTURES / "fake_brev.py"} __{tool} "$@"\n'
        )
        w.chmod(w.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("FAKE_BREV_STATE", str(state))
    return state


def set_fake_mode(state: Path, *flags: str) -> None:
    (state / "mode").write_text(",".join(flags))
