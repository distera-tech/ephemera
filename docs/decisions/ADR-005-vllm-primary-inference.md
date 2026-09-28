# ADR-005: vLLM as the primary inference engine; NIM behind the same port

**Status:** accepted

**Context.** We need an open-source, GPU-efficient server with an OpenAI-compatible API
and structured output, deployable by `docker run` on a fresh instance.

**Decision.** The official `vllm/vllm-openai` image (entrypoint `vllm serve`), chosen on
the instance from the NVIDIA driver (`VLLM_IMAGE=auto`): `v0.30.0` (CUDA 13 main build) on
driver ≥ 580, otherwise `v0.19.1`, the last release whose main build is CUDA 12.9. The
`v0.30.0-cu129` variant was rejected after it crashed on real Brev (`operator
torchvision::nms does not exist`). Used with `response_format: json_schema`, bound to `127.0.0.1` on the
instance. Access is via SSH-executed `curl` against localhost — no port forwarding,
no public port. `InferenceProvider` is the abstraction; `INFERENCE_ENGINE=nim` exists as
a configuration value that currently fails fast with a clear error.

We use the `-cu129` image variant: the plain `v0.30.0` tag is built on CUDA 13.0 and requires
NVIDIA driver ≥ 580, which many cloud GPU images do not ship yet. The image pull and container
start run in the background on the instance and are polled, because a multi-GB pull inside a
single SSH session is fragile. If the server rejects `response_format` (HTTP 400), the request
is retried once without it; output is schema-validated either way.

**Consequences.** NVIDIA NIM can be added as a provider reusing transfer/infer/cleanup
(same OpenAI API) with its own image, `NGC_API_KEY` login and cache path. vLLM's own API
key is not treated as a security boundary; network isolation is.
