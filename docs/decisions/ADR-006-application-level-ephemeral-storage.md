# ADR-006: Application-level ephemeral storage, short-lived results

**Status:** accepted

**Context.** Documents are sensitive in the target use case. We can control what the
application writes and deletes; we cannot control physical media at a cloud provider.

**Decision.**
* Uploads live in `DATA_DIR/<uuid>/document.pdf` (0700 dir, 0600 file; in-memory tmpfs
  in Compose) and are deleted in teardown on every exit path.
* PostgreSQL stores metadata only. The **validated analysis** is stored in
  `job_results` with an expiry (`RESULT_RETENTION_SECONDS`, default 1 h), purged by the
  worker and deletable on demand — the API and worker are separate processes, so the UI
  needs somewhere to read it from.
* Remote payloads are deleted immediately after use and the instance (with its disk) is
  destroyed.

**Consequences.** We say "application-level temporary data is deleted", never
"guaranteed/unrecoverable deletion". Results are derived confidential content; keeping
them briefly is an explicit, configurable trade-off rather than an accident.
