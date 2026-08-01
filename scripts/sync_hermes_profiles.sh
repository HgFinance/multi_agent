#!/usr/bin/env bash
#
# Sync department Hermes profiles from this repo into the local ~/.hermes/profiles/
# runtime, so `git pull` + this script is how everyone picks up teammates' changes
# to config.yaml / SOUL.md. Never touches auth.json, .env, memories/, sessions/,
# state.db*, logs/, workspace/ — those stay local to each machine.
#
# `push` does a full-file overwrite of config.yaml, so it drops any
# runtime-only bookkeeping Hermes has appended locally (_config_version,
# display:, plugins:, verify_on_stop). That's expected and harmless — those
# are default values Hermes re-adds on its own schedule, not state you'd lose
# (auth/session/memory are already excluded above). If you've hand-edited
# those fields locally, run `pull` first to capture them in the repo copy.
set -euo pipefail

REPO_ROOT="$(git rev-parse --show-toplevel)"
SRC_ROOT="$REPO_ROOT/departments"
# Hermes Profile 실제 위치는 OS마다 다르다. Windows 설치본은 ~/.hermes가 아니라
# AppData/Local/hermes를 쓴다 - 여기가 틀리면 8개 부서가 전부 조용히 skip된다.
DEST_ROOT="${HERMES_HOME:-$HOME/.hermes}/profiles"
[[ -d "$DEST_ROOT" ]] || DEST_ROOT="$HOME/AppData/Local/hermes/profiles"

# 2026-08-02 (재일): 부서별 Hermes Container 로 가면서 데이터 디렉터리도 부서별로
# 갈렸다(docker-compose.yml 의 ~/.hermes-<부서>). Credential·Memory Namespace 분리가
# 목적이므로 한 곳에 몰아넣으면 분리가 무의미해진다. 부서별 경로가 있으면 그쪽을
# 쓰고, 없으면 기존 공용 경로로 떨어진다 - 아직 안 옮긴 부서도 그대로 동작한다.
dest_for() {
  local dept="$1" per_dept="${HERMES_HOME_PREFIX:-$HOME/.hermes}-$1/profiles/$1"
  if [[ -d "$per_dept" ]]; then echo "$per_dept"; else echo "$DEST_ROOT/$dept"; fi
}

# dept -> departments/<n>/hermes 매핑. 순서는 CLAUDE.md 담당자 표와 무관하며
# REPOSITORY_DEPARTMENT_STRUCTURE.md 2절 조직 번호를 따른다.
DEPARTMENTS=(
  "ceo-agent:00-ceo-office"
  "hr-department:07-agent-workforce"
  "research-department:01-research"
  "trading-department:02-trading"
  "risk-management:03-risk"
  "quant-backtest-department:04-quant-backtest"
  "accounting-portfolio-department:05-accounting-portfolio"
  "qa-department:06-ai-qa-audit"
)

MODE="${1:-push}"   # push (repo -> ~/.hermes, default) | pull (~/.hermes -> repo)

sync_one() {
  local dept="$1" src_dir="$2" dest_dir="$3"

  if [[ ! -d "$src_dir" ]]; then
    echo "  skip: $src_dir not found"
    return
  fi
  if [[ ! -d "$dest_dir" ]]; then
    echo "  skip: $dest_dir not found (run: hermes profile create $dept)"
    return
  fi

  for f in config.yaml SOUL.md; do
    if [[ -f "$src_dir/$f" ]]; then
      cp "$src_dir/$f" "$dest_dir/$f"
      echo "  synced: $dept/$f"
    fi
  done
}

case "$MODE" in
  push)
    echo "Syncing repo -> ~/.hermes/profiles (config.yaml, SOUL.md only)"
    for entry in "${DEPARTMENTS[@]}"; do
      dept="${entry%%:*}"; folder="${entry##*:}"
      sync_one "$dept" "$SRC_ROOT/$folder/hermes" "$(dest_for "$dept")"
    done
    ;;
  pull)
    echo "Syncing ~/.hermes/profiles -> repo (config.yaml, SOUL.md only)"
    for entry in "${DEPARTMENTS[@]}"; do
      dept="${entry%%:*}"; folder="${entry##*:}"
      sync_one "$dept" "$(dest_for "$dept")" "$SRC_ROOT/$folder/hermes"
    done
    echo "Review with 'git diff' before committing."
    ;;
  *)
    echo "usage: $0 [push|pull]" >&2
    echo "  push  copy repo -> ~/.hermes/profiles (default, run after git pull)" >&2
    echo "  pull  copy ~/.hermes/profiles -> repo (run before git commit, after local edits)" >&2
    exit 1
    ;;
esac

echo "Done."
