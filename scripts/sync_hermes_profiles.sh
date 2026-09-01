#!/usr/bin/env bash
#
# Sync department Hermes profiles from this repo into the local ~/.hermes/profiles/
# runtime, so `git pull` + this script is how everyone picks up teammates' changes
# to config.yaml, SOUL.md, and declared profile-owned skills. Never touches
# auth.json, .env, memories/, sessions/, state.db*, logs/, workspace/ — local.
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
PROFILE_REGISTRY="$REPO_ROOT/scripts/hermes_profile_registry.txt"
# Hermes Profile runtime root는 ~/.hermes/profiles다. 다른 루트를 써야 할 때만
# HERMES_HOME으로 명시적으로 override한다.
DEST_ROOT="${HERMES_HOME:-$HOME/.hermes}/profiles"
SKILL_BACKUP_ROOT="${HERMES_SKILL_BACKUP_ROOT:-${HOME}/.hermes/skill-backups}"

if [[ ! -f "$PROFILE_REGISTRY" ]]; then
  echo "Hermes Profile registry missing: $PROFILE_REGISTRY" >&2
  exit 1
fi

# 2026-08-02 (재일): 당시 docker-compose.yml 은 부서별로 ~/.hermes-<부서> 를
# 따로 마운트했다. 2026-08-10 부로 docker-compose.yml 은 통일된
# ~/.hermes/profiles/<부서> (AWS: /home/ubuntu/.hermes/profiles/<부서>) 로
# 정리됐고 DEST_ROOT 가 그 기본값이다. 아래 dest_for() 의 per_dept 분기는 그
# 구 경로가 남아있는 로컬 설치본을 위한 하위호환 fallback일 뿐, 새로 만들
# 필요는 없다.
dest_for() {
  local dept="$1"
  echo "$DEST_ROOT/$dept"
}

MODE="${1:-push}"   # push (repo -> ~/.hermes, default) | pull (~/.hermes -> repo)
PROFILE_FILTER="${2:-}"  # optional single profile; avoids unrelated runtime writes

profile_is_selected() {
  local profile="$1"
  [[ -z "$PROFILE_FILTER" || "$profile" == "$PROFILE_FILTER" ]]
}

sync_one() {
  local profile="$1" src_dir="$2" dest_dir="$3"

  if [[ ! -d "$src_dir" ]]; then
    echo "  skip: $src_dir not found"
    return
  fi
  if [[ ! -d "$dest_dir" ]]; then
    echo "  skip: $dest_dir not found (run: hermes profile create $profile)"
    return
  fi

  for f in config.yaml SOUL.md; do
    if [[ -f "$src_dir/$f" ]]; then
      cp "$src_dir/$f" "$dest_dir/$f"
      echo "  synced: $profile/$f"
    fi
  done
}

sync_repository_profiles() {
  local direction="$1"
  local profile folder kind container source_dir source_path target_path
  local matched=0

  while IFS='|' read -r profile folder kind container; do
    [[ -z "$profile" || "$profile" == \#* ]] && continue
    if ! profile_is_selected "$profile"; then
      continue
    fi
    matched=1
    case "$kind" in
      department) source_dir="$SRC_ROOT/$folder/hermes" ;;
      liaison) source_dir="$SRC_ROOT/$folder/hermes-liaison" ;;
      *)
        echo "invalid Hermes Profile registry kind '$kind' for '$profile'" >&2
        return 1
        ;;
    esac

    if [[ "$direction" == "push" ]]; then
      source_path="$source_dir"
      target_path="$(dest_for "$profile")"
    elif [[ "$direction" == "pull" ]]; then
      source_path="$(dest_for "$profile")"
      target_path="$source_dir"
    else
      echo "invalid sync direction '$direction'" >&2
      return 1
    fi
    sync_one "$profile" "$source_path" "$target_path"
  done < "$PROFILE_REGISTRY"

  if [[ -n "$PROFILE_FILTER" && "$matched" -ne 1 ]]; then
    echo "unknown Hermes profile: $PROFILE_FILTER" >&2
    return 1
  fi
}

# Only repository-owned, profile-specific skills are copied into a profile.
# Shared skills (including QA's feedback-review skill) stay on their existing
# shared roots. This keeps profile memory/config/SOUL isolation intact and
# prevents a runtime profile from becoming the canonical source.
sync_local_skill() {
  local dept="$1"
  local source_rel="$2"
  local dest_rel="$3"
  local source_dir="$REPO_ROOT/skills/$source_rel"
  local profile_dir="$(dest_for "$dept")"
  local dest_dir="$profile_dir/skills/$dest_rel"

  test -f "$source_dir/SKILL.md" || {
    echo "canonical skill source missing: skills/$source_rel" >&2
    exit 1
  }
  if [ ! -d "$profile_dir" ]; then
    echo " skip skill: $profile_dir not found (run: hermes profile create $dept)"
    return 0
  fi
  mkdir -p "$dest_dir"
  cp -R "$source_dir/." "$dest_dir/"
  echo " synced skill: $dept/$dest_rel"
}

