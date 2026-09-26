#!/usr/bin/env bash
# Ephemera GPU instance bootstrap.
#
# Copied into the job directory on the ephemeral instance and invoked by the
# orchestrator with a FIXED sub-command and the job directory:
#
#   bash bootstrap.sh <prepare|start-model|health|infer|cleanup> <job-dir>
#
# Result protocol: every handled outcome exits 0 and prints exactly one line
#   EPHEMERA_RESULT=ok                 or   EPHEMERA_RESULT=error:<reason>:<message>
# A non-zero exit therefore only ever means "transport failed". This matters because
# `brev exec` treats any non-zero exit as an SSH failure, refreshes its SSH config and
# RE-RUNS the command. Sub-commands are also idempotent for that case. No user-controlled data is ever passed as an argument;
# configuration comes from <job-dir>/runtime.env (non-secret) and
# <job-dir>/secrets.env (0600, deleted as soon as the model container starts).
#
set -Eeuo pipefail
umask 077

CMD="${1:-}"
JOB_DIR="${2:-}"
CONTAINER="ephemera-vllm"

# die <reason> <message>
die() { echo "EPHEMERA_RESULT=error:${1}:${2:-}"; exit 0; }
ok() { echo "EPHEMERA_RESULT=ok"; exit 0; }
trap 'echo "EPHEMERA_RESULT=error:unexpected:command failed at line $LINENO"; exit 0' ERR

case "$JOB_DIR" in
  /tmp/ephemera/jobs/*) ;;
  *) die usage "job dir must live under /tmp/ephemera/jobs" ;;
esac
[[ "$JOB_DIR" =~ ^/tmp/ephemera/jobs/[0-9a-f-]{36}$ ]] || die usage "malformed job dir"

load_runtime_env() {
  [[ -f "$JOB_DIR/runtime.env" ]] || die config "runtime.env missing"
  # shellcheck disable=SC1091
  set -a; source "$JOB_DIR/runtime.env"; set +a
  : "${MODEL_ID:?}" "${VLLM_IMAGE:?}" "${MAX_MODEL_LEN:?}" "${GPU_MEMORY_UTILIZATION:?}" \
    "${SERVED_MODEL_NAME:?}" "${PORT:?}"
}

docker_cmd() {
  if docker info >/dev/null 2>&1; then docker "$@"; else sudo -n docker "$@"; fi
}

cmd_prepare() {
  mkdir -p -m 700 "$JOB_DIR"
  command -v nvidia-smi >/dev/null || die no_gpu "nvidia-smi not found (no NVIDIA driver?)"
  local gpu
  gpu="$(nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits | head -n1)" \
    || die no_gpu "nvidia-smi failed"
  [[ -n "$gpu" ]] || die no_gpu "no NVIDIA GPU visible"
  command -v docker >/dev/null || die no_docker "docker not installed"
  docker_cmd info >/dev/null 2>&1 || die no_docker "docker daemon not reachable"
  if ! docker_cmd info --format '{{json .Runtimes}}' 2>/dev/null | grep -qi nvidia \
     && ! command -v nvidia-container-toolkit >/dev/null && ! command -v nvidia-ctk >/dev/null; then
    die no_runtime "NVIDIA container runtime not available"
  fi
  command -v curl >/dev/null || die no_curl "curl not installed"
  # name, memory MiB, driver
  echo "EPHEMERA_GPU=${gpu}"
  ok
}

cmd_start_model() {
  load_runtime_env
  docker_cmd pull --quiet "$VLLM_IMAGE" >/dev/null || die pull "failed to pull $VLLM_IMAGE"
  # Idempotent: replace any previous container from an earlier attempt.
  docker_cmd rm -f "$CONTAINER" >/dev/null 2>&1 || true
  local env_args=()
  if [[ -f "$JOB_DIR/secrets.env" ]]; then env_args=(--env-file "$JOB_DIR/secrets.env"); fi
  # Published on 127.0.0.1 only: the endpoint is never reachable from outside the instance.
  docker_cmd run -d --name "$CONTAINER" --gpus all --ipc=host \
    -p "127.0.0.1:${PORT}:8000" \
    -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
    "${env_args[@]}" \
    "$VLLM_IMAGE" \
    "$MODEL_ID" \
    --host 0.0.0.0 --port 8000 \
    --served-model-name "$SERVED_MODEL_NAME" \
    --max-model-len "$MAX_MODEL_LEN" \
    --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" >/dev/null || die start "failed to start model container"
  rm -f "$JOB_DIR/secrets.env"
  ok
}

cmd_health() {
  load_runtime_env
  local running
  running="$(docker_cmd inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo missing)"
  if [[ "$running" != "true" ]]; then
    die container_exited "model container not running (exit code $(docker_cmd inspect -f '{{.State.ExitCode}}' "$CONTAINER" 2>/dev/null || echo none))"
  fi
  if curl -sf --max-time 5 "http://127.0.0.1:${PORT}/health" >/dev/null; then
    ok
  fi
  die not_ready "model server still starting"
}

cmd_infer() {
  load_runtime_env
  [[ -f "$JOB_DIR/request.json" ]] || die no_request "request.json missing (already consumed?)"
  local code
  code="$(curl -s -o "$JOB_DIR/response.json" -w '%{http_code}' --max-time "${INFER_TIMEOUT:-230}" \
    -H 'Content-Type: application/json' \
    --data-binary "@$JOB_DIR/request.json" \
    "http://127.0.0.1:${PORT}/v1/chat/completions")" || { rm -f "$JOB_DIR/request.json"; die request "inference request failed or timed out"; }
  # The document payload is no longer needed on the instance.
  rm -f "$JOB_DIR/request.json"
  [[ "$code" == "200" ]] || die http "inference endpoint returned HTTP $code"
  ok
}

cmd_cleanup() {
  docker_cmd rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf -- "$JOB_DIR"
  [[ ! -e "$JOB_DIR" ]] || die cleanup "job dir still present"
  ok
}

case "$CMD" in
  prepare) cmd_prepare ;;
  start-model) cmd_start_model ;;
  health) cmd_health ;;
  infer) cmd_infer ;;
  cleanup) cmd_cleanup ;;
  *) die usage "unknown command" ;;
esac
