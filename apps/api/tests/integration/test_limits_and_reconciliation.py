"""TEST 6 (worker restart → reconciliation) and TEST 8 (concurrency limits)."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import inspect

from app.application.reconciliation import Reconciler
from app.domain.models import InstanceInfo, instance_name_for
from app.domain.status import InstanceState, InvalidTransitionError, JobStatus
from tests.conftest import make_pdf
from tests.fakes import FakeCompute, submit

pytestmark = pytest.mark.integration


def reconciler(repo, compute, workspace, **kw):  # type: ignore[no-untyped-def]
    args = dict(
        repo=repo,
        compute=compute,
        workspace=workspace,
        worker_id="worker-a",
        stale_job_seconds=60,
        destroy_timeout_s=5,
        destroy_verify_timeout_s=2,
    )
    args.update(kw)
    return Reconciler(**args)


async def mark_cleanup_failed(repo, job_id) -> None:  # type: ignore[no-untyped-def]
    await repo.transition(job_id, JobStatus.CLEANING, "sim")
    await repo.transition(
        job_id,
        JobStatus.CLEANUP_FAILED,
        "sim",
        fields={"instance_state": InstanceState.DESTROY_FAILED},
    )


async def test_update_fields_cannot_bypass_state_machine(repo, workspace) -> None:  # type: ignore[no-untyped-def]
    job_id = await submit(repo, workspace, make_pdf())
    with pytest.raises(ValueError):
        await repo.update_fields(job_id, status=JobStatus.COMPLETED)


async def test_8_second_job_stays_queued_when_limit_reached(repo, workspace) -> None:  # type: ignore[no-untyped-def]
    first = await submit(repo, workspace, make_pdf())
    second = await submit(repo, workspace, make_pdf())
    job, reason = await repo.claim_next("w1", max_active_jobs=1, max_gpu_instances=1)
    assert job is not None and job.id == first
    job2, reason = await repo.claim_next("w2", max_active_jobs=1, max_gpu_instances=1)
    assert job2 is None and reason == "max_active_jobs"
    assert (await repo.get(second)).status is JobStatus.QUEUED


async def test_leaked_instance_blocks_new_provisioning(repo, workspace) -> None:  # type: ignore[no-untyped-def]
    """A terminal job whose GPU was never verified destroyed counts against MAX_GPU_INSTANCES."""
    leaked = await submit(repo, workspace, make_pdf())
    await mark_cleanup_failed(repo, leaked)
    await submit(repo, workspace, make_pdf())
    job, reason = await repo.claim_next("w1", max_active_jobs=5, max_gpu_instances=1)
    assert job is None and reason == "max_gpu_instances"


async def test_repository_rejects_illegal_transition(repo, workspace) -> None:  # type: ignore[no-untyped-def]
    job_id = await submit(repo, workspace, make_pdf())
    with pytest.raises(InvalidTransitionError):
        await repo.transition(job_id, JobStatus.COMPLETED, "cheating")


async def test_transitions_are_persisted_as_events(repo, workspace) -> None:  # type: ignore[no-untyped-def]
    job_id = await submit(repo, workspace, make_pdf())
    await repo.transition(job_id, JobStatus.PROVISIONING, "go", metadata={"k": "v"})
    events = await repo.list_events(job_id)
    t = [e for e in events if e.event_type == "state.transition"][0]
    assert t.status == "PROVISIONING" and t.metadata == {
        "from": "QUEUED",
        "to": "PROVISIONING",
        "k": "v",
    }
    assert t.timestamp is not None


async def test_schema_has_no_document_body_columns(engine) -> None:  # type: ignore[no-untyped-def]
    async with engine.connect() as conn:
        cols = await conn.run_sync(
            lambda c: {
                t: [x["name"] for x in inspect(c).get_columns(t)]
                for t in ("jobs", "job_events", "job_results")
            }
        )
    flat = " ".join(" ".join(v) for v in cols.values())
    for forbidden in ("content", "text", "body", "prompt", "bytes", "raw"):
        assert forbidden not in flat.replace("content_type", ""), forbidden


async def test_6_restart_recovers_in_flight_job_and_destroys_instance(repo, workspace) -> None:  # type: ignore[no-untyped-def]
    job_id = await submit(repo, workspace, make_pdf())
    job, _ = await repo.claim_next("worker-a", 5, 5)
    name = instance_name_for(job_id)
    # Simulate: worker-a provisioned a GPU, reached INFERENCING, then was SIGKILLed.
    for s in (
        JobStatus.PROVISIONING,
        JobStatus.BOOTSTRAPPING,
        JobStatus.MODEL_LOADING,
        JobStatus.READY,
        JobStatus.TRANSFERRING,
        JobStatus.INFERENCING,
    ):
        await repo.transition(job_id, s, "sim")
    await repo.update_fields(job_id, instance_state=InstanceState.ACTIVE)
    compute = FakeCompute()
    compute.instances[name] = InstanceInfo(name, "RUNNING")

    report = await reconciler(repo, compute, workspace).reconcile(startup=True)

    assert report.recovered_jobs == [str(job_id)]
    final = await repo.get(job_id)
    assert final.status is JobStatus.FAILED
    assert final.error_code == "WORKER_INTERRUPTED"
    assert final.instance_state is InstanceState.DESTROYED
    assert compute.instances == {}
    assert not workspace.exists(job_id)
    assert report.observed_instances == []
    types = [e.event_type for e in await repo.list_events(job_id)]
    assert "reconciliation.recovering" in types and "instance.destroyed" in types


async def test_restart_requeues_claimed_but_unstarted_job(repo, workspace) -> None:  # type: ignore[no-untyped-def]
    job_id = await submit(repo, workspace, make_pdf())
    await repo.claim_next("worker-a", 5, 5)
    report = await reconciler(repo, FakeCompute(), workspace).reconcile(startup=True)
    assert report.requeued_jobs == [str(job_id)]
    job = await repo.get(job_id)
    assert job.status is JobStatus.QUEUED and job.worker_id is None
    assert workspace.exists(job_id)  # the upload is kept for the retry


async def test_live_job_of_other_worker_is_left_alone(repo, workspace) -> None:  # type: ignore[no-untyped-def]
    job_id = await submit(repo, workspace, make_pdf())
    await repo.claim_next("worker-b", 5, 5)
    await repo.transition(job_id, JobStatus.PROVISIONING, "sim")
    await repo.heartbeat(job_id, "worker-b")
    report = await reconciler(repo, FakeCompute(), workspace).reconcile(startup=True)
    assert report.recovered_jobs == []
    assert (await repo.get(job_id)).status is JobStatus.PROVISIONING


async def test_stale_heartbeat_of_other_worker_is_recovered(repo, workspace) -> None:  # type: ignore[no-untyped-def]
    job_id = await submit(repo, workspace, make_pdf())
    await repo.claim_next("worker-b", 5, 5)
    await repo.transition(job_id, JobStatus.PROVISIONING, "sim")
    await repo.update_fields(
        job_id,
        heartbeat_at=datetime.now(UTC) - timedelta(minutes=10),
        instance_state=InstanceState.PROVISIONING,
    )
    report = await reconciler(repo, FakeCompute(), workspace).reconcile(startup=False)
    assert report.recovered_jobs == [str(job_id)]


async def test_orphan_instance_of_terminal_job_is_destroyed(repo, workspace) -> None:  # type: ignore[no-untyped-def]
    job_id = await submit(repo, workspace, make_pdf())
    await mark_cleanup_failed(repo, job_id)
    name = instance_name_for(job_id)
    compute = FakeCompute()
    compute.instances[name] = InstanceInfo(name, "RUNNING")
    report = await reconciler(repo, compute, workspace).reconcile(startup=False)
    assert name in report.destroyed_orphans
    assert compute.instances == {}
    assert (await repo.get(job_id)).instance_state is InstanceState.DESTROYED


async def test_unknown_ephemera_instance_is_reported_not_deleted_by_default(
    repo, workspace
) -> None:  # type: ignore[no-untyped-def]
    foreign = instance_name_for(uuid.uuid4())
    compute = FakeCompute()
    compute.instances[foreign] = InstanceInfo(foreign, "RUNNING")
    report = await reconciler(repo, compute, workspace).reconcile(startup=False)
    assert report.unknown_instances == [foreign]
    assert foreign in compute.instances
    report = await reconciler(repo, compute, workspace, delete_unknown_instances=True).reconcile(
        startup=False
    )
    assert foreign not in compute.instances
