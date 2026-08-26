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

  # ▶ **활성 프로필은 /opt/data 루트다** (2026-08-11 실측)
  #   컨테이너는 처음 뜰 때 /opt/data/config.yaml 에 **Hermes 기본 예제**(88KB)를
  #   깔아 둔다. `profiles/<이름>/` 하위에 심으면 목록에는 뜨지만 활성 설정이 아니다.
  #   부서 설정을 실제로 쓰게 하려면 루트를 갈아끼워야 한다.
  # ▶ **${VAR} 를 .env 값으로 치환해서 넣는다** (2026-08-11 실측)
  #   저장소 config 의 MCP 토큰은 `Bearer ${MCP_RESEARCH_API_KEY}` 라는 참조다.
  #   그대로 심으면 헤르메스가 그 문자열을 그대로 보내 **HTTP 401** 이 난다.
  #   실제로 서브에이전트가 여기서 막혔고, 지어내지 않고 blocked 로 멈춘 것은
  #   옳은 동작이었다 - 고쳐야 할 것은 토큰 주입 쪽이다.
  #   재설치 때마다 손으로 넣으면 언젠가 또 잊으므로 절차에 넣는다.
  local tmp; tmp="$(mktemp)"
  if [ -f "$ROOT/.env" ]; then
    # Git Bash resolves repository paths as /c/..., while the selected
    # ``python`` executable is normally native Windows Python. Convert only
    # the Python argv paths in that environment; Docker/GNU tools below still
    # need the POSIX spellings.
    local python_cfg="$src/config.yaml" python_env="$ROOT/.env" python_tmp="$tmp"
    if command -v cygpath >/dev/null 2>&1; then
      python_cfg="$(cygpath -w "$python_cfg")"
      python_env="$(cygpath -w "$python_env")"
      python_tmp="$(cygpath -w "$python_tmp")"
    fi
    local python_bin
    python_bin="$(command -v python3 || command -v python || true)"
    [ -n "$python_bin" ] || { echo "  ✗ Python interpreter not found"; rm -f "$tmp"; return 1; }
    PYTHONIOENCODING=utf-8 "$python_bin" - "$python_cfg" "$python_env" "$python_tmp" <<'PYEOF'
import os, re, sys
cfg, envf = sys.argv[1], sys.argv[2]
env = {}
for line in open(envf, encoding="utf-8", errors="replace"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k] = v.strip()
text = open(cfg, encoding="utf-8").read()
# ${VAR} 와 ${VAR:-기본} 둘 다. 값이 없으면 **원문을 남긴다** - 빈 문자열로
# 바꾸면 "인증 없음" 이 "인증 성공" 처럼 조용히 통과한다.
def sub(m):
    key = m.group(1)
    return env.get(key, m.group(0))
out = re.sub(r"\$\{([A-Z_][A-Z0-9_]*)(?::-[^}]*)?\}", sub, text)
# ▶ **stdout 으로 흘리면 호스트 인코딩(cp949)을 타서 UTF-8 이 깨진다.**
#   실제로 config.yaml 이 깨져 헤르메스가 기본 설정으로 폴백했고, 그 결과가
#   401·"Model parameter is required" 로 나타나 원인을 찾는 데 시간이 걸렸다.
#   파일로 직접 쓴다.
open(sys.argv[3], "w", encoding="utf-8", newline="").write(out)
PYEOF
  else
    cp "$src/config.yaml" "$tmp"
  fi
  docker cp "$tmp" "$container:/opt/data/config.yaml"

  # ▶ **루트 교체만으로는 kanban spawn 이 안 된다** (2026-08-11 실측)
  #   활성 설정은 /opt/data 루트지만, `kanban dispatch` 는 카드의 assignee 를
  #   **프로필 이름**으로 찾는다. profiles/<이름>/ 이 없으면
  #   "non-spawnable assignee — terminal lane" 으로 조용히 건너뛴다.
  #   둘 다 있어야 한다: 루트는 이 컨테이너가 무엇인지, profiles/ 는 남이 이
  #   부서를 지목할 수 있는 이름표다.
  MSYS_NO_PATHCONV=1 docker exec "$container" sh -c "mkdir -p /opt/data/profiles/$profile"
  docker cp "$tmp" "$container:/opt/data/profiles/$profile/config.yaml"
  [ -f "$src/SOUL.md" ] && docker cp "$src/SOUL.md" "$container:/opt/data/profiles/$profile/SOUL.md"
  MSYS_NO_PATHCONV=1 docker exec "$container" sh -c "chown -R hermes:hermes /opt/data/profiles; chmod 700 /opt/data/profiles/$profile"
  rm -f "$tmp"
  [ -f "$src/SOUL.md" ] && docker cp "$src/SOUL.md" "$container:/opt/data/SOUL.md"

  # ▶ docker cp 는 root 소유로 넣는다 - hermes 사용자가 못 읽으면 첫 실행에 죽는다
  #   (`Permission denied: /opt/data/.../cron`). 이 줄이 함정이었다.
  MSYS_NO_PATHCONV=1 docker exec "$container" sh -c "chown hermes:hermes /opt/data/config.yaml /opt/data/SOUL.md 2>/dev/null; true"

  local got
  got=$(MSYS_NO_PATHCONV=1 docker exec "$container" head -2 /opt/data/config.yaml 2>&1 | tr -d '
')
  if echo "$got" | grep -q "Hermes Agent CLI Configuration"; then
    echo "  ✗ $profile: 아직 Hermes 기본 설정이다 - 복사가 안 먹었다"
    return 1
  fi
  echo "  ✓ $profile (활성 설정 교체됨)"
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
