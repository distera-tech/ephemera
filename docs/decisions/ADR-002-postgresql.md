# ADR-002: PostgreSQL for jobs, events and coordination

**Status:** accepted

**Context.** Lifecycle state must survive restarts and be auditable; the worker needs
atomic claiming under concurrency limits.

**Decision.** PostgreSQL 16 with SQLAlchemy 2 (async, asyncpg) and Alembic migrations.
Tables: `jobs` (metadata only), `job_events` (append-only transitions), `job_results`
(validated analysis with `expires_at`), `worker_heartbeats` (liveness + provider-observed
instances).

**Consequences.** Row locks make transitions + event writes atomic; an advisory lock
serialises claims so `MAX_ACTIVE_JOBS`/`MAX_GPU_INSTANCES` hold across workers. The
schema deliberately has no column for document bytes, text, prompts or raw output
(enforced by `test_schema_has_no_document_body_columns`).
