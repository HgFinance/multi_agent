#!/usr/bin/env bash
# 종목 추천 3단 실행. 호스트에서 돌린다 - 각 단계를 자격이 있는 컨테이너로 보낸다.
#
#   ./run_pipeline.sh [후보수]
#
# 단계 사이는 JSON 파일로 넘긴다. 중간에 끊겨도 그 파일부터 다시 시작할 수 있다.
set -euo pipefail

REPO="${REPO:-$HOME/hgfinance}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK="${WORK:-/tmp/recommendation}"
ENRICH_MAX="${1:-6}"

mkdir -p "$WORK"

# DSN 은 .env 에서 읽어 환경변수로만 넘긴다 - 로그에 찍지 않는다.
cd "$REPO"
TSDB_IP="$(docker inspect hedgefund-timescaledb \
  --format '{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}' | awk '{print $1}')"
TSDB_PW="$(grep -m1 '^HEDGEFUND_TSDB_PASSWORD=' .env | cut -d= -f2- | tr -d '\r"')"
export TIMESCALE_DATABASE_URL="postgresql://postgres:${TSDB_PW}@${TSDB_IP}:5432/market"
export CONTROL_DATABASE_URL="postgresql://postgres:${TSDB_PW}@${TSDB_IP}:5432/control"

echo "── 1층: 유니버스 채점 (호스트) ────────────────────────────────"
SCREEN_OUT="$WORK/candidates.json" "$REPO/.venv/bin/python" "$HERE/screen_universe.py"

echo
echo "── 1.5층: 수급·공매도·밸류 (ls-mcp) ──────────────────────────"
for f in instrument_scoring.py enrich_candidates.py; do
  docker cp "$HERE/$f" hedgefund-ls-mcp:/tmp/ >/dev/null
done
docker cp "$WORK/candidates.json" hedgefund-ls-mcp:/tmp/candidates.json >/dev/null
docker exec -e SCREEN_OUT=/tmp/candidates.json -e ENRICH_MAX="$ENRICH_MAX" \
  hedgefund-ls-mcp python /tmp/enrich_candidates.py
docker cp hedgefund-ls-mcp:/tmp/cards.json "$WORK/cards.json" >/dev/null

echo
echo "── 2층: 뉴스·공시 판정 (research-mcp) ────────────────────────"
echo "   DART 기업색인 워밍업에 ~220초가 걸린다(프로세스 캐시라 매 실행 발생)."
for f in instrument_scoring.py narrative_axes.py judge_candidates.py; do
  docker cp "$HERE/$f" hedgefund-research-mcp:/tmp/ >/dev/null
done
docker cp "$WORK/cards.json" hedgefund-research-mcp:/tmp/cards.json >/dev/null
docker exec -e CARDS_IN=/tmp/cards.json -e CARDS_OUT=/tmp/cards_final.json \
  hedgefund-research-mcp python /tmp/judge_candidates.py
docker cp hedgefund-research-mcp:/tmp/cards_final.json "$WORK/cards_final.json" >/dev/null

echo
echo "완료 -> $WORK/cards_final.json"