retire_legacy_skill() {
  local dept="$1"
  local legacy_rel="$2"
  local legacy_dir="$(dest_for "$dept")/skills/$legacy_rel"
  if [ ! -e "$legacy_dir" ]; then
    return 0
  fi
  mkdir -p "$SKILL_BACKUP_ROOT"
  local backup_dir="$SKILL_BACKUP_ROOT/${dept}-$(basename "$legacy_dir")-$(date +%Y%m%d%H%M%S)"
  mv -- "$legacy_dir" "$backup_dir"
  echo " retired legacy skill: $dept/$legacy_rel -> $backup_dir"
}

retire_duplicate_skill_if_identical() {
  local dept="$1"
  local skill_rel="$2"
  local canonical_dir="$REPO_ROOT/skills/$skill_rel"
  local profile_dir="$(dest_for "$dept")/skills/$skill_rel"

  if [ ! -d "$profile_dir" ] || [ ! -d "$canonical_dir" ]; then
    return 0
  fi
  # Only retire a profile copy when the complete skill tree is byte-identical
  # to the shared canonical source.  A locally diverged skill remains visible
  # and is reported for explicit review instead of being overwritten/moved.
  if ! diff -qr -- "$canonical_dir" "$profile_dir" >/dev/null; then
    echo " preserve divergent duplicate: $dept/$skill_rel" >&2
    return 0
  fi
  retire_legacy_skill "$dept" "$skill_rel"
}

case "$MODE" in
  push)
    echo "Syncing repo -> ~/.hermes/profiles (config.yaml, SOUL.md, owned skills)"
    sync_repository_profiles push
    if profile_is_selected "ceo-agent"; then
      sync_local_skill "ceo-agent" "ceo/hermes-multi-agent-pipelines" "orchestration/hermes-multi-agent-pipelines"
      sync_local_skill "ceo-agent" "ceo/hermes-memory" "orchestration/hermes-memory"
    fi
    # autonomous-quant-research belongs to the direct strategy-hermes runtime,
    # not the Research HQ profile. Strategy Hermes receives it from the shared
    # /opt/shared-skills mount; do not copy it into research-department.
    if profile_is_selected "research-department"; then
      sync_local_skill "research-department" "methodology-scout" "research/methodology-scout"
    fi
    # QA feedback review is a single shared skill. Retire the two old,
    # profile-local trigger matches so Hermes cannot choose an obsolete
    # duplicate instead of /opt/shared-skills/qa-feedback-bottleneck-review.
    if profile_is_selected "qa-department"; then
      retire_legacy_skill "qa-department" "qa/metadata-only-qa-feedback-review"
      retire_legacy_skill "qa-department" "qa/skill-create-latency-control"
    fi
    # Shared /opt/shared-skills is the canonical copy for this byte-identical
    # research skill; retire only an identical profile duplicate so qualified
    # and categorized skill_view names do not become ambiguous.
    if profile_is_selected "research-department"; then
      retire_duplicate_skill_if_identical "research-department" "research/financial-equity-research"
    fi
    # The same byte-identical skill is mounted at /opt/shared-skills in the
    # runtime. Keeping a profile mirror makes Hermes skill_view report an
    # ambiguity and forces an avoidable provider re-plan. The shared copy is
    # canonical; retire only an identical profile duplicate into the existing
    # recoverable backup area.
    if profile_is_selected "quant-backtest-department"; then
      retire_duplicate_skill_if_identical "quant-backtest-department" "quant/equity-quant-assessment"
    fi
    if profile_is_selected "risk-management"; then
      retire_legacy_skill "risk-management" "autonomous-ai-agents/hermes-multi-agent-pipelines"
    fi
  ;;
  pull)
    echo "Syncing ~/.hermes/profiles -> repo (config.yaml, SOUL.md only)"
    sync_repository_profiles pull
    echo "Review with 'git diff' before committing."
    ;;
  *)
    echo "usage: $0 [push|pull] [profile]" >&2
    echo "  push  copy repo -> ~/.hermes/profiles (default, run after git pull)" >&2
    echo "  pull  copy ~/.hermes/profiles -> repo (run before git commit, after local edits)" >&2
    echo "  profile  optionally limit the operation to one registered profile" >&2
    exit 1
    ;;
esac

echo "Done."
