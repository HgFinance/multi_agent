#!/usr/bin/env bash
# 관측 기반 추천 - "누가 사 모으고 있나" 4단 실행.
#
#   ./run_ownership.sh [후보수] [조회일수]
#
# 가격 랭킹(run_pipeline.sh)과 다른 산출물이다. 저쪽은 차트가 고르고,
# 여기는 **지분공시가 고른다** - 우리가 검증한 알파가 없으므로 예측 대신
# 관측을 판다.
set -euo pipefail

REPO="${REPO:-$HOME/hgfinance}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EV="$REPO/departments/01-research/evidence"
WORK="${WORK:-/tmp/ownership}"
TOP="${1:-4}"
DAYS="${2:-14}"

mkdir -p "$WORK"

echo "── 1단: 지분공시 시장 전체 스캔 (research-mcp) ──────────────"
echo "   DART list.json 1회 + 종목별 상세. 수백 초 걸린다."
docker cp "$EV/ownership_flow.py" hedgefund-research-mcp:/app/departments/01-research/evidence/ >/dev/null
docker cp "$HERE/scan_ownership.py" hedgefund-research-mcp:/tmp/ >/dev/null
docker exec -e SCAN_DAYS="$DAYS" -e SCAN_MAX_CORPS=60 \
  hedgefund-research-mcp python /tmp/scan_ownership.py
docker cp hedgefund-research-mcp:/tmp/ownership_scan.json "$WORK/scan.json" >/dev/null

echo
echo "── 2단: 수급·테마·밸류·가격계획 (ls-mcp) ────────────────────"
docker cp "$HERE/enrich_ownership.py" hedgefund-ls-mcp:/tmp/ >/dev/null
docker cp "$WORK/scan.json" hedgefund-ls-mcp:/tmp/ownership_scan.json >/dev/null
docker exec -e ENRICH_MAX="$TOP" hedgefund-ls-mcp python /tmp/enrich_ownership.py
docker cp hedgefund-ls-mcp:/tmp/ownership_cards.json "$WORK/cards.json" >/dev/null

echo
echo "── 3단: 뉴스·공시 호재/악재 판정 (research-mcp) ─────────────"
for f in judge_candidates.py narrative_axes.py instrument_scoring.py; do
  docker cp "$HERE/$f" hedgefund-research-mcp:/tmp/ >/dev/null
done
docker cp "$EV/price_levels.py" hedgefund-research-mcp:/tmp/ >/dev/null
docker cp "$EV/answer_builder.py" hedgefund-research-mcp:/app/departments/01-research/evidence/ >/dev/null
docker cp "$WORK/cards.json" hedgefund-research-mcp:/tmp/cards.json >/dev/null
docker exec -e CARDS_IN=/tmp/cards.json -e CARDS_OUT=/tmp/own_final.json \
  hedgefund-research-mcp python /tmp/judge_candidates.py >/dev/null
docker cp hedgefund-research-mcp:/tmp/own_final.json "$WORK/final.json" >/dev/null

echo
echo "── 4단: 근거 등급 답변 (호스트) ─────────────────────────────"
CARDS_FINAL="$WORK/final.json" ANSWERS_OUT="$WORK/answers.json" \
  AS_OF="$(date +%Y-%m-%d)" \
  "$REPO/.venv/bin/python" "$HERE/render_answers.py"

echo
echo "완료 -> $WORK/answers.json"
