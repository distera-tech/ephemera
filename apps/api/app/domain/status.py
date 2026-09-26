"""Job lifecycle state machine.

The lifecycle is modelled explicitly: every transition is validated against
``ALLOWED_TRANSITIONS`` and persisted with a timestamped event by the
repository. There is deliberately no path from an "active compute" state to a
terminal state that bypasses destruction: failures route through CLEANING →
DESTROYING → VERIFYING_DESTRUCTION.
"""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    QUEUED = "QUEUED"
    PROVISIONING = "PROVISIONING"
    BOOTSTRAPPING = "BOOTSTRAPPING"
    MODEL_LOADING = "MODEL_LOADING"
    READY = "READY"
    TRANSFERRING = "TRANSFERRING"
    INFERENCING = "INFERENCING"
    CLEANING = "CLEANING"
    DESTROYING = "DESTROYING"
    VERIFYING_DESTRUCTION = "VERIFYING_DESTRUCTION"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    CANCELLED = "CANCELLED"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATES

    @property
    def is_compute_phase(self) -> bool:
        """States in which a GPU instance may exist or be in the middle of creation."""
        return self in COMPUTE_PHASE_STATES


TERMINAL_STATES: frozenset[JobStatus] = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CLEANUP_FAILED, JobStatus.CANCELLED}
)

COMPUTE_PHASE_STATES: frozenset[JobStatus] = frozenset(
    {
        JobStatus.PROVISIONING,
        JobStatus.BOOTSTRAPPING,
        JobStatus.MODEL_LOADING,
        JobStatus.READY,
        JobStatus.TRANSFERRING,
        JobStatus.INFERENCING,
    }
)

# Happy-path order, used by the UI timeline and by tests.
HAPPY_PATH: tuple[JobStatus, ...] = (
    JobStatus.QUEUED,
    JobStatus.PROVISIONING,
    JobStatus.BOOTSTRAPPING,
    JobStatus.MODEL_LOADING,
    JobStatus.READY,
    JobStatus.TRANSFERRING,
    JobStatus.INFERENCING,
    JobStatus.CLEANING,
    JobStatus.DESTROYING,
    JobStatus.VERIFYING_DESTRUCTION,
    JobStatus.COMPLETED,
)

_TEARDOWN = {JobStatus.CLEANING, JobStatus.DESTROYING}

ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    # Before provisioning no compute exists, so a job may fail/cancel after local cleanup.
    JobStatus.QUEUED: frozenset({JobStatus.PROVISIONING, JobStatus.CLEANING}),
    JobStatus.PROVISIONING: frozenset({JobStatus.BOOTSTRAPPING, *_TEARDOWN}),
    JobStatus.BOOTSTRAPPING: frozenset({JobStatus.MODEL_LOADING, *_TEARDOWN}),
    JobStatus.MODEL_LOADING: frozenset({JobStatus.READY, *_TEARDOWN}),
    JobStatus.READY: frozenset({JobStatus.TRANSFERRING, *_TEARDOWN}),
    JobStatus.TRANSFERRING: frozenset({JobStatus.INFERENCING, *_TEARDOWN}),
    JobStatus.INFERENCING: frozenset(_TEARDOWN),
    # CLEANING → terminal is only legal when no provisioning was ever attempted;
    # the orchestrator enforces that (see Orchestrator._teardown).
    JobStatus.CLEANING: frozenset(
        {
            JobStatus.DESTROYING,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.CLEANUP_FAILED,
        }
    ),
    JobStatus.DESTROYING: frozenset({JobStatus.VERIFYING_DESTRUCTION, JobStatus.CLEANUP_FAILED}),
    JobStatus.VERIFYING_DESTRUCTION: frozenset(
        {
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.CLEANUP_FAILED,
        }
    ),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
    JobStatus.CLEANUP_FAILED: frozenset(),
    JobStatus.CANCELLED: frozenset(),
}


class InvalidTransitionError(ValueError):
    def __init__(self, current: JobStatus, target: JobStatus) -> None:
        super().__init__(f"illegal job transition {current} -> {target}")
        self.current = current
        self.target = target


def can_transition(current: JobStatus, target: JobStatus) -> bool:
    return target in ALLOWED_TRANSITIONS[current]


def assert_transition(current: JobStatus, target: JobStatus) -> None:
    if not can_transition(current, target):
        raise InvalidTransitionError(current, target)


class DocumentState(StrEnum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    TRANSFERRED = "TRANSFERRED"
    PROCESSED = "PROCESSED"
    CLEANED = "CLEANED"
    FAILED = "FAILED"


class InstanceState(StrEnum):
    """What Ephemera believes about the job's GPU instance.

    ``NONE`` — no provisioning attempted. Everything except ``NONE`` and
    ``DESTROYED`` counts as potentially billable compute.
    """

    NONE = "NONE"
    PROVISIONING = "PROVISIONING"
    ACTIVE = "ACTIVE"
    DESTROYING = "DESTROYING"
    DESTROYED = "DESTROYED"
    DESTROY_FAILED = "DESTROY_FAILED"

    @property
    def counts_as_active(self) -> bool:
        return self not in (InstanceState.NONE, InstanceState.DESTROYED)
