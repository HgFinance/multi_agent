#!/usr/bin/env python3
"""연구 벤치 - **에이전트가 측정을 스스로 설계하는 무인 루프.**

담당: 재일 (리서치본부 RES + 퀀트·백테스트본부 QNT)

▶ 왜 만들었나 (2026-08-25 진단)
  공장은 **반대쪽 절반을 자동화했다.** 브리더가 시간당 수식 64개를 찍어내는
  동안 측정은 상수로 얼려 뒀다(평가기 v12, 퍼널 6/20/60, 비용 23bp, 판정 11조항).
  그런데 이 프로젝트가 알아낸 것은 전부 **측정 설계**에서 나왔다 -
  `ml_pipeline/audit_*.py` 85개가 그 증거다. 이름을 보면 안다:
  anatomy·leakage·abstention·morphology·context_axes. 한 개도 "새 수식" 이 아니다.

  그래서 이 루프는 산출물 계약을 바꾼다:
      수식(AST)  ->  **실험 스크립트 + 숫자 + 해석 + 다음 질문**

▶ 어떻게 스스로 도나
  다음 질문의 출처는 셋이고, 이 순서로 고른다:
    1. 사람이 던진 아이디어 큐 (`ideas.jsonl`)   <- 개입
    2. **직전 발견이 낳은 다음 질문**              <- 자기개선의 엔진
    3. 씨앗 질문 목록                              <- 큐가 마르면
  2번이 핵심이다. 발견이 다음 실험을 설계하므로 사람이 없어도 루프가 돈다.

▶ 탐색과 확증을 가른다
  여기엔 사전등록도 시도 예산도 AST 문법도 없다. 탐색은 원래 p-해킹적이어야
  하고 그래도 된다 - **승격만 안 하면** 된다. 홀드아웃(최근 12세션)은 탐색이
  절대 안 만지고, 승격 후보는 기존 공장의 확증 경로가 거기서 판정한다.

▶ 왜 퀀트 프로필인가
  dispatch-guard 가 시장 DB DSN 을 `quant-backtest-department` 에만 준다(실측).
  측정을 하려면 시장 데이터가 필요하므로 카드는 그 프로필로 간다.

사용:
    python3 research_bench.py --self-check
    python3 research_bench.py --once --dry-run
    python3 research_bench.py --once
    python3 research_bench.py --loop --interval-min 20
    python3 research_bench.py --idea "장 마감 전 호가 소멸을 봐라"
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

import research_log as rlog                                    # noqa: E402

MODULE_VERSION = "research-bench-v1"

# f-string 안에서 줄바꿈을 안전하게 쓰기 위한 상수.
_NL = chr(10)

FACTORY_CONTAINER = os.getenv("KANBAN_CLI_CONTAINER",
                              "hedgefund-factory-kanban-dispatcher")
FACTORY_BOARD = os.getenv("FACTORY_KANBAN_BOARD", "alpha-factory")
# 시장 DB DSN 을 가진 유일한 프로필(dispatch-guard 스코핑, 2026-08-24 실측).
BENCH_ASSIGNEE = "quant-backtest-department"

# 한 번에 한 실험만 연다. 병렬로 열면 발견이 서로를 못 본다 - 계보가 끊긴다.
MAX_OPEN = int(os.getenv("RESEARCH_MAX_OPEN", "1"))

BENCH_ORIGIN_HEADER = (
    "origin=research-bench\n"
    "workflow_plane=alpha-factory\n"
    "user_query_routing=forbidden"
)

# 큐가 마르면 쓰는 씨앗. **오늘 실측에서 나온 진짜 미해결 질문들**이다
# (후보 45개 중 순수익 양수 0개 · 1,790만 관측 → 기회 2건).
SEED_QUESTIONS = [
    "후보 45개 중 순수익 양수가 0개인 것은 스프레드 때문인가, 지평선 때문인가, "
    "아니면 사건 정의가 너무 좁아서인가? 셋을 분해해서 각각의 기여를 재라.",
    "23bp 왕복 허들을 지평선별(30초·1분·5분·30분)로 나누면 어느 지평선에서 "
    "총마크아웃이 허들을 넘기 시작하는가? 넘는 지평선이 아예 없는가?",
    "사건 정의(OFI 임계)를 완화하면 기회 수와 평균 엣지가 어떻게 교환되는가? "
    "기회 수가 통계에 충분해지는 지점에서 엣지가 살아 있는가?",
    "체결 가능성을 TAKER 대신 패시브(대기열 하한)로 재면 순엣지가 어떻게 바뀌는가?",
    "종목 2,522개 중 실제로 사건이 발생하는 종목은 몇 개이고, 그들의 공통 "
    "특성(유동성·스프레드·거래대금)은 무엇인가?",
]


def _run(argv: list[str], timeout: int = 60) -> tuple[int, str]:
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _cli(*args: str) -> list[str]:
    return ["docker", "exec", "-u", "1000", "-i", FACTORY_CONTAINER,
            "hermes", "kanban", "--board", FACTORY_BOARD, *args]


# ── 다음 질문 고르기 ────────────────────────────────────────────────────────
def pick_question() -> tuple[str, str, str, str]:
    """(질문, 출처, 부모, 종류) - **직전 발견이 낳은 질문이 자기개선의 엔진이다.**

    ▶ 문헌 질문이 측정 질문보다 먼저인 이유 (2026-08-25)
      벽에 부딪혀 "남들은 이걸 어떻게 넘었나" 가 나왔다면, 그 답을 모르는 채로
      다음 측정을 설계하면 **같은 우물을 다시 판다.** 읽는 것이 먼저다.
      대신 문헌 카드는 반드시 측정 질문을 낳아야 해서(계약) 루프가 독서로
      새지 않는다.
    """
    asked = {e.question.strip() for e in rlog.read_log()}

    # ⓪ **확증이 무엇보다 먼저다.** 후보를 지목해 놓고 탐색을 계속하면
    #   홀드아웃 판정이 계속 미뤄지고, 그 사이 탐색이 홀드아웃을 오염시킬
    #   위험만 쌓인다. 지목됐으면 즉시 확증한다.
    for cand_entry in rlog.pending_candidates():
        cand = cand_entry.candidate or {}
        q = (f"[확증] {cand.get('name') or cand_entry.id}: "
             f"{cand.get('claim') or ''}").strip()
        if q not in asked:
            return q, "auto", cand_entry.id, "confirm"

    for idea in rlog.pending_ideas():                  # ① 사람 개입이 최우선
        q = str(idea.get("question") or "").strip()
        if q and q not in asked:
            return q, "user", "", "measure"

    for found in rlog.recent_findings(limit=8):        # ② 벽을 만났으면 읽는다
        for q in getattr(found, "lit_questions", []) or []:
            q = str(q).strip()
            if q and q not in asked:
                return q, "auto", found.id, "literature"

    for found in rlog.recent_findings(limit=8):        # ③ 자기개선(측정)
        for q in found.next_questions:
            q = str(q).strip()
            if q and q not in asked:
                return q, "auto", found.id, "measure"

    for q in SEED_QUESTIONS:                           # ④ 씨앗
        if q not in asked:
            return q, "seed", "", "measure"

    return "", "", "", ""


# ── 카드 본문 = 계약 ────────────────────────────────────────────────────────
def card_body(entry_id: str, question: str, parent: str) -> str:
    findings = rlog.recent_findings(limit=4)
    prior = ""
    if findings:
        blocks = []
        for f in findings:
            nums = ", ".join(f"{k}={v}" for k, v in list(f.numbers.items())[:6])
            blocks.append(
                f"[{f.id}] 질문: {f.question}\n"
                f"  스크립트: {f.script}\n"
                f"  숫자: {nums or '(없음)'}\n"
                f"  발견: {f.finding}")
        prior = ("\n\n## 이전 발견 (원문 그대로 - 교훈 코드가 아니다)\n\n"
                 + "\n\n".join(blocks))

    return f"""{BENCH_ORIGIN_HEADER}
