#!/usr/bin/env bash
# 저장소의 부서 헤르메스 프로필을 컨테이너 런타임에 심는다.
#
# ▶ 왜 스크립트인가 (2026-08-11 실측)
#   `docker cp` 로 손으로 심으면 **root 소유로 만들어져 에이전트가 죽는다** -
#   컨테이너 안에서 hermes 는 `hermes` 사용자로 도는데 /opt/data/profiles 가
#   root:root 면 `Permission denied: /opt/data/profiles/<이름>/cron` 으로 실패한다.
#   CEO 를 심을 때 여기서 걸렸다. 실패가 "권한" 이 아니라 "cron" 으로 보여서
#   원인을 찾는 데 시간이 걸렸다 - 그래서 절차에 못 박는다.
#
# ▶ 하지 않는 것
#   인증(`hermes auth add`)은 대화형이라 여기서 하지 않는다. 프로필만 심는다.
#   기존 /opt/data 를 지우지 않는다 - 인증 자격이 거기 있다.
#
# 사용
#   scripts/install_hermes_profile.sh <컨테이너> <부서디렉터리> <프로필명>
#   scripts/install_hermes_profile.sh --all
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 컨테이너 : 부서 디렉터리 : 프로필 이름
# 프로필 이름은 docker-compose.yml 의 마운트 경로가 정본이다
# (orchestration/workflows/*.yaml 은 더 오래된 판이라 이름이 다를 수 있다).
DEPARTMENTS=(
  "hedgefund-ceo-hermes:00-ceo-office:ceo-agent"
  "hedgefund-research-hermes:01-research:research-department"
  "hedgefund-trading-hermes:02-trading:trading-department"
  "hedgefund-risk-hermes:03-risk:risk-management"
  "hedgefund-quant-hermes:04-quant-backtest:quant-backtest-department"
  "hedgefund-accounting-hermes:05-accounting-portfolio:accounting-portfolio-department"
  "hedgefund-qa-hermes:06-ai-qa-audit:qa-department"
  "hedgefund-workforce-hermes:07-agent-workforce:workforce-management"
)

install_one() {
  local container="$1" dept="$2" profile="$3"
  local src="$ROOT/departments/$dept/hermes"

  [ -f "$src/config.yaml" ] || { echo "  ✗ $dept: config.yaml 없음"; return 1; }
  docker inspect "$container" >/dev/null 2>&1 || { echo "  - $container: 안 떠 있음(건너뜀)"; return 0; }

  local dest="/opt/data/profiles/$profile"
  docker exec "$container" mkdir -p "$dest"
  docker cp "$src/config.yaml" "$container:$dest/config.yaml"
  [ -f "$src/SOUL.md" ] && docker cp "$src/SOUL.md" "$container:$dest/SOUL.md"

  # ▶ **이 두 줄이 함정이다.** docker exec 는 root 로 만든다 - 안 고치면 에이전트가
  #   자기 프로필 디렉터리에 못 써서 첫 실행에 죽는다.
  docker exec "$container" chown -R hermes:hermes /opt/data/profiles
  docker exec "$container" chmod 700 "$dest"

  if docker exec "$container" hermes profile list 2>&1 | grep -q "$profile"; then
    echo "  ✓ $profile"
  else
    echo "  ✗ $profile: 심었으나 인식 안 됨 - 프로필 이름이 compose 와 다른지 확인"
    return 1
  fi
}

if [ "${1:-}" = "--all" ]; then
  echo "부서 프로필 설치 (${#DEPARTMENTS[@]}개)"
  fail=0
  for entry in "${DEPARTMENTS[@]}"; do
    IFS=: read -r c d p <<< "$entry"
    install_one "$c" "$d" "$p" || fail=$((fail + 1))
  done
  echo "완료 - 실패 $fail"
  echo "인증은 따로 한다: docker exec -it <컨테이너> hermes auth add openai-codex"
  exit $((fail > 0))
fi

[ $# -eq 3 ] || { sed -n '1,20p' "$0"; exit 2; }
install_one "$1" "$2" "$3"
