# ADR-001: FastAPI for the control-plane API

**Status:** accepted

**Context.** The API validates uploads, persists metadata and returns immediately
(HTTP 202). GPU work happens elsewhere. We need typed request/response models, an
OpenAPI document for the typed frontend client, and async database access.

**Decision.** FastAPI + Pydantic v2 + Uvicorn.

**Consequences.** OpenAPI comes for free (`/openapi.json`, `/docs`); the same Pydantic
models validate model output (`DocumentAnalysis`). The API never blocks on
provisioning, so request latency is independent of GPU availability.
