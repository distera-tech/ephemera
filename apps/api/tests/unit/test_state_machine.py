import pytest

from app.domain.models import CleanupResult, instance_name_for, is_ephemera_instance_name
from app.domain.status import (
    ALLOWED_TRANSITIONS,
    COMPUTE_PHASE_STATES,
    HAPPY_PATH,
    TERMINAL_STATES,
    InvalidTransitionError,
    JobStatus,
    assert_transition,
    can_transition,
)


def test_happy_path_is_a_legal_chain() -> None:
    for current, target in zip(HAPPY_PATH, HAPPY_PATH[1:], strict=False):
        assert can_transition(current, target), f"{current} -> {target}"


def test_terminal_states_are_final() -> None:
    for state in TERMINAL_STATES:
        assert ALLOWED_TRANSITIONS[state] == frozenset()


@pytest.mark.parametrize("state", sorted(COMPUTE_PHASE_STATES))
def test_compute_phases_cannot_skip_teardown(state: JobStatus) -> None:
    """While a GPU may exist, the only exits lead through CLEANING/DESTROYING."""
    for target in TERMINAL_STATES:
        assert not can_transition(state, target)
    assert can_transition(state, JobStatus.CLEANING)
    assert can_transition(state, JobStatus.DESTROYING)


def test_completed_only_reachable_after_verification() -> None:
    sources = [s for s, targets in ALLOWED_TRANSITIONS.items() if JobStatus.COMPLETED in targets]
    assert sources == [JobStatus.VERIFYING_DESTRUCTION]


def test_illegal_transition_raises() -> None:
    with pytest.raises(InvalidTransitionError):
        assert_transition(JobStatus.QUEUED, JobStatus.COMPLETED)
    with pytest.raises(InvalidTransitionError):
        assert_transition(JobStatus.INFERENCING, JobStatus.FAILED)


def test_every_state_has_transition_entry() -> None:
    assert set(ALLOWED_TRANSITIONS) == set(JobStatus)


def test_instance_naming_is_deterministic_and_guarded() -> None:
    import uuid

    job_id = uuid.UUID("8f32ab00-1111-2222-3333-444455556666")
    name = instance_name_for(job_id)
    assert name == "ephemera-8f32ab001111"
    assert is_ephemera_instance_name(name)
    for bad in [
        "ephemera-",
        "prod-db",
        "ephemera-XYZ",
        "ephemera-8f32ab001111; rm -rf /",
        "my-ephemera-8f32ab001111",
    ]:
        assert not is_ephemera_instance_name(bad)


def test_cleanup_result_success_rules() -> None:
    assert CleanupResult(local_deleted=True).succeeded  # nothing provisioned
    assert not CleanupResult(local_deleted=False).succeeded
    assert not CleanupResult(
        local_deleted=True, destroy_attempted=True, destroyed=True, destruction_verified=False
    ).succeeded
    # remote wipe failed but instance (and its disk) verifiably destroyed → acceptable
    assert CleanupResult(
        local_deleted=True,
        remote_deleted=False,
        destroy_attempted=True,
        destroyed=True,
        destruction_verified=True,
    ).succeeded
