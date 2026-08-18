#!/usr/bin/env bash
# 로컬(비-Docker) 개발 스택 기동 — BFF + governance-api + accounting-api.
#
# ## 왜 이 스크립트가 있나
#
# 2026-08-14, `/ui/mandates` 계열이 전부 404였다. 원인은 코드도 .env도 아니고
# **이틀 전에 뜬 uvicorn 프로세스가 8001을 점유한 채 옛 라우트를 서빙**하고
# 있었던 것이다(--reload 없음). "팀원은 되는데 나만 안 된다"의 정체가 이거였다.
#
# 그래서 이 스크립트는 **띄우기 전에 항상 기존 프로세스를 정리한다.** 손으로
# uvicorn을 세 번 띄우면 그중 하나가 낡은 채로 남는 사고가 반복된다.
#
# ## 왜 환경변수를 여기서 넣나
#
# `docker-compose.yml` / `departments/*/compose.yaml`이 정본이지만, Docker 없이
# 호스트에서 띄우면 그 environment 블록이 적용되지 않는다. 아래 값은 compose와
# **같은 값**이다 — 여기서 새로 정하는 것이 없다:
#
#   GOVERNANCE_API_URL  compose: http://governance-api:8000   → 호스트: 127.0.0.1:8043
#   PORTFOLIO_API_URL   compose: http://accounting-api:8000   → 호스트: 127.0.0.1:8046
#   ACCOUNTING_MODE     compose: ${ACCOUNTING_MODE:-PAPER_DB} → 그대로 PAPER_DB
#
# 이 셋을 `.env`에 넣지 않는 이유: `.env`는 compose도 읽는다. 거기에 127.0.0.1을
# 박으면 컨테이너 안에서 자기 자신을 가리켜 조용히 깨진다.
#
# ## 알아둘 것
#
# - `ACCOUNTING_MODE=PAPER_DB`가 없으면 accounting-api가 인메모리로 뜬다.
#   그러면 `/ui/investor-profiles`가 503이고 적합성 프로필이 저장되지 않는다.
#   `departments/05-accounting-portfolio/api/app.py`는 `load_dotenv`를 부르지
#   않으므로 `DATABASE_URL`도 반드시 프로세스 환경으로 넘겨야 한다.
# - 저장 대상은 `LOCAL_CONTROL_DATABASE_URL`이다. 값이 없으면 로컬 Supabase
#   PostgreSQL(`127.0.0.1:54322`)을 사용하며, hosted Supabase 주소는 거부한다.
#
# 사용: ./scripts/run_local_stack.sh [start|stop|status]

set -euo pipefail
cd "$(dirname "$0")/.."

PY=".venv/Scripts/python.exe"
[ -x "$PY" ] || PY=".venv/bin/python"
[ -x "$PY" ] || { echo "저장소 .venv를 찾지 못했습니다. CLAUDE.md의 uv venv 절차를 먼저 실행하세요."; exit 1; }

BFF_PORT=${PORTFOLIO_BFF_PORT:-8001}
GOV_PORT=8043    # compose: 127.0.0.1:8043 -> governance-api:8000
ACC_PORT=8046    # compose: 127.0.0.1:8046 -> accounting-api:8000
LOG_DIR="${TMPDIR:-/tmp}/hgfinance-stack"

stop_port() {
  # Windows/POSIX 양쪽에서 도는 방법이 달라 둘 다 시도한다. pkill은 Windows
  # 네이티브 프로세스를 못 잡는 경우가 있어 PowerShell 경로를 먼저 쓴다.
  if command -v powershell.exe >/dev/null 2>&1; then
    powershell.exe -NoProfile -Command "
      \$c = Get-NetTCPConnection -LocalPort $1 -State Listen -ErrorAction SilentlyContinue
      foreach (\$conn in \$c) {
        \$p = Get-CimInstance Win32_Process -Filter \"ProcessId=\$(\$conn.OwningProcess)\" -ErrorAction SilentlyContinue
        if (\$p -and \$p.CommandLine -like '*uvicorn*') { Stop-Process -Id \$p.ProcessId -Force -ErrorAction SilentlyContinue }
      }" >/dev/null 2>&1 || true
  fi
  pkill -f "uvicorn.*--port $1" >/dev/null 2>&1 || true
}

case "${1:-start}" in
  stop)
    for p in "$BFF_PORT" "$GOV_PORT" "$ACC_PORT"; do stop_port "$p"; done
    echo "중지했습니다: $BFF_PORT $GOV_PORT $ACC_PORT"
    exit 0
    ;;
  status)
    for p in "$GOV_PORT" "$ACC_PORT" "$BFF_PORT"; do
      printf '%-6s ' "$p"
      curl -s -m 5 "http://127.0.0.1:$p/health" || echo "(응답 없음)"
      echo
    done
    exit 0
    ;;
