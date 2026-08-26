#!/usr/bin/env bash
# Reclaim EBS disk space from stopped containers, unused networks, dangling
# images, and build cache. Never touches volumes since those hold
# TimescaleDB/Postgres data.
#
# Long-lived containers that are started by a separate scheduler must survive
# while stopped. Protect them either by name below or with the label
# `hgfinance.prune.protected=true`.
#
# Manual maintenance only. Do not schedule this script: this host also runs
# long-lived containers outside Compose, and an automatic cleanup cannot infer
# whether every stopped container is disposable.
set -euo pipefail

if [[ "${1:-}" != "--confirm" ]]; then
  printf 'Refusing automatic cleanup. Run manually with --confirm after reviewing Docker state.\n' >&2
  exit 2
fi

date

readonly -a PROTECTED_CONTAINER_NAMES=(
  "strategy-dispersed-long"
)

is_protected_name() {
  local candidate="$1"
  local protected_name

  for protected_name in "${PROTECTED_CONTAINER_NAMES[@]}"; do
    if [[ "$candidate" == "$protected_name" ]]; then
      return 0
    fi
  done

  return 1
}

mapfile -t stopped_container_ids < <(
  docker container ls --all --quiet \
    --filter status=created \
    --filter status=exited \
    --filter status=dead
)

for container_id in "${stopped_container_ids[@]}"; do
  container_name="$(docker inspect --format '{{.Name}}' "$container_id")"
  container_name="${container_name#/}"
  protected_label="$(
    docker inspect \
      --format '{{with index .Config.Labels "hgfinance.prune.protected"}}{{.}}{{end}}' \
      "$container_id"
  )"

  if is_protected_name "$container_name" || [[ "$protected_label" == "true" ]]; then
    printf 'Protected stopped container retained: %s (%s)\n' \
      "$container_name" "$container_id"
    continue
  fi

  docker container rm "$container_id"
done

docker network prune -f
docker image prune -f
docker builder prune -f
