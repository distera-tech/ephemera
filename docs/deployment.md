# Deployment

## Validation status

| Path | Status |
|---|---|
| Simulation mode (API, worker, PostgreSQL, UI, Compose) | **Validated** — unit/integration tests, Playwright E2E against `docker compose` |
| Real-mode code path (Brev adapter, GPU selection, vLLM bootstrap protocol, teardown) | **Validated against a fake `brev` CLI** that mirrors brev-cli v0.6.335 syntax and JSON output, plus the real `bootstrap.sh` run with stubbed `nvidia-smi`/`docker`/`curl` |
| **Real Brev GPU provisioning + real vLLM inference** | **Provisioning + destruction validated on real Brev (2026-09-27); inference pending re-run after the SSH fix.** Before that: The build environment had no `BREV_API_KEY` and its network policy blocked the Brev API and Hugging Face. It has **not** been run against real infrastructure. Run `scripts/real_smoke_test.py` (below) before relying on it. |

Brev CLI facts were verified from `brev --help` and source of v0.6.335 (built from the
Go module proxy), not from memory — see `docs/implementation-plan.md` §0.

## Local (simulation)

```bash
cp .env.example .env          # EPHEMERA_MODE=simulation — no credentials needed
make up                       # docker compose: postgres, migrate, api, worker, web
open http://localhost:3000
```

Without Docker: PostgreSQL 16 on localhost, then `make install migrate`, and `make api`,
`make worker`, `make web` in three terminals.

## Real mode (NVIDIA Brev)

