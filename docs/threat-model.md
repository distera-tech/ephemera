# Threat model

Scope: the Ephemera MVP (control plane + worker + ephemeral Brev GPU instance). The
prototype is **single-user and demo-only**, intended for synthetic/public data. This
document states mitigations that are implemented and tested, and the risk that remains.
It is not a claim of security certification or regulatory compliance.

## Assets

* Document contents, extracted text, prompts, model output
* Credentials: `BREV_API_KEY`, `HF_TOKEN`, `NGC_API_KEY`, database password
* The organisation's Brev account (money, other instances)
* Availability of the service

## Trust boundaries

1. Browser ↔ Next.js origin (untrusted user input)
2. Next.js ↔ API (internal network)
3. Worker ↔ Brev CLI/API (credentials)
4. Worker ↔ GPU instance over Brev-managed SSH
5. Model server ↔ document text (untrusted content inside the model's context)

## Threats

### T1 — Prompt injection
* **Attack surface:** document text placed into the model context.
* **Mitigations:** document never enters the system prompt; system prompt declares the
  document untrusted and forbids following embedded instructions or revealing
  instructions; text is enclosed between markers carrying a per-request random token and
  any occurrence of the token or chat-template control tokens (`<|eot_id|>`, …) is
  neutralised; output is constrained by a JSON schema and validated. **The model has
  zero tools**: it cannot run commands, read files, reach credentials or call Brev.
  (`tests/unit/test_prompts_and_parsing.py`, `test_9_prompt_injection_is_treated_as_data`)
* **Residual risk:** an injection can still bias the *content* of the analysis (e.g. an
  understated risk). Results are therefore labelled "human review required" by default
  and must not be used for automated decisions.

### T2 — Malicious PDF
* **Attack surface:** PyMuPDF parsing.
* **Mitigations:** extraction runs in a **separate process** with `RLIMIT_AS` (1 GiB),
  `RLIMIT_CPU` (60 s), a wall-clock timeout (kill on expiry), a minimal environment and no
  stdout echo of text; page limit; encrypted PDFs rejected; extraction happens **before
  any GPU is provisioned**. (`tests/unit/test_documents.py`)
* **Residual risk:** a PyMuPDF memory-safety bug could compromise the worker process
  (which holds the Brev key). Stronger isolation (separate container / seccomp /
  gVisor) is on the roadmap.

### T3 — Oversized document
* **Mitigations:** size enforced while streaming (413 as soon as the limit is crossed;
  nothing is written), Next.js proxy body limit set above the API limit so the API is
  authoritative, page and character limits, queue length limit.
* **Residual risk:** a 25 MB upload is buffered in API memory; acceptable at MVP scale.

### T4 — Malicious filename
* **Mitigations:** the user filename is display metadata only — NFKC-normalised, path
  components dropped, restricted character set, 120 chars. Files are stored as
  `document.pdf` under a server-generated UUID directory. The filename never reaches a
  path, a shell or the Brev CLI. React renders it as text. (`test_filename_is_sanitized_for_display`)

### T5 — Path traversal
* **Mitigations:** job directories derive only from `uuid.UUID` objects and are checked to
  stay under the workspace root; `scp` paths must match `^/[A-Za-z0-9._/-]+$`
  without `..`; `bootstrap.sh` rejects any job dir not matching
  `/tmp/ephemera/jobs/<uuid>`. (`test_copy_rejects_unsafe_paths`, `test_rejects_job_dirs_outside_scheme`)

### T6 — Command injection
* **Attack surface:** the remote command is passed to `ssh` and interpreted by the
  instance's login shell.
* **Mitigations:** `create_subprocess_exec` with argument arrays (never `shell=True`);
  remote commands are a fixed set built from constants plus `shlex.quote`d UUID paths;
  single-line/length checks; instance names must match `ephemera-<12 hex>`; model id and
  image are passed via a `runtime.env` file with `shlex.quote`d values, not the command
  line. (`test_security_controls.py`, `test_real_mode_success_path` asserts every remote
  command is one of the fixed forms)
* **Residual risk:** `MODEL_ID` / `VLLM_IMAGE` are operator-controlled configuration and
  trusted.

### T7 — Credential leakage
* **Mitigations:** secrets only in environment variables (`SecretStr`), never returned by
  the API (`/api/config` exposes no secret), `.env` git-ignored. JSON logs redact
  registered secret values and common token patterns (`hf_…`, `nvapi-…`, bearer,
  `key=value`), exceptions are logged as type + message only (no tracebacks with locals).
  The Brev CLI gets a **sanitized environment** (PATH, isolated HOME, proxy, its own key —
  not `HF_TOKEN` or `DATABASE_URL`). `HF_TOKEN` reaches the instance as a 0600 file over
  SSH, never on a command line, and is deleted after the container starts.
  (`test_registered_secrets_never_reach_logs`, `test_environment_is_sanitized`,
  `test_real_mode_hf_token_never_on_command_line`)
* **Residual risk:** `HF_TOKEN` is present in the vLLM container environment on the
  instance until it is destroyed; use a read-only, narrowly scoped token. The worker
  host holds `BREV_API_KEY`; compromise of the worker = compromise of the Brev org.

### T8 — Orphan GPU instance (cost + data persistence)
* **Mitigations:** the destroy-in-`finally` invariant (see architecture.md), shielded
  teardown, destroy retries, verification via `brev ls`, persisted `instance_state`
  before `brev create`, reconciliation on start-up/periodically/after each job,
  leaked instances block new provisioning (`MAX_GPU_INSTANCES`), deterministic names,
  10-minute Compose stop grace period for the worker.
  (`test_orchestrator.py` 1–5 + timeout/cancel/shutdown/DB-outage, `test_limits_and_reconciliation.py`)
* **Residual risk:** if the Brev API is unavailable for longer than the verification
  window, the instance may live until reconciliation succeeds; the job shows
  `CLEANUP_FAILED` and the dashboard does not claim COMPUTE = 0. Operators should also
  set a provider-side budget alert.

### T9 — Public inference endpoint
* **Mitigations:** vLLM is published on `127.0.0.1` only (`-p 127.0.0.1:8000:8000`);
  requests are issued from the instance itself via SSH; no `brev port-forward` or public
  port is opened; Jupyter disabled at create (`--jupyter=false`).
  (`test_start_model_binds_localhost_only_and_deletes_secrets`)
* **Residual risk:** other processes on the instance (Brev's own agents) can reach
  localhost. vLLM's API key is not relied upon.

### T10 — Unauthorized job access
* **Mitigations:** jobs addressed by random UUIDs; every endpoint goes through an
  ownership check (`current_owner()`), currently a single demo owner.
* **Residual risk:** **there is no authentication**. Anyone who can reach the web port can
  create jobs and read results. Compose binds to `127.0.0.1` by default. Do not expose
  the MVP to untrusted networks.

### T11 — Model output injection
* **Mitigations:** output parsed as JSON and validated against a strict, size-bounded
  Pydantic schema (`extra="forbid"`, enum severities, list/string caps); invalid output
  is rejected (one stricter retry, then `INFERENCE_OUTPUT_INVALID`) and never echoed into
  errors or logs; the UI renders values as React text nodes (no HTML/markdown).
* **Residual risk:** plausible but wrong content (hallucination).

### T12 — Denial of service via jobs
* **Mitigations:** `MAX_ACTIVE_JOBS`, `MAX_GPU_INSTANCES` (atomic, advisory-locked
  claim), `MAX_QUEUED_JOBS` (429), per-step timeouts and `MAX_JOB_RUNTIME_SECONDS`, size
  and page limits. Cost exposure is bounded by concurrency × runtime.
* **Residual risk:** no per-user rate limiting (no users yet); queue can be filled by
  anyone with access (see T10).

## Explicit non-claims

Ephemera does **not** claim: absolute security or confidentiality; regulatory compliance
(HIPAA, GDPR, …); guaranteed or unrecoverable deletion from physical media; "zero trust".
It provides ephemeral compute, application-level temporary data deletion, reduced
persistence, and an auditable lifecycle.
