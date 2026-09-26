"""Real-infrastructure smoke test (spends real GPU money).

Runs the production orchestrator in EPHEMERA_MODE=real against NVIDIA Brev:
  1. success run   — provision, verify GPU, start vLLM, infer, clean, destroy, verify
  2. failure run   — same, with inference failure injected; the GPU must still be destroyed
Then lists the provider's ephemera-* instances and fails if any remain.

Usage (from apps/api, with .env containing real credentials and a migrated database):
    uv run python ../../scripts/real_smoke_test.py --yes [--skip-failure-run]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "apps" / "api"))

from app.application.orchestrator import Orchestrator  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.container import (  # noqa: E402
    build_compute,
    build_database,
    build_documents,
    build_inference,
    build_workspace,
    orchestrator_config,
)
from app.core.logging import configure_logging  # noqa: E402
from app.domain.models import AnalysisType, instance_name_for  # noqa: E402
from app.infrastructure.storage.workspace import DOCUMENT_FILENAME  # noqa: E402

DOC = ROOT / "examples" / "demo-documents" / "sample-contract.pdf"


async def run_once(force_failure: bool) -> dict[str, object]:
    settings = get_settings()
    db = build_database(settings)
    workspace = build_workspace(settings)
    compute = build_compute(settings)
    orch = Orchestrator(
        repo=db.repo, compute=compute, inference=build_inference(settings),
        documents=build_documents(settings), workspace=workspace,
        config=orchestrator_config(settings), worker_id="real-smoke-test",
    )
    data = DOC.read_bytes()
    job_id = uuid.uuid4()
    (workspace.create(job_id) / DOCUMENT_FILENAME).write_bytes(data)
    await db.repo.create(
        job_id=job_id, filename=DOC.name, content_type="application/pdf", file_size=len(data),
        sha256=hashlib.sha256(data).hexdigest(), analysis_type=AnalysisType.CONTRACT, mode="real",
        model_id=settings.model_id, requested_gpu=settings.gpu_preference,
        instance_name=instance_name_for(job_id), force_inference_failure=force_failure,
    )
    job, reason = await db.repo.claim_next("real-smoke-test", 1, settings.max_gpu_instances)
    if job is None or job.id != job_id:
        raise SystemExit(f"could not claim smoke-test job (blocked: {reason}); is a worker running?")
    status = await orch.run(job)
    final = await db.repo.get(job_id)
    events = await db.repo.list_events(job_id)
    remaining = [i.name for i in await compute.list_instances()]
    await db.engine.dispose()
    assert final is not None

    def span(a: object, b: object) -> float | None:
        return round((b - a).total_seconds(), 1) if a and b else None  # type: ignore[operator]

    return {
        "job_id": str(job_id), "status": status.value, "error_code": final.error_code,
        "gpu": final.actual_gpu, "instance_type": final.instance_type, "vram_mb": final.gpu_vram_mb,
        "instance_state": final.instance_state.value,
        "timings_s": {
            "provisioning": span(final.provisioning_started_at, final.provisioning_completed_at),
            "model_loading": span(final.model_loading_started_at, final.model_ready_at),
            "inference": span(final.inference_started_at, final.inference_completed_at),
            "cleanup": span(final.cleanup_started_at, final.cleanup_completed_at),
            "destruction": span(final.destruction_started_at, final.destruction_completed_at),
            "gpu_runtime": span(final.provisioning_started_at, final.destruction_completed_at),
            "total": span(final.created_at, final.completed_at),
        },
        "ephemera_instances_remaining": remaining,
        "events": [f"{e.timestamp:%H:%M:%S} {e.event_type}: {e.message}" for e in events],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", action="store_true", help="confirm you accept real GPU costs")
    parser.add_argument("--skip-failure-run", action="store_true")
    args = parser.parse_args()
    settings = get_settings()
    configure_logging("WARNING", settings.secret_values())
    if settings.is_simulation:
        print("EPHEMERA_MODE must be 'real' for this smoke test.", file=sys.stderr)
        return 2
    problems = settings.configuration_problems()
    if problems:
        print("Configuration error: " + "; ".join(problems), file=sys.stderr)
        return 2
    if not args.yes:
        print("This provisions a real GPU on Brev and costs money. Re-run with --yes.", file=sys.stderr)
        return 2

    ok = True
    runs = [("success", False)] + ([] if args.skip_failure_run else [("forced-failure", True)])
    for label, force in runs:
        print(f"\n=== {label} run ===", flush=True)
        report = asyncio.run(run_once(force))
        print(json.dumps(report, indent=2))
        expected = "FAILED" if force else "COMPLETED"
        if report["status"] != expected or report["instance_state"] != "DESTROYED" or report["ephemera_instances_remaining"]:
            ok = False
            print(f"!!! {label} run did not meet expectations", file=sys.stderr)
    print("\nREAL SMOKE TEST", "PASSED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