factory_assignee={BENCH_ASSIGNEE}
research_entry_id={entry_id}
research_parent={parent or '(없음)'}

## 이번 질문

{question}

**너는 측정을 직접 설계한다.** 정해진 문법도, 사전등록도, 시도 예산도 없다.
`ml_pipeline/audit_*.py` 를 쓰던 방식 그대로다 - 어떻게 재야 이 질문에 답이
나오는지 네가 정하고, 스크립트를 쓰고, 돌리고, 숫자를 보고, 해석해라.
{prior}

## 실행면

  파이썬   `quant-py` (pandas·numpy·psycopg2 포함. system python 에는 없다)
  메타 DB  `$QUANT_DATABASE_URL`  (control - 가설·실험·판정·리드)
  시장 DB  `$TIMESCALE_DATABASE_URL` (market - 아래 표)

  원시 이벤트   `ext_src.quotes` (L10 호가), `ext_src.ticks` (체결)
                66세션 적재됨. 컬럼은 원본 그대로(ts, symbol, bid1..bid10,
                ask1..ask10, bid_vol1..10, ask_vol1..10, spread, bi /
                ts, symbol, price, volume, side, market, ofi_contrib)
  일별 피처     `market.microstructure_features` (ms-daily-v5, origin=external)
  종목 매핑     `market.symbol_map` (symbol -> instrument_id)
  일봉          `market.market_bars` (interval_code='1D', 2016~)

  ⚠ `quant.hypotheses` 등 quant 스키마는 RLS 가 svc_quant 전용이다.
    연결 직후 `set role svc_quant` 를 하지 않으면 **오류 없이 0행**이 보인다.

