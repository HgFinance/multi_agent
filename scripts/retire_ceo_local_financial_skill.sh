#!/usr/bin/env bash
# Move the former CEO-local copy outside every Hermes skills root.
# The operation is recoverable and deliberately targets one exact skill path.
set -euo pipefail

repo_root="$(git rev-parse --show-toplevel)"
profile_root="${HERMES_PROFILE_ROOT:-${HOME}/.hermes/profiles}"
source_skill="${repo_root}/skills/finance/financial-portfolio-assessment/SKILL.md"
legacy="${profile_root}/ceo-agent/skills/finance/financial-portfolio-assessment"
backup_root="${HERMES_SKILL_BACKUP_ROOT:-${HOME}/.hermes/skill-backups}"

test -f "$source_skill"
if [ ! -e "$legacy" ]; then
  echo "legacy CEO-local skill is already absent: $legacy"
  exit 0
fi

mkdir -p "$backup_root"
backup="$backup_root/financial-portfolio-assessment-$(date +%Y%m%d%H%M%S)"
if [ -f "$legacy/SKILL.md" ] && ! cmp -s "$legacy/SKILL.md" "$source_skill"; then
  echo "legacy CEO-local content differs from repository canonical source; preserving it in backup for review" >&2
fi
mv -- "$legacy" "$backup"
test ! -e "$legacy"
echo "moved legacy CEO-local skill to: $backup"
