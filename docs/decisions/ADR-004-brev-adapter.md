# ADR-004: All Brev interaction behind one adapter that shells out to the CLI

**Status:** accepted

**Context.** NVIDIA Brev is operated through its CLI (`brev create/ls/exec/copy/delete/search`).
CLI syntax drifts between versions; subprocess use is a classic injection and credential
leak vector.

**Decision.** `app/infrastructure/brev/` is the only code that runs `brev`.
`commands.py` builds argument vectors (pure, unit-tested, verified against v0.6.335
help/source); `client.py` runs them with `create_subprocess_exec`, explicit timeouts,
process-group kill, closed stdin, a sanitized environment and redacted errors;
`lifecycle.py` implements the `ComputeProvider` port. Every mutating builder requires an
`ephemera-<12 hex>` name. The Docker image builds the CLI from source at a pinned version.

**Consequences.** CLI changes are absorbed in one place; tests run the real client
against a fake `brev` binary with the same JSON shapes. Trade-off: we depend on the CLI's
behaviours, e.g. `brev exec` re-running commands after a non-zero exit — handled by the
bootstrap result-line protocol (see architecture.md).
