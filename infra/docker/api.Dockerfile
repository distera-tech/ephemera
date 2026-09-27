# syntax=docker/dockerfile:1.7
# Image for the Ephemera API *and* worker (same code, different command).
# Build context: repository root.

# --- Brev CLI, built from the Go module proxy at a pinned, verified version ---------------
FROM golang:1.25-bookworm AS brev
ARG BREV_CLI_VERSION=v0.6.335
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then cp /run/secrets/extra_ca /usr/local/share/ca-certificates/extra.crt && update-ca-certificates; fi \
 && GOBIN=/out go install github.com/brevdev/brev-cli@${BREV_CLI_VERSION} \
 && mv /out/brev-cli /out/brev

# --- Python dependencies -------------------------------------------------------------------
FROM python:3.12-bookworm AS deps
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NATIVE_TLS=1
WORKDIR /app/api
COPY apps/api/pyproject.toml apps/api/uv.lock ./
RUN --mount=type=secret,id=extra_ca,required=false \
    if [ -s /run/secrets/extra_ca ]; then cp /run/secrets/extra_ca /usr/local/share/ca-certificates/extra.crt \
      && update-ca-certificates && export PIP_CERT=/etc/ssl/certs/ca-certificates.crt; fi \
 && pip install --no-cache-dir uv==0.8.17 \
 && uv sync --frozen --no-dev --no-install-project

# --- Runtime -------------------------------------------------------------------------------
# The full (non-slim) image ships openssh-client, which `brev exec` / `brev copy` shell out
# to; this avoids an apt step (and its mirror dependency) entirely.
FROM python:3.12-bookworm
RUN ssh -V \
 && useradd --uid 10001 --create-home --shell /bin/bash ephemera \
 && mkdir -p /tmp/ephemera/jobs \
 && chown -R ephemera:ephemera /tmp/ephemera
COPY --from=brev /out/brev /usr/local/bin/brev
COPY --from=deps /opt/venv /opt/venv
COPY apps/api /app/api
COPY infra/gpu/bootstrap.sh /app/infra/gpu/bootstrap.sh
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app/api
USER ephemera
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')" || exit 1
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