## 싸게 먼저 재라 - 이건 규칙이다

  `ext_src.quotes`(2.7억 행)·`ext_src.ticks`(2억 행)·`market.market_quotes`
  같은 원시 표에 **전체 `count(*)`·`count(distinct)`·정렬을 걸지 마라.**
  압축 하이퍼테이블이라 전 청크를 훑고, 그 긴 SELECT 하나가 **다른 작업의
  락을 막는다**(2026-08-24 실측: 집계 쿼리 하나가 압축을 죽여 이관 전체가
  중단됐고, 그 전에는 시장 API 가 3시간 33분 멈췄다).

  대신:
  - 행수·기간·세션 목록은 **청크 카탈로그**에서 (메타데이터라 즉시 끝난다)
    `select range_start::date from timescaledb_information.chunks
      where hypertable_schema='ext_src' and hypertable_name='ticks'`
  - 값이 필요하면 **하루·몇 종목으로 좁혀서** 먼저 본다
    (`where ts >= '2026-06-02' and ts < '2026-06-03' and symbol in (...)`)
  - 그렇게 모양을 본 뒤에 필요한 만큼만 넓힌다

  **한 번에 다 재려다 아무것도 못 재는 것보다, 좁게 여러 번이 빠르다.**

## 자원 예산 - 넘으면 쿼리가 죽는다

  이 DB 는 `temp_file_limit = 1.5GB` 다(세션당). 정렬·해시·`group by` 가
  그보다 크면 **쿼리가 죽는다** - 버그가 아니라 방어다. 라이브 수집·공장·
  연구가 **같은 디스크**를 쓰고, 거기가 차면 연구가 아니라 운영이 죽는다
  (2026-08-25 실측: 전 종목 초단위 집계가 디스크를 98%까지 밀었다).

  그래서 큰 집계는 이렇게 짠다:
  - **종목을 나눠 부분집계 → 파일로 떨구고 → 마지막에 합친다.**
    하루 단위로 쪼개도 전 종목이면 여전히 크다. 종목 축도 쪼개라
    (예: `md5(symbol)` 앞 한 글자로 16조각).
  - 중간 결과는 `/app/quant-data/research/out/` 에 JSON/CSV 로 떨군다.
    DB 안에서 다 이어붙이려 하지 마라.
  - 쿼리가 `temp file limit exceeded` 로 죽으면 **범위를 반으로 줄여라.**
    같은 쿼리를 다시 던지지 마라 - 같은 자리에서 또 죽는다.

  **표본으로 답할 수 있으면 표본으로 답해라.** 결정론 표본(md5 접두)으로
  모양을 잡고, 결론이 표본에 민감할 때만 전수로 간다. r0002 가 1/256 표본으로
  165만 사건을 재서 답을 냈다 - 전수가 필요했던 게 아니다.

