#!/usr/bin/env bash
# Canonical, fail-closed entry point for the Qwen AWQ v1 serving container.
#
# This script deliberately never stops, removes, or prunes a container. If a
# manual vLLM/Qwen container is present, it reports the conflict and exits so a
# team member can identify the owner before changing runtime state.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
SERVICE="vllm"
CONTAINER="hedgefund-vllm"
PROJECT="hedgefund"
NETWORK="hedgefund_default"
PINNED_IMAGE="vllm/vllm-openai@sha256:0a51ea5b4ae2dc5d81890e5173f54203d2a3ae0cfffe51b8fd2afd4391bfd967"
COMPOSE=(docker compose -f "$ROOT_DIR/docker-compose.yml" -f "$ROOT_DIR/docker-compose.model.yml")

die() {
  echo "vLLM runtime guard: $*" >&2
  exit 1
}

container_exists() {
  test -n "$(docker ps -aq --filter "name=^/${CONTAINER}$")"
}

compose_config_check() {
  (cd "$ROOT_DIR" && VLLM_IMAGE="$PINNED_IMAGE" "${COMPOSE[@]}" config --quiet) \
    || die "Compose model overlay is invalid; fix config before touching vLLM"
}

check_duplicate_model_containers() {
  local rows
  rows="$(docker ps -a --format '{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}' \
    | awk 'BEGIN{IGNORECASE=1} ($2 ~ /vllm|qwen/ || $3 ~ /vllm|qwen/) {print}')"
  while IFS=$'\t' read -r cid name image status; do
    test -n "${cid:-}" || continue
    if test "$name" != "$CONTAINER"; then
      die "duplicate/manual model container detected: $name ($image, $status). Do not stop it automatically; resolve ownership first."
    fi
  done <<< "$rows"
}

check_owner_and_shape() {
  container_exists || die "$CONTAINER does not exist; run '$0 up'"

  local project service owner profile launcher image state health networks ports
  project="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$CONTAINER")"
  service="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$CONTAINER")"
  owner="$(docker inspect -f '{{index .Config.Labels "com.hgfinance.runtime.owner"}}' "$CONTAINER")"
  profile="$(docker inspect -f '{{index .Config.Labels "com.hgfinance.runtime.profile"}}' "$CONTAINER")"
  launcher="$(docker inspect -f '{{index .Config.Labels "com.hgfinance.runtime.launcher"}}' "$CONTAINER")"
  image="$(docker inspect -f '{{.Config.Image}}' "$CONTAINER")"
  state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER")"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$CONTAINER")"
  networks="$(docker inspect -f '{{range $name, $net := .NetworkSettings.Networks}}{{$name}}={{join $net.Aliases ","}} {{end}}' "$CONTAINER")"
  ports="$(docker inspect -f '{{json .HostConfig.PortBindings}}' "$CONTAINER")"

  test "$project" = "$PROJECT" || die "$CONTAINER is not owned by Compose project $PROJECT (owner=$project)"
  test "$service" = "$SERVICE" || die "$CONTAINER has unexpected Compose service label: $service"
  test "$owner" = "compose" || die "$CONTAINER is not marked as Compose-owned (owner=$owner)"
  test "$profile" = "qwen-awq-v1" || die "$CONTAINER has unexpected runtime profile: $profile"
  test "$launcher" = "scripts/model_plane/vllm_runtime.sh" || die "$CONTAINER was not launched by the canonical runtime contract"
  test "$image" = "$PINNED_IMAGE" || die "$CONTAINER image drifted from pinned digest: $image"
  [[ " $networks " == *"$NETWORK="* ]] || die "$CONTAINER is not attached to $NETWORK"
  [[ " $networks " == *"vllm"* ]] || die "$CONTAINER has no vllm network alias"
  [[ "$ports" == *'127.0.0.1'* ]] || die "vLLM host port is not loopback-only: $ports"

  echo "container=$CONTAINER state=$state health=$health image=$image"
  echo "compose_project=$project service=$service profile=$profile network=$NETWORK alias=vllm"
  test "$state" = "running" || die "$CONTAINER is not running (state=$state)"
  test "$health" = "healthy" || die "$CONTAINER is not healthy yet (health=$health); wait for model load and retry"
}

check_ready_endpoint() {
  local models
  curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8000/health >/dev/null \
    || die "vLLM /health is not ready"
  models="$(curl --fail --silent --show-error --max-time 10 http://127.0.0.1:8000/v1/models)" \
    || die "vLLM /v1/models is not ready"
  grep -q 'qwen2.5-14b-instruct-awq' <<< "$models" \
    || die "lowercase Qwen v1 served model alias is missing"
  grep -q 'hgfinance-awq-arithmetic-2epoch' <<< "$models" \
    || die "approved arithmetic adapter is missing from vLLM model registry"
  echo "models=Qwen2.5-14B-Instruct-AWQ base + qwen2.5-14b-instruct-awq alias + hgfinance-awq-arithmetic-2epoch"
}

check() {
  compose_config_check
  check_duplicate_model_containers
  check_owner_and_shape
  check_ready_endpoint
  echo "vLLM runtime contract: PASS"
}

up() {
  compose_config_check
  check_duplicate_model_containers
  if container_exists; then
    local project service owner
    project="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$CONTAINER")"
    service="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$CONTAINER")"
    owner="$(docker inspect -f '{{index .Config.Labels "com.hgfinance.runtime.owner"}}' "$CONTAINER")"
    test "$project" = "$PROJECT" && test "$service" = "$SERVICE" \
      || die "$CONTAINER exists but is not the expected Compose vLLM service; refusing to stop or remove it"
    if test "$owner" != "compose"; then
      echo "Reconciling the existing Compose vLLM container with the hardened ownership labels."
    fi
  fi
  echo "Starting only through the pinned Compose model overlay. Existing data and volumes are untouched."
  (cd "$ROOT_DIR" && VLLM_IMAGE="$PINNED_IMAGE" "${COMPOSE[@]}" up -d "$SERVICE")
  echo "vLLM is starting. Run '$0 check' after the 14B model and CUDA graphs finish loading."
}

logs() {
  docker logs -f "$CONTAINER"
}

usage() {
  cat <<'EOF'
Usage: scripts/model_plane/vllm_runtime.sh <check|up|logs>

  check  Verify Compose ownership, pinned image, network, loopback port, health, and model aliases.
  up     Start/reconcile only the Compose-owned Qwen AWQ v1 service; never removes a conflict.
  logs   Follow the canonical container logs.
EOF
}

case "${1:-}" in
  check) check ;;
  up) up ;;
  logs) logs ;;
  -h|--help) usage ;;
  *) usage >&2; exit 2 ;;
esac
