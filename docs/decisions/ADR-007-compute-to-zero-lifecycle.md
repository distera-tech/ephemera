# ADR-007: Compute-to-Zero lifecycle as the core invariant

**Status:** accepted

**Context.** The product claim is that a GPU exists only for the workload. That claim is
worthless if a failure path can leave an instance running.

**Decision.**
1. If provisioning was *attempted*, teardown destroys and verifies — on success, error,
   timeout, cancellation and worker shutdown; teardown is shielded from cancellation and
   each step is best-effort so one failure cannot skip destruction.
2. Success is defined by verification against the provider's listing, not by a delete
   call's exit code. Unverified destruction ends the job in `CLEANUP_FAILED`, keeps the
   instance counted as active and blocks new provisioning.
3. Reconciliation covers what `finally` cannot (SIGKILL, host loss).
4. "COMPUTE = 0" in the UI requires both the database and the latest provider listing to
   agree.

**Consequences.** Some latency is spent on verification polling, and a Brev API outage
can make a job `CLEANUP_FAILED` even if deletion eventually succeeded — we prefer a
visible, conservative failure to an unverified success claim.