1. **Credentials** (in `.env`, never committed):
   * `BREV_API_KEY` — the CLI reads it natively; the worker refuses to start in real mode
     without it (or `BREV_ALLOW_CLI_LOGIN=true` after `brev login` as the worker's OS user).
   * Do not point `BREV_HOME` anywhere other than the OS user's home directory: Brev writes its
     SSH config to `$HOME/.ssh/config`, but OpenSSH reads the passwd home and ignores `$HOME`,
     so ssh/scp to the instance would fail. Real mode refuses to start if they differ.
   * `BREV_ORG` — optional; the worker runs `brev set <org>` once.
   * `HF_TOKEN` — needed for gated models such as `meta-llama/Llama-3.1-8B-Instruct`.
     Accept the model licence on Hugging Face first and use a **read-only** token.
2. **Model**: `MODEL_ID` is configurable. Verify the licence, gating and that it fits the
   selected GPU (an 8B model in bf16 needs ~16 GB for weights; `GPU_MIN_VRAM_GB=40`
   leaves room for KV cache at `VLLM_MAX_MODEL_LEN=16384`). Llama models are
   *open-weight*, not open source.
3. **GPU**: `GPU_PREFERENCE=L40S`, `GPU_MIN_VRAM_GB=40`, fallbacks in
   `GPU_FALLBACK_TYPES`. Preview what would be chosen without creating anything:
   ```bash
   BREV_API_KEY=… brev search gpu --min-total-vram 40 --min-capability 8.0 --sort price
   ```
   Pin exact types with `BREV_INSTANCE_TYPES=type1,type2` if you want determinism.
4. **Image assumptions**: Brev VM-mode instances with NVIDIA driver, Docker and the NVIDIA
   container runtime. `bootstrap.sh prepare` verifies all three and fails the job
   (`BOOTSTRAP_FAILED`, GPU destroyed) otherwise. Commands run over `ssh <instance>`
   (Brev's SSH-access endpoint), falling back to `ssh <instance>-host`
   (`BREV_SSH_TARGET=auto|instance|host`), using the SSH config written by `brev refresh`; `BREV_SSH_CONNECT_TIMEOUT_SECONDS` (default 60) bounds each connection.
5. **Timeouts**: first runs pull the vLLM image (several GB) and the model weights (~16 GB for an 8B bf16 model) onto
   a fresh instance. Defaults: provisioning 2100 s (create + Brev setup + SSH, room for one
   provider switch), bootstrap 600 s, model ready 1500 s, job 5400 s.
   Measure with the smoke test and adjust.
6. **Start**: set `EPHEMERA_MODE=real` and `make up`. The UI shows "Real mode · Brev".

### Before spending money: outbound ports

Brev usually exposes an instance's SSH on a high, provider-assigned port. Networks that only
allow 80/443 outbound (corporate/school Wi-Fi, some VPNs and routers) make every connection
time out. Check first, with the stack running:

```bash
make net-check                         # from the worker container
python3 scripts/net_check.py           # from the host
```

If `portquiz.net:443` is OK but the high ports are `BLOCKED`, use another network (e.g. a
phone hotspot) or disable the VPN/firewall; real mode cannot work from that network.

### Real smoke test (costs money)

```bash
cd apps/api
uv run alembic upgrade head
uv run python ../../scripts/real_smoke_test.py --yes
```

It runs the production orchestrator twice — a normal job and one with inference failure
injected — prints per-phase timings, the GPU reported by `nvidia-smi`, all lifecycle
events, and fails unless both runs end with the instance destroyed and **no
`ephemera-*` instance remains** in `brev ls`. Stop the Compose worker first (or run the
test against a separate database), since both would compete for `MAX_GPU_INSTANCES`.

Record the output in this file when done:

| Date | GPU / type | Provisioning | Model loading | Inference | Destruction | GPU runtime | Result |
|---|---|---|---|---|---|---|---|
| 2026-09-27 | L40S (real Brev) | `brev create` 190–195 s | — | — | delete + verify absent ≈ 35–45 s | ≈ 5 min | **Partial.** Provisioning, deletion and verification of absence confirmed on real Brev (2 jobs, both `FAILED` → GPU `DESTROYED`, COMPUTE = 0). Bootstrap never started: `brev exec` could not resolve the instance because `BREV_HOME` ≠ OS home (OpenSSH ignores `$HOME`); the SSH probe was also not retried. Both fixed; re-run pending. |
| 2026-09-27 (run 3) | L40S (real Brev) | `brev create` 173 s | — | — | — | — | **Further.** SSH fix confirmed: `brev refresh` + `brev exec` OK, job reached BOOTSTRAPPING, remote mkdir and 2 uploads OK (each `brev copy` ≈ 24 s because it refreshes SSH config). A remote step then hung until the 300 s bootstrap timeout. Likely cause: Brev reports RUNNING before its instance setup (`build_status`) is COMPLETED. Fixed: wait for build COMPLETED, timeouts inside `bootstrap.sh`, per-step logging, larger time budgets. |
| 2026-09-27 (run 4) | L40S (real Brev) | `brev create` ≈ 3–3.5 min, then Brev setup until build COMPLETED | — | — | delete + verify absent ≈ 35–70 s | — | **Further.** Build-status wait confirmed. The SSH probe `brev exec … true` then failed after exactly ≈ 103 s (exit 1) — Brev's own SSH wait, 20 × 5 s attempts — and the job hit `PROVISIONING_TIMEOUT`; GPU destroyed. Fixed: `ssh`/`scp` are now called directly with Brev's SSH config and a 60 s connect timeout; `brev refresh` is repeated every 3 failed probes. |
| 2026-09-27 (run 5) | L40S (real Brev) | `brev create` 178 s, setup COMPLETED ≈ 1.5 min later | — | — | delete + verify absent ≈ 70 s | ≈ 21 min | **Blocked by network reachability.** Direct `ssh` worked as designed, but every TCP connection to the instance's SSH endpoint (`216.81.248.28:44689`, a provider-assigned high port) timed out for 15 min, until `PROVISIONING_TIMEOUT`; GPU destroyed. `make net-check` then showed outbound high ports open from the same machine, so the provider's SSH port forwarding was at fault. Added: after `BREV_SSH_UNREACHABLE_TIMEOUT_SECONDS` (300 s) of TCP-level failures the instance is destroyed and re-created on the next candidate from **another provider** (at most twice; `BREV_EXCLUDED_PROVIDERS` to skip one permanently), `make net-check` to test outbound ports without creating a GPU. |
| 2026-09-28 (run 6) | L40S: Crusoe → Scaleway → AWS g6e.xlarge (real Brev) | `brev create` 42 s / 250 s / 100 s | — | — | delete + verify absent ≈ 6 min | ≈ 35 min | **Root cause found.** The provider fallback worked (3 providers, one instance at a time, all destroyed), but SSH timed out on all three — so not the providers. Ephemera used the legacy `<name>-host` alias; brev-cli v0.6.335 now resolves a per-user **SSH-access** endpoint for `<name>` (what `brev shell`/`brev exec` use by default) and only keeps `-host` as a legacy fallback whose hostname/port pairing is not guaranteed. Fixed: probe `<name>` first, `<name>-host` second, pin the one that answers. |

## Operating notes

* Stop the worker with SIGTERM (`docker compose stop worker`), not SIGKILL: it cancels
  the running job and completes destroy + verify first (grace period 10 min).
* A job in `CLEANUP_FAILED` blocks new provisioning while `MAX_GPU_INSTANCES` is
  reached. Check `brev ls`; reconciliation retries every `RECONCILE_INTERVAL_SECONDS`.
* `RECONCILE_DELETE_UNKNOWN_INSTANCES=false` (default) means `ephemera-*` instances not in
  this database are only reported. Set it to `true` only if this deployment is the sole
  user of the `ephemera-` prefix in the Brev org.
* Ephemera never runs `brev delete` on anything that does not match `ephemera-<12 hex>`,
  and has no "delete all" code path.
* Behind a TLS-intercepting proxy, build with `EXTRA_CA_CERT=/path/ca.pem make up`; the CA
  is used during the build only and not baked into the runtime images.
* Put a real authentication layer in front before exposing beyond localhost
  (see threat model T10).
