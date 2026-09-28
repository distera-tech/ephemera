#!/usr/bin/env bash
# Ephemera GPU instance bootstrap.
#
# Copied into the job directory on the ephemeral instance and invoked by the
# orchestrator with a FIXED sub-command and the job directory:
#
#   bash bootstrap.sh <prepare|start-model|health|infer|cleanup> <job-dir>
#
# start-model only *launches* the image pull + container start in the background
# (a multi-GB pull inside one SSH session is fragile); `health` then reports the phase
# from <job-dir>/start.status: pulling → starting → started, or failed:<reason>:<msg>.
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

# VLLM_IMAGE=auto: pick the image from the NVIDIA driver. CUDA 13 builds need driver >= 580.
# The resolved image is recorded in $JOB_DIR/image so health messages name it.
resolve_image() {
  if [[ "$VLLM_IMAGE" == "auto" ]]; then
    local driver major
    driver="$(timeout 60 nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -n1 || true)"
    major="${driver%%.*}"
    if [[ "$major" =~ ^[0-9]+$ ]] && (( major >= 580 )); then
      VLLM_IMAGE="${VLLM_IMAGE_CUDA13:?}"
    else
      VLLM_IMAGE="${VLLM_IMAGE_CUDA12:?}"
    fi
  fi
  echo "$VLLM_IMAGE" > "$JOB_DIR/image"
}

resolved_image() { cat "$JOB_DIR/image" 2>/dev/null || echo "$VLLM_IMAGE"; }