esac

# `.env`에서는 로컬 control DB 계약만 꺼낸다. 값은 출력하지 않는다.
# 일반 DATABASE_URL은 hosted DB를 가리킬 수 있으므로 의도적으로 무시한다.
LOCAL_CONTROL_DATABASE_URL="${LOCAL_CONTROL_DATABASE_URL:-$("$PY" -c "
import io
try:
    for l in io.open('.env',encoding='utf-8',errors='replace'):
        l=l.strip()
        if l.startswith('LOCAL_CONTROL_DATABASE_URL='):
            print(l.split('=',1)[1].strip().strip('\"').strip(\"'\")); break
except FileNotFoundError: pass
")}"
LOCAL_CONTROL_DATABASE_URL="${LOCAL_CONTROL_DATABASE_URL:-postgresql://postgres:postgres@127.0.0.1:54322/postgres}"
if ! LOCAL_CONTROL_DATABASE_URL="$LOCAL_CONTROL_DATABASE_URL" "$PY" -c '
import os, sys
from urllib.parse import urlsplit
host = (urlsplit(os.environ["LOCAL_CONTROL_DATABASE_URL"]).hostname or "").lower()
if not host or host.endswith(".supabase.co") or host.endswith(".supabase.com"):
    sys.exit(1)
'; then
  echo "오류: LOCAL_CONTROL_DATABASE_URL은 hosted Supabase가 아닌 로컬/private control DB여야 합니다." >&2
  exit 1
fi
export LOCAL_CONTROL_DATABASE_URL
export CONTROL_DATABASE_URL="$LOCAL_CONTROL_DATABASE_URL"
export DATABASE_URL="$LOCAL_CONTROL_DATABASE_URL"
export ACCOUNTING_MODE="${ACCOUNTING_MODE:-PAPER_DB}"
export GOVERNANCE_API_URL="http://127.0.0.1:$GOV_PORT"
export PORTFOLIO_API_URL="http://127.0.0.1:$ACC_PORT"
export APP_ENV="local"
export PORTFOLIO_AUTH_MODE="fixture"
export PORTFOLIO_AUTH_REQUIRED="false"

mkdir -p "$LOG_DIR"
for p in "$BFF_PORT" "$GOV_PORT" "$ACC_PORT"; do stop_port "$p"; done
sleep 2

"$PY" -m uvicorn app:app --app-dir departments/00-ceo-office/api \
  --host 127.0.0.1 --port "$GOV_PORT" > "$LOG_DIR/governance.log" 2>&1 &
"$PY" -m uvicorn app:app --app-dir departments/05-accounting-portfolio/api \
  --host 127.0.0.1 --port "$ACC_PORT" > "$LOG_DIR/accounting.log" 2>&1 &
sleep 6
"$PY" -m uvicorn apps.api.main:app \
  --host 127.0.0.1 --port "$BFF_PORT" > "$LOG_DIR/bff.log" 2>&1 &

echo "기동 중… 로그: $LOG_DIR"
sleep 22

fail=0
# governance는 canonical_db_configured, accounting은 store 문자열로 durable 여부가 드러난다.
# 인메모리로 떴는데 "떴다"고만 말하면 저장이 안 되는 걸 나중에 알게 된다.
for check in "$GOV_PORT:canonical_db_configured\":true" "$ACC_PORT:supabase accounting" ; do
  port="${check%%:*}"; want="${check#*:}"
  body="$(curl -s -m 10 "http://127.0.0.1:$port/health" || true)"
  case "$body" in
    *"$want"*) echo "ok   :$port  $body" ;;
    *) echo "경고 :$port  DB에 붙지 않았습니다 -> $body"; fail=1 ;;
  esac
done

routes="$(curl -s -m 10 "http://127.0.0.1:$BFF_PORT/openapi.json" \
  | "$PY" -c "import sys,json;p=json.load(sys.stdin)['paths'];print(len([k for k in p if 'mandate' in k or 'investor' in k]))" 2>/dev/null || echo 0)"
echo "ok   :$BFF_PORT  mandate/investor 라우트 $routes개"
# 12개 미만이면 옛 코드가 도는 것이다 — 2026-08-14 사고가 정확히 이 모양이었다.
[ "$routes" -ge 12 ] || { echo "경고: 라우트가 부족합니다. 옛 프로세스가 남아 있는지 확인하세요."; fail=1; }

exit "$fail"
