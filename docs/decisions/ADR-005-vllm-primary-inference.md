# ADR-005: vLLM as the primary inference engine; NIM behind the same port

**Status:** accepted

**Context.** We need an open-source, GPU-efficient server with an OpenAI-compatible API
and structured output, deployable by `docker run` on a fresh instance.

**Decision.** `vllm/vllm-openai:v0.30.0` (entrypoint `vllm serve`, verified from the
tagged Dockerfile) with `response_format: json_schema`, bound to `127.0.0.1` on the
instance. Access is via SSH-executed `curl` against localhost — no port forwarding,
no public port. `InferenceProvider` is the abstraction; `INFERENCE_ENGINE=nim` exists as
a configuration value that currently fails fast with a clear error.

**Consequences.** NVIDIA NIM can be added as a provider reusing transfer/infer/cleanup
(same OpenAI API) with its own image, `NGC_API_KEY` login and cache path. vLLM's own API
key is not treated as a security boundary; network isolation is.
