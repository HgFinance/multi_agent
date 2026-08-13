#!/bin/sh
# 내가 지금 무엇을 쓸 수 있는가 - 카드 시작할 때 한 번 돌린다.
#
# ▶ 왜 (2026-08-12 실측)
#   에이전트가 "도구가 없다 / 저장소가 없다 / 볼 수단이 없다" 로 카드를 닫은 일이
#   하루에 세 번 났다. 셋 다 있었다:
#     `source_registry.py ABSENT`  → /app/repo 에 54,905바이트로 있었다
#     `시세 조회 도구 없음`         → market-api:8036 이 인증 없이 열려 있었다
#     `ModuleNotFoundError`        → 설치돼 있는데 PATH 밖이었다
#   한 번 훑으면 끝날 것을 155초씩 쓰고 오진했다. **먼저 훑어라.**
#
# 사용:  sh capabilities.sh
set -u

echo "── 실행면 (파이썬·도구) ──"
for t in quant-py reach-py python3 agent-reach yt-dlp gh mcporter; do
  printf "  %-12s " "$t"
  command -v "$t" >/dev/null 2>&1 && echo "있음  ($(command -v "$t"))" || echo "없음"
done

echo "── 경로 ──"
echo "  QUANT_REPO       ${QUANT_REPO:-(미설정)}   ← 저장소 전체(읽기 전용)"
echo "  QUANT_WORKSPACE  ${QUANT_WORKSPACE:-(미설정)}   ← 고쳐도 되는 코드"
for d in "${QUANT_REPO:-}" "${QUANT_WORKSPACE:-}"; do
  [ -n "$d" ] && [ -d "$d" ] && echo "    $d → $(ls "$d" 2>/dev/null | wc -l)개 항목"
done
echo "  마운트:"
mount 2>/dev/null | grep -E " on (/app|/opt/data)" | sed 's/^/    /' | head -8

echo "── 원장 (DSN 은 프로필 env 로 온다) ──"
for v in DATABASE_URL TIMESCALE_DATABASE_URL QUANT_DATABASE_URL; do
  printf "  %-24s " "$v"
  eval "val=\${$v:-}"
  [ -n "$val" ] && echo "있음" || echo "없음 - 이 세션에는 안 왔다"
done

echo "── HTTP 창구 (인증 불필요) ──"
for u in market-api:8036 research-api:8035 quant-api:8037; do
  printf "  %-22s " "$u"
  code=$(timeout 8 curl -s -o /dev/null -w "%{http_code}" "http://$u/health" 2>/dev/null)
  [ "$code" = "200" ] && echo "200  살아 있음" || echo "${code:-불가}"
done

echo "── 스킬 ──"
find /opt/data -maxdepth 6 -name SKILL.md -path "*skills*" 2>/dev/null \
  | sed 's#.*/skills/##; s#/SKILL.md##' | sort -u | sed 's/^/  /' | head -12

echo ""
echo "▶ 위에서 '있음' 인 것은 **쓸 수 있다.** 없다고 적기 전에 이 목록을 보라."
echo "  DSN 이 없어도 시세는 HTTP 로 읽는다. MCP 가 없어도 도구가 없는 게 아니다."
