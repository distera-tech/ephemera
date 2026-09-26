"""API tests (HTTPX against the ASGI app) + end-to-end simulation through the worker path."""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from app.application.orchestrator import Orchestrator
from app.core.config import Settings
from app.core.container import build_documents, orchestrator_config
from app.domain.status import JobStatus
from app.infrastructure.compute.simulated import SimulatedComputeProvider
from app.infrastructure.inference.simulated import SimulatedInferenceProvider
from app.main import create_app
from tests.conftest import make_pdf

pytestmark = pytest.mark.integration


@pytest.fixture
def settings(migrated_db: str, tmp_path: Path) -> Settings:
    return Settings(
        database_url=migrated_db,
        data_dir=tmp_path / "jobs",
        ephemera_mode="simulation",
        max_document_size_mb=1,
        simulation_time_scale=0.01,
        heartbeat_interval_seconds=0.1,
        max_queued_jobs=3,
        _env_file=None,  # type: ignore[call-arg]
    )


@pytest_asyncio.fixture
async def client(settings: Settings, engine) -> AsyncIterator[httpx.AsyncClient]:  # type: ignore[no-untyped-def]
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
            c.app = app  # type: ignore[attr-defined]
            yield c


def upload(pdf: bytes, content_type: str = "application/pdf", **data: str) -> dict[str, object]:
    return {"files": {"file": ("contract.pdf", pdf, content_type)}, "data": data}


async def test_create_job_returns_202_and_is_queued(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/jobs", **upload(make_pdf(), analysis_type="contract"))
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "QUEUED" and body["display_id"].startswith("EPH-")
    detail = (await client.get(f"/api/jobs/{body['job_id']}")).json()
    assert detail["analysis_type"] == "contract"
    assert detail["instance_name"].startswith("ephemera-")
    assert detail["filename"] == "contract.pdf"
    assert "text" not in detail and "document" not in detail


async def test_7_oversized_document_rejected_before_anything(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    big = b"%PDF-1.7\n" + b"0" * (2 * 1024 * 1024)
    r = await client.post("/api/jobs", **upload(big))
    assert r.status_code == 413
    assert (await client.get("/api/jobs")).json() == []
    assert list(settings.data_dir.iterdir()) == []  # nothing left on disk


@pytest.mark.parametrize(
    ("payload", "ctype", "code"),
    [
        (b"MZ not a pdf", "application/pdf", 415),
        (b"%PDF-1.4 ok", "text/plain", 415),
        (b"", "application/pdf", 400),
    ],
)
async def test_invalid_uploads_rejected(
    client: httpx.AsyncClient, payload: bytes, ctype: str, code: int
) -> None:
    r = await client.post("/api/jobs", **upload(payload, content_type=ctype))
    assert r.status_code == code


async def test_queue_limit(client: httpx.AsyncClient) -> None:
    for _ in range(3):
        assert (await client.post("/api/jobs", **upload(make_pdf()))).status_code == 202
    assert (await client.post("/api/jobs", **upload(make_pdf()))).status_code == 429


async def test_unknown_job_404(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/jobs/00000000-0000-0000-0000-000000000000")).status_code == 404
    assert (await client.get("/api/jobs/not-a-uuid")).status_code == 422


async def test_health_readiness_config(client: httpx.AsyncClient) -> None:
    assert (await client.get("/api/health")).json() == {"status": "ok"}
    ready = (await client.get("/api/readiness")).json()
    assert ready["ready"] is True and ready["checks"]["database"] == "ok"
    cfg = (await client.get("/api/config")).json()
    assert cfg["mode"] == "simulation" and cfg["model_id"].startswith("simulation/")
    assert "brev_api_key" not in str(cfg).lower() and "hf_token" not in str(cfg).lower()


async def test_cancel_queued_job(client: httpx.AsyncClient) -> None:
    job_id = (await client.post("/api/jobs", **upload(make_pdf()))).json()["job_id"]
    r = await client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200
    detail = (await client.get(f"/api/jobs/{job_id}")).json()
    assert detail["cancel_requested"] is True


async def test_failure_injection_can_be_disabled(settings: Settings, engine) -> None:  # type: ignore[no-untyped-def]
    settings.demo_allow_failure_injection = False
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://t"
        ) as c:
            r = await c.post("/api/jobs", **upload(make_pdf(), force_inference_failure="true"))
            assert r.status_code == 403


async def run_worker_once(client: httpx.AsyncClient, settings: Settings) -> JobStatus:
    app = client.app  # type: ignore[attr-defined]
    repo = app.state.repo
    job, _ = await repo.claim_next("e2e-worker", 1, 1)
    assert job is not None
    orch = Orchestrator(
        repo=repo,
        compute=SimulatedComputeProvider(time_scale=0.01),
        inference=SimulatedInferenceProvider(time_scale=0.01),
        documents=build_documents(settings),
        workspace=app.state.workspace,
        config=orchestrator_config(settings),
        worker_id="e2e-worker",
    )
    return await asyncio.wait_for(orch.run(job), 30)


async def test_e2e_simulation_upload_to_compute_zero(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    pdf = (
        Path(__file__).resolve().parents[4] / "examples" / "demo-documents" / "sample-contract.pdf"
    )
    job_id = (
        await client.post("/api/jobs", **upload(pdf.read_bytes(), analysis_type="contract"))
    ).json()["job_id"]
    assert await run_worker_once(client, settings) is JobStatus.COMPLETED

    detail = (await client.get(f"/api/jobs/{job_id}")).json()
    assert detail["status"] == "COMPLETED"
    assert detail["instance_state"] == "DESTROYED"
    assert detail["document_state"] == "CLEANED"
    assert detail["result"]["requires_human_review"] is True
    assert detail["result"]["summary"].startswith("[Simulated analysis")
    assert detail["durations"]["gpu_runtime_seconds"] is not None
    events = (await client.get(f"/api/jobs/{job_id}/events")).json()
    assert [e["status"] for e in events if e["event_type"] == "state.transition"][-1] == "COMPLETED"
    stats = (await client.get("/api/stats")).json()
    assert stats["active_gpu_instances"] == 0 and stats["compute_to_zero"] is True
    assert stats["completed_jobs"] == 1

    assert (await client.delete(f"/api/jobs/{job_id}/result")).status_code == 204
    assert (await client.get(f"/api/jobs/{job_id}")).json()["result"] is None


async def test_e2e_simulation_forced_failure(client: httpx.AsyncClient, settings: Settings) -> None:
    job_id = (
        await client.post("/api/jobs", **upload(make_pdf(), force_inference_failure="true"))
    ).json()["job_id"]
    assert await run_worker_once(client, settings) is JobStatus.FAILED
    d = (await client.get(f"/api/jobs/{job_id}")).json()
    assert d["error_code"] == "INFERENCE_FORCED_FAILURE"
    assert d["instance_state"] == "DESTROYED" and d["cleanup_report"]["succeeded"] is True
    stats = (await client.get("/api/stats")).json()
    assert stats["failed_jobs"] == 1 and stats["compute_to_zero"] is True
