# Ephemera — Implementation Plan

Status legend: ✅ done · 🟡 implemented, real-infra validation pending · ⬜ not started

## 0. Observations (environment inspection, 2026-09-26)

| Item | Finding | Consequence |
|---|---|---|
| Repository | Empty (no commits) | Greenfield monorepo |
| Python | 3.11 default, 3.12 / 3.13 available, `uv` 0.8 | Backend targets Python 3.12 via `uv` |
| Node | v22, npm 10, pnpm 10 | Frontend with npm |
| Docker | Engine 29.3 + Compose v5 (daemon startable) | Compose stack validated locally |
| PostgreSQL | 16 installed locally | Integration tests run against real Postgres |
| Brev CLI | Not installed. Built **v0.6.335** from the Go module proxy (`go install github.com/brevdev/brev-cli@v0.6.335`) and captured `--help` + source for `search`, `create`, `ls`, `exec`, `copy`, `delete`, `port-forward`, `login`, `set` | The adapter is written against verified syntax and JSON shapes, not memory |
| Brev API reachability | `brev search` → `Forbidden` (sandbox egress policy) | Real provisioning cannot be exercised from this sandbox |
| Credentials | `BREV_API_KEY`, `HF_TOKEN`, `NGC_API_KEY` unset | **Real Brev validation pending credentials** |
| vLLM | Latest release `v0.30.0`; image entrypoint `vllm serve`; structured output via `response_format: json_schema` | `VLLM_IMAGE=vllm/vllm-openai:v0.30.0` pinned, overridable |
| Hugging Face | Blocked by sandbox egress | Model licence/gating must be checked by the operator before real deployment |

### Verified Brev CLI facts that shape the design

* `BREV_API_KEY` in the environment authenticates the CLI (pkg/auth/auth.go); `BREV_NO_ANALYTICS=1` disables telemetry.
* `brev search gpu --json` returns an array of `{type, provider, gpu_name, gpu_count, vram_per_gpu_gb, total_vram_gb, capability, price_per_hour, boot_time_seconds, ...}`.
* `brev create NAME --type a,b,c --timeout S` tries types in order (fallback chain) and waits for readiness.
* `brev ls --json` returns `{"workspaces":[{name,id,status,build_status,shell_status,health_status,instance_type,instance_kind,gpu}]}`; statuses include `RUNNING, STARTING, STOPPING, DEPLOYING, STOPPED, DELETING, FAILURE`.
* `brev exec NAME [--host] CMD` runs over SSH. On a failed first attempt it looks up the instance and **re-runs the command** → every remote command must be idempotent. The command string is embedded into a local `bash -c` → it must never contain user-controlled data.
* `brev copy SRC DST [--host]` uses `instance:/path` notation.
* `brev delete NAME` deletes by name or id.

## Phases

| # | Phase | Objective | Key files | Acceptance criteria | Tests | Status |
|---|---|---|---|---|---|---|
| 1 | Domain | State machine, error codes, value objects, ports | `app/domain/*` | Illegal transitions rejected; terminal states final | `test_state_machine.py` | ✅ |
| 2 | Persistence | `jobs`, `job_events`, `worker_heartbeats`; Alembic | `app/infrastructure/database/*`, `alembic/` | `alembic upgrade head` on empty DB; no document body columns | `test_repository.py` | ✅ |
| 3 | API | Upload, list, detail, events, cancel, health, readiness, stats | `app/api/*` | 202 on create; oversized/non-PDF rejected before any compute | `test_api.py` | ✅ |
| 4 | Documents | PyMuPDF extraction in a sandboxed subprocess with timeout + page limit | `app/infrastructure/documents/*` | Malformed PDF → `DOCUMENT_INVALID` before provisioning | `test_documents.py` | ✅ |
| 5 | Orchestrator | PROVISION→INFER→CLEAN→DESTROY→VERIFY with `finally` destroy | `app/application/orchestrator.py` | Destroy attempted on every exit path after a provision attempt | `test_orchestrator.py` (10 critical tests) | ✅ |
| 6 | Simulation | `EPHEMERA_MODE=simulation` compute + inference providers | `app/infrastructure/compute/simulated.py`, `inference/simulated.py` | Full lifecycle without credentials; UI badge | `test_e2e_simulation.py` | ✅ |
| 7 | Brev adapter | `BrevClient` + `BrevComputeProvider`, GPU selection | `app/infrastructure/brev/*` | Arg arrays, timeouts, sanitized env, prefix guard on delete | `test_brev_client.py` against a fake `brev` binary | 🟡 |
| 8 | vLLM | `VLLMProvider` over the remote executor; bootstrap script | `app/infrastructure/inference/vllm.py`, `infra/gpu/bootstrap.sh` | Localhost-only endpoint, JSON-schema output, retry once | fake-brev real-mode E2E | 🟡 |
| 9 | Worker | Claim loop (`SKIP LOCKED`), limits, heartbeats, SIGTERM handling, reconciliation | `app/workers/*`, `app/application/reconciliation.py` | Restart recovers in-flight jobs and destroys their instances | `test_reconciliation.py` | ✅ |
| 10 | Frontend | Control plane, job timeline, COMPUTE = 0 hero, landing, architecture | `apps/web/*` | Reflects backend state via polling | Playwright `lifecycle.spec.ts` | ✅ |
| 11 | Ops | Dockerfiles, Compose, Makefile, CI | `infra/*`, `Makefile`, `.github/workflows/ci.yml` | `docker compose up` works in simulation | manual + CI | ✅ |
| 12 | Docs | README, architecture, threat model, demo, deployment, ADRs | `docs/*` | No unsupported security claims | review | ✅ |
| 13 | Real validation | Smoke test on a real Brev GPU | `scripts/real_smoke_test.py` | Timings recorded | — | ⬜ pending credentials + network |

## Dependencies

Backend: fastapi, uvicorn, pydantic v2, pydantic-settings, sqlalchemy 2 (async), asyncpg, alembic, pymupdf, python-multipart, httpx (tests).
Frontend: next, react, tailwindcss v4, @playwright/test.
No Redis, Celery, Kafka, Kubernetes, Terraform (see ADR-003).