## 홀드아웃 - 절대 만지지 마라

  **{rlog.HOLDOUT_FROM} 이후 세션은 이 실험에서 쓰지 않는다.**
  탐색은 마음껏 p-해킹해도 된다 - 홀드아웃이 깨끗해야 나중에 승격 판정이
  정직해진다. 쓴 세션 목록을 아래 `--sessions` 로 반드시 신고해라.

## 산출물 - 이 셋을 만들면 끝이다

1. **스크립트**: 반드시 `/app/quant-data/research/scripts/{entry_id}_<짧은이름>.py`
   에 둔다. **다른 곳(`/opt/data` 등)에 쓰면 계보에서 사라진다** - 다음 실험의
   내가 그 코드를 못 읽으면 같은 자리를 다시 판다. 탐색용 임시 파일을 딴 데
   썼더라도 **최종 스크립트는 여기로 옮겨라.**
   - 돌려서 숫자가 나와야 한다. 안 돌아가면 고쳐서 돌려라(그게 일이다).
   - 출력은 `/app/quant-data/research/out/` 아래로.
2. **숫자**: 질문에 답하는 핵심 수치 몇 개(전부가 아니라 답이 되는 것).
3. **발견 + 다음 질문**: 아래 명령 한 번으로 기록한다.

```
quant-py /app/repo/departments/01-research/bench/research_log.py close \\
  --id {entry_id} \\
  --script research/scripts/{entry_id}_<이름>.py \\
  --numbers '{{"핵심수치": 값, "...": ...}}' \\
  --finding '무엇을 알았는가 - 숫자가 무슨 뜻인지 한두 문단' \\
  --next '다음에 재볼 것 1' --next '다음에 재볼 것 2' \\
  --sessions 2026-06-02 --sessions 2026-06-03
```

## 규율 세 줄

- **숫자에는 출처가 있어야 한다.** 스크립트 없이 숫자만 적으면 기록이 거부된다.
  네 기억에서 나온 숫자는 숫자가 아니다.
- **발견은 다음 질문을 낳아야 한다.** 없으면 루프가 멎는다. "안 됐다" 도
  발견이다 - 무엇이 왜 안 됐는지 적고 그래서 무엇을 다르게 볼지 적어라.
- **못 하겠으면 못 하겠다고 적어라.** 지어낸 숫자 하나가 뒤의 실험 열 개를
  오염시킨다. 막혔으면 `--finding` 에 무엇이 막았는지 쓰고 다음 질문을 남겨라.
"""


# ── 문헌 카드 본문 ──────────────────────────────────────────────────────────
def literature_card_body(entry_id: str, question: str, parent: str) -> str:
    """읽는 카드. **산출물은 반드시 "다음에 무엇을 재볼지" 로 끝난다.**"""
    findings = rlog.recent_findings(limit=3)
    prior = ""
    if findings:
        blocks = []
        for f in findings:
            nums = ", ".join(f"{k}={v}"
                             for k, v in list(f.numbers.items())[:6])
            blocks.append(
                f"[{f.id}] {f.question}" + _NL
                + f"  숫자: {nums or '(없음)'}" + _NL
                + f"  발견: {f.finding}")
        prior = (_NL + _NL + "## 우리가 부딪힌 벽 (원문)" + _NL + _NL
                 + (_NL + _NL).join(blocks))

    return f"""{BENCH_ORIGIN_HEADER}