# Decide once (with bounded probes) whether docker needs sudo. A bare `docker info` can hang
# while the instance is still being set up, so every probe has a timeout.
DOCKER=()
detect_docker() {
  if [[ ${#DOCKER[@]} -gt 0 ]]; then return 0; fi
  if timeout 20 docker info >/dev/null 2>&1; then DOCKER=(docker); return 0; fi
  if timeout 20 sudo -n docker info >/dev/null 2>&1; then DOCKER=(sudo -n docker); return 0; fi
  return 1
}
docker_cmd() {
  detect_docker || die no_docker "docker daemon not reachable"
  "${DOCKER[@]}" "$@"
}

# Optional runtime settings for sub-commands that can run before/without runtime.env.
load_runtime_env_optional() {
  if [[ -f "$JOB_DIR/runtime.env" ]]; then set -a; source "$JOB_DIR/runtime.env"; set +a; fi
}

# Last relevant lines of the model container log, for error messages. Never includes tokens.
container_hint() {
  local logs hint
  logs="$("${DOCKER[@]}" logs --tail 400 "$CONTAINER" 2>&1 | grep -viE "hf_[a-z0-9]|token=" || true)"
  # Prefer the final exception / known causes; otherwise the last lines of the log.
  hint="$(printf '%s\n' "$logs" \
    | grep -iE "error|exception|denied|gated|unauthori|401|403|restricted|out of memory|cuda|driver|no space|killed" \
    | tail -n 3 || true)"
  [[ -n "$hint" ]] || hint="$(printf '%s\n' "$logs" | grep -v '^[[:space:]]*$' | tail -n 3 || true)"
  printf '%s' "$hint" | tr '\n' ' ' | tr -s ' ' | cut -c1-600
}

cmd_prepare() {
  mkdir -p -m 700 "$JOB_DIR"
  load_runtime_env_optional
  command -v nvidia-smi >/dev/null || die no_gpu "nvidia-smi not found (no NVIDIA driver?)"
  local gpu
  gpu="$(timeout 90 nvidia-smi --query-gpu=name,memory.total,driver_version --format=csv,noheader,nounits | head -n1)" \
    || die no_gpu "nvidia-smi failed or timed out"
  [[ -n "$gpu" ]] || die no_gpu "no NVIDIA GPU visible"
  command -v docker >/dev/null || die no_docker "docker not installed"
  # The daemon may still be (re)starting right after instance setup: wait up to ~3 min.
  local tries=0
  until detect_docker; do
    tries=$((tries + 1))
    [[ $tries -ge ${EPHEMERA_DOCKER_WAIT_TRIES:-9} ]] && die no_docker "docker daemon not reachable after waiting"
    sleep "${EPHEMERA_DOCKER_WAIT_SLEEP:-20}"
  done
  if ! timeout 20 "${DOCKER[@]}" info --format '{{json .Runtimes}}' 2>/dev/null | grep -qi nvidia \
     && ! command -v nvidia-container-toolkit >/dev/null && ! command -v nvidia-ctk >/dev/null; then
    die no_runtime "NVIDIA container runtime not available"
  fi
  command -v curl >/dev/null || die no_curl "curl not installed"
  # Image (~13 GB) + model weights (~16 GB for 8B bf16) need room; fail before downloading.
  local root avail
  root="$(timeout 20 "${DOCKER[@]}" info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
  [[ -e "$root" ]] || root=/
  avail="$(df -BG --output=avail "$root" 2>/dev/null | tail -n1 | tr -dc '0-9')"
  if [[ -n "$avail" && "$avail" -lt "${MIN_FREE_DISK_GB:-50}" ]]; then
    die no_disk "only ${avail} GB free for docker (need ${MIN_FREE_DISK_GB:-50} GB): pick an instance type with a larger disk"
  fi
  echo "EPHEMERA_DISK_FREE_GB=${avail:-unknown}"
  # name, memory MiB, driver
  echo "EPHEMERA_GPU=${gpu}"
  ok
}

cmd_start_model() {
  load_runtime_env
  detect_docker || die no_docker "docker daemon not reachable"
  # Idempotent: a start already in progress (or finished) is not launched twice.
  if [[ -f "$JOB_DIR/start.pid" ]] && kill -0 "$(cat "$JOB_DIR/start.pid")" 2>/dev/null; then ok; fi
  if [[ "$(cat "$JOB_DIR/start.status" 2>/dev/null || true)" == "started" ]]; then ok; fi
  echo "pulling" > "$JOB_DIR/start.status"
  setsid nohup bash "$0" _start-bg "$JOB_DIR" > "$JOB_DIR/start.log" 2>&1 < /dev/null &
  echo "$!" > "$JOB_DIR/start.pid"
  ok
}

# Runs detached. Reports progress only through start.status (never stdout).
cmd_start_bg() {
  local status="$JOB_DIR/start.status"
  trap 'echo "failed:unexpected:start failed at line $LINENO" > "$status"; rm -f "$JOB_DIR/secrets.env"; exit 0' ERR
  load_runtime_env
  resolve_image
  detect_docker || { echo "failed:no_docker:docker daemon not reachable" > "$status"; exit 0; }
  if ! "${DOCKER[@]}" pull --quiet "$VLLM_IMAGE" > "$JOB_DIR/pull.log" 2>&1; then
    echo "failed:pull:failed to pull $VLLM_IMAGE ($(tail -c 160 "$JOB_DIR/pull.log" | tr '\n' ' '))" > "$status"
    rm -f "$JOB_DIR/secrets.env"; exit 0
  fi
  echo "starting" > "$status"
  "${DOCKER[@]}" rm -f "$CONTAINER" >/dev/null 2>&1 || true
  local env_args=()
  if [[ -f "$JOB_DIR/secrets.env" ]]; then env_args=(--env-file "$JOB_DIR/secrets.env"); fi
  mkdir -p "$HOME/.cache/huggingface"
  # Published on 127.0.0.1 only: the endpoint is never reachable from outside the instance.
  if ! "${DOCKER[@]}" run -d --name "$CONTAINER" --gpus all --ipc=host \
      -p "127.0.0.1:${PORT}:8000" \
      -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
      ${env_args[@]+"${env_args[@]}"} \
      "$VLLM_IMAGE" \
      "$MODEL_ID" \
      --host 0.0.0.0 --port 8000 \
      --served-model-name "$SERVED_MODEL_NAME" \
      --max-model-len "$MAX_MODEL_LEN" \
      --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" > /dev/null 2> "$JOB_DIR/run.err"; then
    echo "failed:start:$(tail -c 200 "$JOB_DIR/run.err" | tr '\n' ' ')" > "$status"
    rm -f "$JOB_DIR/secrets.env"; exit 0
  fi
  rm -f "$JOB_DIR/secrets.env"
  echo "started" > "$status"
}

cmd_health() {
  load_runtime_env
  local st running
  st="$(cat "$JOB_DIR/start.status" 2>/dev/null || echo missing)"
  case "$st" in
    pulling) die not_ready "pulling image $(resolved_image)" ;;
    starting) die not_ready "starting model container" ;;
    failed:*) die start_failed "${st#failed:}" ;;
    missing) die start_failed "model start was never launched" ;;
  esac
  detect_docker || die not_ready "docker not reachable"
  running="$("${DOCKER[@]}" inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null || echo missing)"
  if [[ "$running" != "true" ]]; then
    die container_exited "model container ($(resolved_image)) stopped (exit code $("${DOCKER[@]}" inspect -f '{{.State.ExitCode}}' "$CONTAINER" 2>/dev/null || echo none)) $(container_hint)"
  fi
  if curl -sf --max-time 5 "http://127.0.0.1:${PORT}/health" >/dev/null; then
    ok
  fi
  die not_ready "loading model weights"
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
  if [[ "$code" != "200" ]]; then
    # vLLM error bodies describe the request problem (schema, context length), not the document.
    local msg
    msg="$(grep -o '"message": *"[^"]\{0,200\}' "$JOB_DIR/response.json" 2>/dev/null | head -n1 | sed 's/.*"message": *"//' || true)"
    rm -f "$JOB_DIR/response.json"
    die http "HTTP $code ${msg}"
  fi
  ok
}

cmd_cleanup() {
  # Best effort and tolerant: the instance is destroyed right after this anyway.
  if [[ -f "$JOB_DIR/start.pid" ]]; then kill -- "-$(cat "$JOB_DIR/start.pid")" 2>/dev/null || kill "$(cat "$JOB_DIR/start.pid")" 2>/dev/null || true; fi
  if detect_docker; then "${DOCKER[@]}" rm -f "$CONTAINER" >/dev/null 2>&1 || true; fi
  rm -rf -- "$JOB_DIR"
  [[ ! -e "$JOB_DIR" ]] || die cleanup "job dir still present"
  ok
}

case "$CMD" in
  prepare) cmd_prepare ;;
  start-model) cmd_start_model ;;
  _start-bg) cmd_start_bg ;;
  health) cmd_health ;;
  infer) cmd_infer ;;
  cleanup) cmd_cleanup ;;
  *) die usage "unknown command" ;;
esac
