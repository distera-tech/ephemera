# Demo script (≈ 4 minutes)

Goal: in under a minute a judge should see **GPU = 0 → GPU = 1 → GPU = 0**, and then see
that a failed job still returns to zero.

## Before the demo

```bash
cp .env.example .env
# Real demo: EPHEMERA_MODE=real, BREV_API_KEY, BREV_ORG, HF_TOKEN (model licence accepted)
# Offline rehearsal: keep EPHEMERA_MODE=simulation (optionally SIMULATION_TIME_SCALE=0.5)
make up
open http://localhost:3000/dashboard
```

Checklist:
* `curl localhost:8000/api/readiness` → `"worker":"ok"`.
* Dashboard shows **0 GPU INSTANCES** and **COMPUTE = 0 — Confirmed by … provider listing**.
* In real mode, run `scripts/real_smoke_test.py --yes` once beforehand: the first run
  downloads the model onto the fresh instance, so rehearse timings (see
  [deployment.md](deployment.md)). Be ready to narrate provisioning — it can take
  several minutes and Ephemera never promises "instant GPU".
* Have `examples/demo-documents/sample-contract.pdf` and `sample-policy.pdf` ready.
* If you present in simulation mode, **say so**: the amber "SIMULATION MODE" badge is on
  every screen and the summary starts with "[Simulated analysis — no LLM was run]".

## Script

| # | Screen | Action | Say |
|---|---|---|---|
| 1 | Dashboard | Point at the hero card: **0 GPU INSTANCES / COMPUTE = 0** | "There is no GPU running. Not idle — not there." |
| 2 | Dashboard | Choose `sample-contract.pdf`, *Contract review*, **Analyze document** | "Upload a (synthetic) contract." |
| 3 | Job page | `QUEUED` → `PROVISIONING GPU`, banner **GPU INSTANCE: PROVISIONING** (amber) | "Ephemera is asking Brev for an L40S right now, with fallbacks if none is free." |
| 4 | Job page | **GPU INSTANCE: ACTIVE**, Compute panel shows requested vs actual GPU, VRAM | "Now there is exactly one GPU, and it exists only for this job." |
| 5 | Job page | `BOOTSTRAPPING` → `MODEL LOADING` → `INFERENCING` | "It verified the GPU with nvidia-smi, started vLLM bound to localhost only, and sent the document as untrusted data." |
| 6 | Job page | Result appears (summary, risks, human-review flag) | "Structured, schema-validated output." |
| 7 | Job page | `CLEANING` → `DESTROYING GPU` → `VERIFYING DESTRUCTION` | "Temporary data is deleted, the instance is deleted, and we check Brev until it's really gone." |
| 8 | Job page | **GPU INSTANCE: DESTROYED**, **COMPUTE = 0**, GPU runtime e.g. `00:03:42` | "**The job is finished. The compute is gone.**" |
| 9 | Dashboard | Hero back to **0**, "Confirmed by Brev provider listing" | "That zero is Brev's own instance list, not just our database." |

### Failure mode (the proof)

| # | Action | Say |
|---|---|---|
| 10 | Dashboard → tick **Inject inference failure** → `sample-policy.pdf` → Analyze | "Now let's break it on purpose, after the GPU is up." |
| 11 | Job page reaches `INFERENCING`, then **INFERENCE FORCED FAILURE** | "Inference failed." |
| 12 | Error panel shows **Cleanup successful** and **GPU destroyed**; banner **DESTROYED**; **COMPUTE = 0** | "And the GPU is still destroyed. That's the invariant: if we provision it, we destroy it — success, failure, timeout, cancellation or shutdown." |

Optional: upload `prompt-injection-test.pdf` — the document tells the model to reveal
its prompt and keys; the result instead lists it as a **high** risk.

Alternative to the checkbox: start the worker with `DEMO_FORCE_INFERENCE_FAILURE=true`
(every job fails at inference).

## Cost talking point

Show the job's **GPU runtime** and "CREATED → USED → DESTROYED". Say: *"Ephemera is
designed for workloads where GPU demand is intermittent, so compute is not kept active
between jobs."* The estimated cost shown is Brev's listed hourly price × observed
runtime, and "unavailable" when no price was reported. Do not quote savings percentages.

## If something goes wrong live

* Provisioning slow → it's real; narrate the timeline and the provisioning timeout.
* A job ends `CLEANUP_FAILED` → that is the system being honest: the dashboard will not
  show COMPUTE = 0, new jobs are blocked, and reconciliation retries destruction.
  Check `brev ls` and the worker logs (`make logs`).