factory_assignee={BENCH_ASSIGNEE}
research_entry_id={entry_id}
research_parent={parent or '(없음)'}
research_kind=literature

## 이번에 읽을 것

{question}

**이 카드는 재는 카드가 아니라 읽는 카드다.** 우리가 실측으로 벽을 만났고,
그 벽을 남들은 어떻게 넘었는지(혹은 못 넘었는지) 찾는 것이 임무다.
{prior}

## 읽는 도구 (이 컨테이너에 다 있다)

  `agent-reach doctor`         살아 있는 채널 확인
  arXiv q-fin                  `reach-py -c "import feedparser; ..."`
  임의 페이지                  `curl -s https://r.jina.ai/<URL>`
  발표·인터뷰                  `yt-dlp --write-auto-subs --skip-download <URL>`
  코드·저장소                  `gh search repos` / `gh search code`
  의미검색                     `mcporter` (Exa)

## 산출물 - 이 셋

1. **출처**: 실제로 읽은 것의 URL 을 `--cite` 로 전부 남긴다. 못 읽었으면
   못 읽었다고 적어라 - **읽은 척이 제일 나쁘다.**
2. **발견**: 남들이 이 벽에서 무엇을 했는가. 우리 숫자와 **대조해서** 적어라
   (예: "우리 총엣지 2.84bp vs 스프레드 38.55bp. 문헌 X 는 같은 문제를
   패시브 체결로 우회했고 왕복 비용을 스프레드의 절반으로 잡는다").
3. **다음에 잴 것**: `--next` 로 **측정 질문**을 반드시 남긴다. 문헌만 읽고
   끝나면 루프가 독서로 샌다. "그래서 우리 데이터로 무엇을 재면 이게 참인지
   알 수 있는가" 를 적어라.

```
quant-py /app/repo/departments/01-research/bench/research_log.py close \
  --id {entry_id} \
  --cite 'https://...' --cite 'https://...' \
  --finding '남들은 이 벽을 이렇게 다뤘고, 우리 숫자와 이렇게 다르다' \
  --next '그래서 우리 데이터로 이것을 재보자' \
  --next-lit '이건 더 읽어봐야 한다(있으면)'
```

## 규율

- **읽은 것만 인용한다.** 제목만 보고 내용을 지어내면 뒤의 측정이 통째로 헛돈다.
- **우리 숫자와 대조한다.** 남의 결론을 그대로 옮기는 것은 발견이 아니다.
  우리가 잰 것과 어디가 같고 어디가 다른지가 발견이다.
- **채널이 말랐으면 말랐다고 적어라.** 빈 것도 사실이다(어느 렌즈가 죽었는지
  알아야 다음에 다른 문을 두드린다).
"""


# ── 확증 카드 본문 ──────────────────────────────────────────────────────────
def confirm_card_body(entry_id: str, parent_entry, prereg: str) -> str:
    """확증 카드. **탐색 카드와 규율이 정반대다** - 홀드아웃만 쓰고 사양은 못 고친다."""
    import json as _json
    cand = parent_entry.candidate or {}
    spec = _json.dumps(cand, ensure_ascii=False, indent=2, sort_keys=True)
    return (BENCH_ORIGIN_HEADER + _NL
            + f"factory_assignee={BENCH_ASSIGNEE}" + _NL
            + f"research_entry_id={entry_id}" + _NL
            + f"research_parent={parent_entry.id}" + _NL
            + "research_kind=confirm" + _NL
            + f"prereg_sha256={prereg}" + _NL + _NL
            + "## 동결된 사양 - **한 글자도 바꾸지 마라**" + _NL + _NL
            + "```json" + _NL + spec + _NL + "```" + _NL + _NL
            + "이 사양의 지문이 위 `prereg_sha256` 이다. 홀드아웃을 본 뒤"
            + " 사양을" + _NL
            + "고치면 그 판정은 확증이 아니라 **두 번째 탐색**이 된다."
            + " 승격 다리가" + _NL
            + "지문을 다시 계산해 대조하므로, 바꾸면 승격에서 거부된다." + _NL + _NL
            + "## 이번엔 홀드아웃만 쓴다 - 탐색과 정반대다" + _NL + _NL
            + f"  **{rlog.HOLDOUT_FROM} 이후 세션만** 쓴다."
            + " 개발 구간(그 이전)은 이 카드에서" + _NL
            + "  쓰지 않는다 - 이미 거기서 나온 성적은 근거가 아니다"
            + "(탐색은 p-해킹을" + _NL
            + "  허용한 구간이다). 홀드아웃은 탐색이 **한 번도 안 본** 데이터라"
            + " 여기서" + _NL
            + "  나온 숫자만 정직하다." + _NL + _NL
            + "  부모 실험: " + parent_entry.id + _NL
            + "  부모 발견: " + str(parent_entry.finding)[:400] + _NL + _NL
            + "## 할 일" + _NL + _NL
            + "1. 동결 사양의 스크립트를 **그대로** 홀드아웃 세션에 돌린다."
            + " 스크립트가" + _NL
            + "   개발 구간을 하드코딩했으면 **세션 목록만** 바꾼다"
            + "(로직·파라미터 금지)." + _NL
            + "2. 결과를 판정한다. 통과 기준은 사양의 `claim`·`expected` 다 -"
            + " 지금" + _NL
            + "   새로 정하지 마라. 애매하면 실패로 적어라." + _NL
            + "3. 아래로 기록한다." + _NL + _NL
            + "```" + _NL
            + "quant-py /app/repo/departments/01-research/bench/"
            + "research_log.py close \\" + _NL
            + f"  --id {entry_id} \\" + _NL
            + f"  --script research/scripts/{entry_id}_confirm.py \\" + _NL
            + "  --confirm-result '{\"pass\": true, \"net_bps\": 0.0,"
            + " \"n\": 0}' \\" + _NL
            + "  --numbers '{\"...\": ...}' \\" + _NL
            + "  --finding '홀드아웃에서 무엇이 나왔는가 - 통과/실패와 그 근거' \\"
            + _NL
            + "  --next '다음에 볼 것' \\" + _NL
            + "  --sessions 2026-08-10 --sessions 2026-08-11" + _NL
            + "```" + _NL + _NL
            + "## 규율" + _NL + _NL
            + "- **실패도 성공만큼 값지다.** 여기서 걸러야 돈이 안 든다."
            + " 통과시키려고" + _NL
            + "  기준을 느슨하게 읽지 마라 - 승격 다리가 사양 지문을 대조한다."
            + _NL
            + "- **쓴 홀드아웃 세션을 `--sessions` 로 전부 신고한다.**"
            + " 신고 안 하면" + _NL
            + "  승격에서 `HOLDOUT_NOT_USED` 로 거부된다." + _NL
            + "- 사양이 홀드아웃에서 **돌아가지 않으면** 그것도 결과다"
            + "(pass=false + 사유)." + _NL)


# ── 한 주기 ─────────────────────────────────────────────────────────────────
def run_once(*, dry_run: bool = False) -> dict:
    open_now = rlog.open_questions()
    if len(open_now) >= MAX_OPEN:
        ids = ", ".join(e.id for e in open_now)
        print(f"  열린 실험 {len(open_now)}건({ids}) - 새로 안 연다", flush=True)
        return {"action": "WAIT", "open": [e.id for e in open_now]}

    question, origin, parent, kind = pick_question()
    if not question:
        print("  낼 질문이 없다 - 아이디어를 넣거나 씨앗을 늘려라", flush=True)
        return {"action": "NONE"}

    if dry_run:
        print(f"  [dry-run] 다음 질문({kind}/{origin}, 부모 {parent or '-'}): "
              f"{question[:90]}", flush=True)
        return {"action": "WOULD_OPEN", "origin": origin, "kind": kind}

    parent_entry = rlog.latest_by_id().get(parent) if parent else None
    prereg = ""
    if kind == "confirm" and parent_entry is not None:
        # **사전등록은 여기서 박힌다** - 홀드아웃을 보기 전이다.
        prereg = rlog.prereg_fingerprint(parent_entry.candidate or {})
    entry = rlog.open_entry(question, origin=origin, parent=parent, kind=kind,
                            candidate=(parent_entry.candidate or {})
                            if (kind == "confirm" and parent_entry) else None,
                            prereg_sha256=prereg)
    if kind == "confirm" and parent_entry is not None:
        body = confirm_card_body(entry.id, parent_entry, prereg)
        label = "확증"
    elif kind == "literature":
        body = literature_card_body(entry.id, question, parent)
        label = "문헌"
    else:
        body = card_body(entry.id, question, parent)
        label = "연구"
    rc, out = _run(_cli(
        "create", f"{label}: {question[:60]}",
        "--assignee", BENCH_ASSIGNEE,
        "--idempotency-key", f"research-bench-{entry.id}",
        "--created-by", MODULE_VERSION,
        "--priority", "3",
        "--body", body))
    if rc != 0:
        print(f"  카드 생성 실패: {out.strip()[:160]}", flush=True)
        return {"action": "CREATE_FAILED", "entry": entry.id}

    m = re.search(r"\bt_[0-9a-f]{6,}\b", out)
    card = m.group(0) if m else ""
    if card:
        # 카드 id 는 카드를 만든 **뒤에야** 안다(본문에 entry_id 가 필요하므로
        # 순서를 못 바꾼다). 추가 전용 로그라 갱신 줄을 하나 더 얹는다 -
        # `latest_by_id()` 가 마지막 줄을 현재 상태로 본다.
        entry.card = card
        rlog.append(entry)
    if origin == "user":
        rlog.consume_idea(question)
    print(f"  {entry.id} 열림 ({origin}, 부모 {parent or '-'}) -> {card}\n"
          f"      {question[:100]}", flush=True)
    return {"action": "OPENED", "entry": entry.id, "card": card,
            "origin": origin, "kind": kind}


def status() -> None:
    entries = rlog.latest_by_id()
    done = [e for e in entries.values() if e.status == "DONE"]
    print(f"연구 로그: 실험 {len(entries)}건 (완료 {len(done)})")
    for e in sorted(entries.values(), key=lambda x: x.id)[-8:]:
        head = f"  {e.id} [{e.status:4}] {e.origin:5}"
        print(f"{head} {e.question[:70]}")
        if e.finding:
            print(f"        발견: {e.finding[:90]}")


# ── 자체 점검 ───────────────────────────────────────────────────────────────
def _selfcheck() -> int:
    import tempfile
    fails = 0

    def ok(name, cond):
        nonlocal fails
        print(("  ✓ " if cond else "  ✗ ") + name)
        if not cond:
            fails += 1

    argv = _cli("list")
    ok("보드를 못박는다",
       "--board" in argv and argv[argv.index("--board") + 1] == FACTORY_BOARD)
    ok("시장 DSN 을 가진 프로필로 보낸다",
       BENCH_ASSIGNEE == "quant-backtest-department")
    ok("사용자 질의면으로 오인되지 않는다",
       "origin=research-bench" in BENCH_ORIGIN_HEADER
       and "origin=user-query" not in BENCH_ORIGIN_HEADER)

    with tempfile.TemporaryDirectory() as td:
        # **경로를 전부 바꾼다.** 하나라도 빠뜨리면 자체점검이 실제 작업
        # 디렉터리를 만들려 든다(2026-08-25 실측: /app 권한 오류로 터졌다).
        rlog.ROOT = Path(td)
        rlog.LOG_PATH = rlog.ROOT / "log.jsonl"
        rlog.SCRIPTS_DIR = rlog.ROOT / "scripts"
        rlog.OUT_DIR = rlog.ROOT / "out"
        rlog.IDEA_QUEUE = rlog.ROOT / "ideas.jsonl"

        q, origin, parent, kind = pick_question()
        ok("큐가 비면 씨앗에서 고른다",
           origin == "seed" and bool(q) and kind == "measure")

        rlog.add_idea("사람이 던진 질문")
        q2, o2, _, _ = pick_question()
        ok("사람 아이디어가 최우선", o2 == "user" and q2 == "사람이 던진 질문")

        e = rlog.open_entry("첫 질문", origin="seed")
        rlog.close_entry(e.id, script="s.py", numbers={"a": 1},
                         finding="알아낸 것", next_questions=["파생 질문"],
                         lit_questions=["남들은 이 벽을 어떻게 넘었나"],
                         sessions_used=["2026-07-01"])
        rlog.consume_idea("사람이 던진 질문")
        q3, o3, p3, k3 = pick_question()
        ok("벽을 만나면 읽는 것이 먼저다",
           k3 == "literature" and q3 == "남들은 이 벽을 어떻게 넘었나"
           and p3 == e.id)

        e2 = rlog.open_entry(q3, origin=o3, parent=p3, kind="literature")
        rlog.close_entry(e2.id, script="", numbers={},
                         finding="문헌은 패시브 체결로 우회했다",
                         next_questions=["패시브 체결로 재보자"],
                         citations=["https://example.org/paper"])
        q4, o4, p4, k4 = pick_question()
        ok("문헌 뒤에는 측정으로 돌아온다",
           k4 == "measure" and q4 in ("패시브 체결로 재보자", "파생 질문"))
        ok("문헌 카드는 스크립트 없이도 닫힌다",
           rlog.latest_by_id()[e2.id].status == "DONE")
        ok("인용이 기록된다",
           rlog.latest_by_id()[e2.id].citations == ["https://example.org/paper"])

        lit = literature_card_body("r0009", "남들은?", e.id)
        ok("문헌 카드에 읽는 도구가 있다",
           "agent-reach" in lit and "yt-dlp" in lit)
        ok("문헌 카드가 측정 질문을 요구한다", "다음에 잴 것" in lit)
        ok("문헌 카드가 우리 숫자와 대조를 요구한다", "우리 숫자와 대조" in lit)

        body = card_body("r0002", "측정 질문", e.id)
        ok("카드에 이전 발견 원문이 실린다", "알아낸 것" in body)
        ok("카드에 홀드아웃 경고가 있다", rlog.HOLDOUT_FROM in body)
        ok("카드에 RLS 함정 경고가 있다", "set role svc_quant" in body)
        ok("카드가 측정 설계를 맡긴다", "측정을 직접 설계한다" in body)
        ok("카드에 종료 명령이 있다", "research_log.py close" in body)
        ok("카드가 비싼 쿼리를 금지한다", "싸게 먼저 재라" in body)
        ok("카드가 스크립트 경로를 강제한다", "계보에서 사라진다" in body)

    print("자체점검 통과" if fails == 0 else f"자체점검 실패 {fails}건")
    return fails


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    m = p.add_mutually_exclusive_group(required=True)
    m.add_argument("--once", action="store_true")
    m.add_argument("--loop", action="store_true")
    m.add_argument("--self-check", action="store_true")
    m.add_argument("--status", action="store_true")
    m.add_argument("--idea", default="", help="아이디어를 큐에 넣는다")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--interval-min", type=int, default=20)
    a = p.parse_args(argv)

    if a.self_check:
        return _selfcheck()
    if a.status:
        status()
        return 0
    if a.idea:
        rlog.add_idea(a.idea)
        print(f"아이디어 접수: {a.idea[:80]}")
        return 0
    if a.once:
        run_once(dry_run=a.dry_run)
        return 0
    interval = max(2, a.interval_min) * 60
    print(f"{MODULE_VERSION} 반복 시작 - {a.interval_min}분마다", flush=True)
    while True:
        try:
            run_once(dry_run=a.dry_run)
        except Exception as e:                  # 벤치가 죽어도 공장은 산다
            print(f"  벤치 주기 오류(계속): {e}", flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
