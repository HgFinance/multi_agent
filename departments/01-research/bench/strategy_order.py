#!/usr/bin/env python3
"""전략 주문 - **사람이 목표를 주면 단계를 밟아 배포 가능한 전략까지 간다.**

담당: 재일 (리서치본부 RES)

▶ 왜 무한 루프를 접었나 (2026-08-26)
  자율 루프가 80건을 돌고 후보 1건·확증 통과 0건이었다. 실패 원인은 에이전트가
  아니라 **종점이 없다**는 것이었다. 종점이 없으면 "다음 질문" 이 영원히 있고,
  그중 어느 것도 배포 가능한 전략에 가까워질 의무가 없다.

  더 나쁜 일도 있었다. r0038 이 총엣지 +101.9bp 를 내자 그게 성과처럼 보였는데,
  비용·진입 가정을 넣자 -547bp 였다. **재는 순서가 없으니 3단계 숫자를 최종
  성과로 착각**했다.

▶ 그래서 순서를 고정한다
  존재(총엣지) → 비용(순엣지) → 강건(창·레짐) → 팩 → 배포.
  **앞 단계가 죽으면 뒤는 돌지 않는다.** 이게 낭비를 막는 유일한 장치다.

▶ 사람이 잡는 두 지점
  ① 해석 확인 - "이동평균선 추세추종" 을 어떻게 읽었는지 보여주고 고치게 한다.
     해석이 틀리면 뒤의 실험 전부가 헛돈다.
  ② 배포 방아쇠 - 통과해도 자동 배포하지 않는다.

사용:
    strategy_order.py order "이동평균선을 이용한 추세추종 전략 만들어줘"
    strategy_order.py show <주문번호>
    strategy_order.py confirm <주문번호>            # 해석 승인
    strategy_order.py revise <주문번호> --spec '...' # 해석 수정
    strategy_order.py advance <주문번호>            # 다음 단계 실행
    strategy_order.py status
    strategy_order.py --self-check
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import research_log as rlog  # noqa: E402

MODULE_VERSION = "strategy-order-v1"

ROOT = Path(os.getenv("RESEARCH_ROOT", "/app/quant-data/research"))
ORDERS = ROOT / "orders.jsonl"

FACTORY_CONTAINER = os.getenv("FACTORY_CONTAINER",
                              "hedgefund-factory-kanban-dispatcher")
FACTORY_BOARD = os.getenv("FACTORY_BOARD", "alpha-factory")
ASSIGNEE = os.getenv("BENCH_ASSIGNEE", "quant-backtest-department")

_NL = chr(10)

# ── 단계 정의 ───────────────────────────────────────────────────────────────
# **순서가 계약이다.** 앞이 죽으면 뒤는 안 돈다.
STAGES = [
    ("interpret", "해석",
     "사람 말을 검증 가능한 사양으로 바꾼다. 무엇을 사고 언제 팔고 "
     "무엇을 이기면 성공인지."),
    ("prior", "선행",
     "우리가 이미 잰 것과 문헌을 확인한다. 같은 걸 이미 재봤으면 그 숫자를 쓴다."),
    ("exists", "존재",
     "비용을 빼기 전 총엣지가 우리 데이터에 있기는 한가. 없으면 여기서 끝."),
    ("cost", "비용",
     "실제 체결 가정(진입가·스프레드·유동성)으로도 남는가. 대부분 여기서 죽는다."),
    ("robust", "강건",
     "창을 나눠도·레짐이 바뀌어도 남는가. 홀드아웃까지."),
    ("pack", "팩",
     "배포 가능한 산출물로 굽는다. 모델·피처·게이트·구현 SHA."),
    ("deploy", "배포",
     "사람이 방아쇠를 당긴다. 자동으로 하지 않는다."),
]
STAGE_KEYS = [k for k, _, _ in STAGES]
HUMAN_GATES = {"interpret", "deploy"}   # 사람이 반드시 거치는 두 지점


@dataclass
class Order:
    """주문 하나. **append-only** 로 기록하고 마지막 줄이 현재 상태다."""
    id: str
    ts: str
    request: str                     # 사람이 쓴 원문. 절대 고쳐 쓰지 않는다.
    stage: str = "interpret"
    # OPEN | AWAITING_HUMAN | PROMOTABLE | CHARACTERIZED | BLOCKED
    #
    # **CHARACTERIZED 가 핵심이다.** 기준 미달이어도 팩은 만든다 -
    # 승자만 내놓는 공장은 대부분의 경우 아무것도 안 내놓는다.
    status: str = "OPEN"
    spec: dict = field(default_factory=dict)      # 해석 결과
    spec_confirmed: bool = False
    stage_results: dict = field(default_factory=dict)  # 단계별 요약
    experiments: list = field(default_factory=list)    # 연결된 r#### 목록
    cards: list = field(default_factory=list)
    shortfall_at: str = ""      # 어느 단계에서 기준에 못 미쳤나
    shortfall_reason: str = ""
    verdict: str = ""           # PROMOTABLE|CHARACTERIZED|BLOCKED
    pack_path: str = ""
    note: str = ""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _ensure() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)


def _append(o: Order) -> None:
    _ensure()
    with ORDERS.open("a", encoding="utf-8") as f:
        f.write(json.dumps(asdict(o), ensure_ascii=False) + _NL)


def read_orders() -> list:
    if not ORDERS.exists():
        return []
    out = []
    for line in ORDERS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if not isinstance(raw, dict) or not raw.get("id"):
            continue
        # **읽기는 관대하게.** 모르는 필드는 버리고 빠진 필드는 기본값으로
        # 채운다. 스키마가 바뀌었다고 옛 기록이 사라지면 안 된다.
        known = {f for f in Order.__dataclass_fields__}
        out.append(Order(**{k: v for k, v in raw.items() if k in known}))
    return out


def latest() -> dict:
    """주문번호 → 최신 상태. append-only 라 나중 줄이 이긴다."""
    cur = {}
    for o in read_orders():
        cur[o.id] = o
    return cur


def new_order(request: str) -> Order:
    """사람의 원문을 그대로 받는다.

    **원문을 고쳐 저장하지 않는다.** 해석은 `spec` 에 따로 담는다 - 나중에
    "내가 이렇게 말한 적 없는데" 를 가릴 수 있어야 한다.
    """
    request = str(request).strip()
    if not request:
        raise ValueError("빈 주문은 받을 수 없다")
    o = Order(id="ord_" + uuid.uuid4().hex[:8], ts=_now(), request=request)
    _append(o)
    return o


def next_stage(stage: str) -> str:
    i = STAGE_KEYS.index(stage)
    return STAGE_KEYS[i + 1] if i + 1 < len(STAGE_KEYS) else ""


def stage_title(stage: str) -> str:
    for k, t, _ in STAGES:
        if k == stage:
            return t
    return stage


def stage_goal(stage: str) -> str:
    for k, _, g in STAGES:
        if k == stage:
            return g
    return ""


# ── 카드 본문 ───────────────────────────────────────────────────────────────
def _bench_blocks() -> str:
    """벤치가 이미 만든 블록들을 그대로 쓴다 - 작업실·lib·스킬·표본.

    주문 파이프라인이라고 다른 규율을 쓸 이유가 없다. **한 곳에서만 정의한다.**
    """
    try:
        import research_bench as rb
    except ImportError:
        return ""
    parts = []
    for fn in ("workspace_block", "sample_budget_block", "agent_powers_block"):
        f = getattr(rb, fn, None)
        if f:
            try:
                parts.append(f())
            except Exception:
                pass
    return _NL.join(p for p in parts if p)


def interpret_card_body(o: Order) -> str:
    """1단계 - 사람 말을 사양으로. **여기가 틀리면 전부 헛돈다.**"""
    return f"""origin=strategy-order
workflow_plane=alpha-factory
user_query_routing=forbidden
factory_assignee={ASSIGNEE}
order_id={o.id}
order_stage=interpret

## 사람이 준 목표 - **원문 그대로**

    {o.request}

## 네가 할 일 - 이 말을 **검증 가능한 사양**으로 바꾼다

재는 카드가 아니다. **읽고 정의하는 카드**다. 사람은 한 문장을 줬고, 그
문장으로는 실험을 못 돈다. 실험할 수 있는 형태로 바꿔라.

**사양에 반드시 들어갈 것 여덟:**

0. **데이터** - 이 전략을 재려면 **어느 표의 어느 축**이 필요한가.
   위 "쓸 수 있는 표본" 절을 읽고 네가 고른다. 표 이름·기간·기대 행수와
   **왜 그 축인지**를 적어라.

   - 신호가 **일 단위**면 일봉(`market.market_bars`, `interval_code='1D'`)이다.
     장중 테이프 세션 수는 **아무 상관이 없다.** 실제로 일봉 전략의 이동평균
     기간을 장중 54세션 기준으로 "검증 불능" 처리한 일이 있었다.
   - 신호가 **분·초 단위**면 장중(`ext_src.quotes`/`ticks`)이다. 이쪽은
     홀드아웃 이전 세션 수가 제약이 된다.
   - 둘 다 필요하면 둘 다 적고, 무엇에 무엇을 쓰는지 나눠 적어라
     (예: 신호는 일봉, 체결 비용은 장중 실측 스프레드).

   **표본이 파라미터를 제약하면 그 계산을 보여라.** "SMA60 을 쓰려면 최소
   60거래일 형성기간이 필요한데 우리에겐 N일이 있다" 처럼.

1. **유니버스** - 어떤 종목을. 거래대금·가격 하한을 숫자로. 근거를 대라.
   (저가주가 비용 통계를 지배한 전례가 있다 - 2천원 미만은 평균 스프레드가
   157bp 다.)
2. **신호** - 무엇을 계산해서 무엇을 볼 때 후보로 삼는가. 수식으로.
3. **진입** - 언제 어느 가격에 사는가. **신호가 확정된 다음 봉**이어야 한다.
   같은 봉 종가 진입은 하루 움직임을 다 보고 사는 것이라 실행 불가능하다
   (실측: 종가진입 +101.9bp 가 다음날 시가진입에서 -168.6bp).
4. **청산** - 언제 파는가. 조건과 최대 보유기간을 둘 다.
5. **비용** - 왕복 비용을 무엇으로 잡는가. **가정값이 아니라 그 유니버스의
   실측 스프레드**를 쓴다고 적어라.
6. **성공 기준** - 무엇을 넘으면 성공인가. 숫자로. 비용 후 기준이어야 한다.
7. **반증 기준** - 무엇이 나오면 기각인가. 성공 기준의 반대말이 아니라
   **독립적으로** 적어라.

## 사람의 말을 넘겨짚지 마라

모호한 곳이 있으면 **네가 정하고 그렇게 정한 이유를 적어라.** 예를 들어
"이동평균선" 만 있고 기간이 없으면 관례적인 값을 쓰되 왜 그 값인지 적는다.
사람이 보고 고칠 것이다 - **고칠 수 있게 적는 것이 네 일**이다.

원문에 없는 목표를 추가하지 마라. 사람이 "추세추종" 이라고 했으면 그것을
사양으로 만드는 것이지, 더 좋아 보이는 다른 전략을 제안하는 자리가 아니다.

{_bench_blocks()}
## 산출물

```
quant-py /app/repo/departments/01-research/bench/strategy_order.py \\
  interpret-done --id {o.id} \\
  --spec '{{"data": {{...}}, "universe": {{...}}, "signal": {{...}},
            "entry": {{...}}, "exit": {{...}}, "cost": {{...}},
            "success": {{...}}, "falsify": {{...}}}}' \\
  --rationale '왜 이렇게 읽었는지, 어디를 네가 정했는지'
```

`--spec` 은 위 여덟 항목을 **전부** 담아야 한다. 하나라도 비면 거부된다.
"""


def measure_card_body(o: Order, stage: str) -> str:
    """3~5단계 - 실제로 재는 카드."""
    prior = _NL.join(
        f"  [{k}] {json.dumps(v, ensure_ascii=False)[:400]}"
        for k, v in (o.stage_results or {}).items()) or "  (없음)"
    spec = json.dumps(o.spec, ensure_ascii=False, indent=2)
    return f"""origin=strategy-order
workflow_plane=alpha-factory
user_query_routing=forbidden
factory_assignee={ASSIGNEE}
order_id={o.id}
order_stage={stage}

## 주문 - 사람이 준 목표

    {o.request}

## 확정된 사양 - **사람이 승인했다. 바꾸지 마라.**

```json
{spec}
```

**데이터 축은 사양에 적힌 것을 쓴다.** 다른 표가 더 좋아 보여도 바꾸지 마라 - 앞 단계와 비교가 깨진다.

사양을 바꾸고 싶으면 **바꾸지 말고 발견에 적어라.** 사람이 다시 승인해야
한다. 사양을 몰래 고치면 앞 단계 결과와 비교가 깨진다.

## 이번 단계 - **{stage_title(stage)}**

{stage_goal(stage)}

## 앞 단계에서 나온 것

{prior}

## 이 단계의 통과 조건

{_stage_bar(stage)}

{_bench_blocks()}
## 산출물

숫자를 내고 아래로 닫는다. **통과/탈락을 네가 판정해서 적어라.**

```
quant-py /app/repo/departments/01-research/bench/strategy_order.py \\
  stage-done --id {o.id} --stage {stage} \\
  --verdict PASS|FAIL \\
  --numbers '{{"핵심수치": 값}}' \\
  --finding '숫자가 무슨 뜻인지' \\
  --script research/scripts/{o.id}_{stage}.py \\
  --sessions 2026-06-02 --sessions 2026-06-03
```

**FAIL 도 정상적인 결말이다.** 이 단계에서 죽으면 뒤 단계는 안 돈다 -
그게 이 파이프라인의 요점이다. 억지로 PASS 를 만들지 마라.
"""


def _stage_bar(stage: str) -> str:
    """단계별 통과 조건. **앞 단계가 죽으면 뒤는 안 돈다**는 원칙의 구현."""
    bars = {
        "prior":
            "  우리 원장과 문헌에서 이 사양에 해당하는 기존 측정을 찾는다.\n"
            "  **이미 잰 것이 있으면 다시 재지 말고 그 숫자를 쓴다.**\n"
            "  통과 조건은 없다 - 찾았든 없든 다음으로 간다. 다만 이미 기각된\n"
            "  것과 같은 사양이면 그 사실을 적고 FAIL 로 닫아라.",
        "exists":
            "  **비용을 빼기 전** 총엣지가 0보다 유의하게 큰가.\n"
            "  - 여기서 비용을 넣지 마라. 그건 다음 단계다.\n"
            "  - 없으면 FAIL. 이 사양으로는 더 볼 것이 없다.\n"
            "  - 있으면 크기를 적어라 - 다음 단계에서 비용과 비교한다.",
        "cost":
            "  사양에 적힌 진입·청산·비용 가정으로 **순엣지가 양수**인가.\n"
            "  - 비용은 **그 유니버스의 실측 스프레드**로 넣어라. 가정값 금지.\n"
            "  - 진입은 사양대로. 같은 봉 종가 진입이면 그 자체가 FAIL 이다.\n"
            "  - 순엣지가 0 이하면 FAIL. 얼마나 모자랐는지 숫자로 적어라.",
        "robust":
            "  창을 나눠도·레짐이 달라도 남는가.\n"
            "  - walk-forward 로 비중첩 창을 여러 개 만들어 창별로 낸다.\n"
            "  - 창의 과반이 음수면 FAIL.\n"
            "  - **홀드아웃(2026-08-06 이후)은 여기서 처음 연다.**\n"
            "    홀드아웃에서도 양수여야 PASS.",
        "pack":
            "  **기준을 넘었든 못 넘었든 산출물을 만든다.** 못 넘었으면\n"
            "  `promotable: false` 와 미달 사유·미달 폭을 pack.json 에 적어라.\n"
            "  쓸 수 없는 전략이라는 뜻이 아니라 **단독 배포 후보가 아니라는**\n"
            "  뜻이다 - 다른 신호와 섞거나 다음 연구의 출발점이 된다.\n"
            "  - `~/mlpipe-paper/packs/<날짜>/` 형식을 따른다\n"
            "    (pack.json + 모델 파일 + 피처 목록 + 게이트 임계값).\n"
            "  - `implementation_sha256` 에 쓴 스크립트 전부의 해시를 넣는다.\n"
            "  - 팩만 만들고 **배포하지 마라.** 방아쇠는 사람이 당긴다.",
    }
    return bars.get(stage, "  (조건 없음)")


# ── 카드 발행 ───────────────────────────────────────────────────────────────
def _cli(*args: str) -> list:
    return ["docker", "exec", "-u", "1000", "-i", FACTORY_CONTAINER,
            "hermes", "kanban", "--board", FACTORY_BOARD, *args]


def _run(argv: list, timeout: int = 90) -> tuple:
    r = subprocess.run(argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def issue_card(o: Order, stage: str, *, dry_run: bool = False) -> str:
    body = (interpret_card_body(o) if stage == "interpret"
            else measure_card_body(o, stage))
    title = f"주문 {o.id} [{stage_title(stage)}]: {o.request[:40]}"
    if dry_run:
        return "(dry-run)"
    # 제목은 **위치 인자**다(--title 이 아니다). idempotency-key 로 같은
    # 주문·단계가 두 번 발행되는 것을 막는다.
    rc, out = _run(_cli(
        "create", title,
        "--assignee", ASSIGNEE,
        # **회차를 키에 넣는다.** 해석을 다시 받아야 할 때가 있고
        # (사양 항목이 늘거나 사람이 되돌릴 때) 키가 같으면 멱등에
        # 막혀 새 카드가 안 나간다(2026-08-26 실측).
        "--idempotency-key",
        f"order-{o.id}-{stage}-{len(o.cards)}",
        "--created-by", MODULE_VERSION,
        "--priority", "3",
        "--body", body))
    if rc != 0:
        raise RuntimeError(f"카드 생성 실패: {out.strip()[:200]}")
    import re as _re
    m = _re.search(r"t_[0-9a-f]{6,}", out)
    return m.group(0) if m else ""


# ── 상태 전이 ───────────────────────────────────────────────────────────────
# **여덟 항목.** `data` 는 2026-08-26 에 추가됐다 - 해석이 데이터 축을
# 안 고르면 뒤 단계가 각자 다른 표에서 재고, 실제로 일봉 전략을 장중
# 세션 수로 판단하는 일이 벌어졌다.
REQUIRED_SPEC = ("data", "universe", "signal", "entry", "exit", "cost",
                 "success", "falsify")


def interpret_done(order_id: str, spec: dict, rationale: str) -> Order:
    """해석 완료 → **사람 확인 대기.** 자동으로 다음 단계로 안 간다."""
    cur = latest().get(order_id)
    if cur is None:
        raise ValueError(f"모르는 주문: {order_id}")
    missing = [k for k in REQUIRED_SPEC
               if not (spec or {}).get(k)]
    if missing:
        raise ValueError("사양에 빠진 항목: " + ", ".join(missing))
    cur.spec = spec
    cur.spec_confirmed = False
    cur.status = "AWAITING_HUMAN"
    cur.note = str(rationale or "").strip()
    _append(cur)
    return cur


def confirm(order_id: str) -> Order:
    """사람이 해석을 승인한다. **여기서부터 자동으로 흐른다.**"""
    cur = latest().get(order_id)
    if cur is None:
        raise ValueError(f"모르는 주문: {order_id}")
    if not cur.spec:
        raise ValueError("아직 해석이 없다")
    cur.spec_confirmed = True
    cur.stage = next_stage("interpret")
    cur.status = "OPEN"
    _append(cur)
    return cur


def revise(order_id: str, spec: dict) -> Order:
    """사람이 해석을 고친다. **원문은 그대로 두고 사양만 바꾼다.**"""
    cur = latest().get(order_id)
    if cur is None:
        raise ValueError(f"모르는 주문: {order_id}")
    merged = dict(cur.spec or {})
    merged.update(spec or {})
    missing = [k for k in REQUIRED_SPEC if not merged.get(k)]
    if missing:
        raise ValueError("사양에 빠진 항목: " + ", ".join(missing))
    cur.spec = merged
    cur.spec_confirmed = False
    cur.status = "AWAITING_HUMAN"
    _append(cur)
    return cur


def stage_done(order_id: str, stage: str, verdict: str, *,
               numbers: dict, finding: str, script: str = "",
               sessions=None) -> Order:
    """단계 판정. **FAIL 이면 주문이 거기서 끝난다.**

    이게 이 파이프라인의 핵심이다 - 앞이 죽으면 뒤를 안 돌리는 것.
    80건을 돌고도 아무것도 안 나온 이유가 이 규칙이 없어서였다.
    """
    cur = latest().get(order_id)
    if cur is None:
        raise ValueError(f"모르는 주문: {order_id}")
    verdict = str(verdict).upper()
    if verdict not in ("PASS", "FAIL", "BLOCKED"):
        raise ValueError("verdict 는 PASS · FAIL · BLOCKED 중 하나")
    if not str(finding or "").strip():
        raise ValueError("발견 없이 단계를 닫을 수 없다")
    if verdict == "PASS" and stage in ("exists", "cost", "robust") \
            and not (numbers or {}):
        raise ValueError("숫자 없이 PASS 할 수 없다 - 무엇으로 통과를 판정했나")

    cur.stage_results[stage] = {
        "verdict": verdict, "numbers": numbers or {},
        "finding": str(finding).strip(), "script": script,
        "sessions": list(sessions or []), "ts": _now(),
    }
    if verdict == "BLOCKED":
        # 잴 수가 없었다. 팩도 못 만든다 - 없는 것을 굽지는 않는다.
        cur.status = "BLOCKED"
        cur.verdict = "BLOCKED"
        cur.shortfall_at = stage
        cur.shortfall_reason = str(finding)[:400]
    elif verdict == "FAIL":
        # **기준 미달이지 쓸모없음이 아니다.** 남은 측정은 건너뛰어 낭비를
        # 막되(비용에서 미달이면 강건은 안 잰다), 팩은 만들어 손에 쥐어준다.
        cur.status = "OPEN"
        cur.verdict = "CHARACTERIZED"
        cur.shortfall_at = stage
        cur.shortfall_reason = str(finding)[:400]
        cur.stage = "pack"
    else:
        nxt = next_stage(stage)
        if stage == "robust":
            cur.verdict = "PROMOTABLE"     # 강건까지 통과했다
        cur.stage = nxt or "deploy"
        cur.status = "AWAITING_HUMAN" if nxt in HUMAN_GATES else "OPEN"
    _append(cur)
    return cur


def advance(order_id: str, *, dry_run: bool = False) -> tuple:
    """다음 단계 카드를 낸다. 사람 관문이면 멈춘다."""
    cur = latest().get(order_id)
    if cur is None:
        raise ValueError(f"모르는 주문: {order_id}")
    if cur.status in ("BLOCKED", "PROMOTABLE", "CHARACTERIZED"):
        return cur, f"주문이 이미 끝났다({cur.status})"
    if cur.stage == "interpret" and not cur.spec:
        card = issue_card(cur, "interpret", dry_run=dry_run)
        if card:
            cur.cards.append(card)
            _append(cur)
        return cur, f"해석 카드 발행 {card}"
    if cur.stage == "interpret" and not cur.spec_confirmed:
        return cur, "사람 확인 대기 - `confirm` 또는 `revise` 필요"
    if cur.stage == "deploy":
        return cur, "배포 대기 - 사람이 방아쇠를 당겨야 한다"
    if cur.stage == "pack" and cur.verdict == "CHARACTERIZED":
        # 기준 미달 팩. **배포 후보가 아니라 자료로 낸다.**
        card = issue_card(cur, "pack", dry_run=dry_run)
        if card:
            cur.cards.append(card)
            _append(cur)
        return cur, f"미달 팩 카드 발행 {card} (배포 후보 아님)"
    card = issue_card(cur, cur.stage, dry_run=dry_run)
    if card:
        cur.cards.append(card)
        _append(cur)
    return cur, f"{stage_title(cur.stage)} 카드 발행 {card}"


# ── 보고 ────────────────────────────────────────────────────────────────────
def render(o: Order) -> str:
    lines = [f"주문 {o.id}  [{o.status}]  단계: {stage_title(o.stage)}",
             f"  원문: {o.request}"]
    if o.spec:
        lines.append("  해석" + (" (승인됨)" if o.spec_confirmed
                                 else " (확인 대기)") + ":")
        for k in REQUIRED_SPEC:
            v = o.spec.get(k)
            lines.append(f"    {k:9} {json.dumps(v, ensure_ascii=False)[:110]}")
    if o.note:
        lines.append(f"  해석 근거: {o.note[:300]}")
    for k, _, _ in STAGES:
        r = (o.stage_results or {}).get(k)
        if not r:
            continue
        mark = "통과" if r["verdict"] == "PASS" else "탈락"
        lines.append(f"  [{stage_title(k)}] {mark}  {r['finding'][:150]}")
        if r.get("numbers"):
            lines.append("      " +
                         json.dumps(r["numbers"], ensure_ascii=False)[:200])
    if o.verdict == "CHARACTERIZED":
        lines.append(f"  → {stage_title(o.shortfall_at)} 에서 기준 미달. "
                     "팩은 만든다(단독 배포 후보 아님).")
    elif o.verdict == "BLOCKED":
        lines.append(f"  → {stage_title(o.shortfall_at)} 에서 측정 불가")
    elif o.verdict == "PROMOTABLE":
        lines.append("  → 전 단계 통과. 배포 후보.")
    return _NL.join(lines)


# ── 자체 점검 ───────────────────────────────────────────────────────────────
def _tolerant_read_works() -> bool:
    """옛 스키마 줄과 미래 스키마 줄을 둘 다 읽어내는가."""
    import tempfile
    global ROOT, ORDERS
    saved_root, saved_orders = ROOT, ORDERS
    try:
        with tempfile.TemporaryDirectory() as td:
            ROOT = Path(td)
            ORDERS = Path(td) / "orders.jsonl"
            ORDERS.write_text(
                json.dumps({"id": "ord_old", "ts": "t", "request": "옛것",
                            "rejected_at": "cost"}, ensure_ascii=False)
                + _NL
                + json.dumps({"id": "ord_new", "ts": "t", "request": "미래것",
                              "무슨필드인지모름": 1}, ensure_ascii=False)
                + _NL, encoding="utf-8")
            got = {o.id for o in read_orders()}
            return got == {"ord_old", "ord_new"}
    finally:
        ROOT, ORDERS = saved_root, saved_orders


def _selfcheck() -> int:
    import tempfile
    fails = 0

    def ok(name, cond):
        nonlocal fails
        print(("  ✓ " if cond else "  ✗ ") + name)
        if not cond:
            fails += 1

    import json as _j
    ok("모르는 필드가 있어도 읽는다", _tolerant_read_works())
    ok("단계 순서가 계약대로다",
       STAGE_KEYS == ["interpret", "prior", "exists", "cost", "robust",
                      "pack", "deploy"])
    ok("사람 관문은 해석과 배포 둘",
       HUMAN_GATES == {"interpret", "deploy"})
    ok("존재 다음은 비용", next_stage("exists") == "cost")
    ok("배포 다음은 없다", next_stage("deploy") == "")

    with tempfile.TemporaryDirectory() as td:
        globals()["ROOT"] = Path(td)
        globals()["ORDERS"] = Path(td) / "orders.jsonl"

        o = new_order("이동평균선을 이용한 추세추종 전략 만들어줘")
        ok("주문이 원문을 그대로 보관한다",
           o.request == "이동평균선을 이용한 추세추종 전략 만들어줘")
        ok("첫 단계는 해석", o.stage == "interpret")

        try:
            new_order("   ")
            ok("빈 주문은 거부", False)
        except ValueError:
            ok("빈 주문은 거부", True)

        full = {k: {"v": 1} for k in REQUIRED_SPEC}
        try:
            interpret_done(o.id, {"universe": {"a": 1}}, "부분만")
            ok("빠진 사양은 거부", False)
        except ValueError:
            ok("빠진 사양은 거부", True)

        interpret_done(o.id, full, "이렇게 읽었다")
        cur = latest()[o.id]
        ok("해석 뒤엔 사람 확인 대기", cur.status == "AWAITING_HUMAN")
        ok("해석만으로 다음 단계로 안 간다", cur.stage == "interpret")

        _o, msg = advance(o.id, dry_run=True)
        ok("승인 전엔 진행이 막힌다", "사람 확인 대기" in msg)

        confirm(o.id)
        cur = latest()[o.id]
        ok("승인하면 선행 단계로", cur.stage == "prior")

        stage_done(o.id, "prior", "PASS", numbers={}, finding="기존 측정 없음")
        ok("선행은 숫자 없이도 통과", latest()[o.id].stage == "exists")

        try:
            stage_done(o.id, "exists", "PASS", numbers={}, finding="좋아 보임")
            ok("숫자 없이 존재 통과 불가", False)
        except ValueError:
            ok("숫자 없이 존재 통과 불가", True)

        try:
            stage_done(o.id, "exists", "PASS", numbers={"x": 1}, finding="")
            ok("발견 없이 단계 종료 불가", False)
        except ValueError:
            ok("발견 없이 단계 종료 불가", True)

        stage_done(o.id, "exists", "PASS", numbers={"gross_bp": 40},
                   finding="총엣지 40bp")
        ok("존재 통과하면 비용 단계", latest()[o.id].stage == "cost")

        stage_done(o.id, "cost", "FAIL", numbers={"net_bp": -12},
                   finding="비용 후 -12bp")
        cur = latest()[o.id]
        ok("미달이어도 주문이 안 죽는다", cur.status == "OPEN")
        ok("미달은 CHARACTERIZED 로 표시", cur.verdict == "CHARACTERIZED")
        ok("미달하면 남은 측정을 건너뛴다", cur.stage == "pack")
        ok("어디서 미달인지 남는다", cur.shortfall_at == "cost")

        _o, msg = advance(o.id, dry_run=True)
        ok("미달 팩도 발행된다", "미달 팩" in msg)

        bar = _stage_bar("pack")
        ok("팩 단계가 미달 팩을 지시한다",
           "promotable" in bar and "못 넘었으면" in bar)

        o2 = new_order("측정 불가 시험")
        interpret_done(o2.id, full, "x")
        confirm(o2.id)
        stage_done(o2.id, "prior", "BLOCKED", numbers={},
                   finding="필요한 표가 없다")
        ok("측정 불가는 BLOCKED", latest()[o2.id].status == "BLOCKED")

        o3 = new_order("전 단계 통과 시험")
        interpret_done(o3.id, full, "x")
        confirm(o3.id)
        for st in ("prior", "exists", "cost", "robust"):
            stage_done(o3.id, st, "PASS", numbers={"x": 1}, finding="ok")
        ok("전 단계 통과는 PROMOTABLE",
           latest()[o3.id].verdict == "PROMOTABLE")

        body = interpret_card_body(cur)
        ok("해석 카드가 원문을 싣는다", cur.request in body)
        ok("해석 카드가 여덟 항목을 요구한다",
           all(w in body for w in ("데이터", "유니버스", "신호", "진입",
                                   "청산", "비용", "성공 기준", "반증 기준")))
        ok("해석 카드가 축 선택을 맡긴다",
           "네가 고른다" in body and "market_bars" in body)
        ok("해석 카드가 일봉/장중 혼동을 경고한다",
           "아무 상관이 없다" in body and "검증 불능" in body)
        ok("측정 카드가 데이터 축 변경을 막는다",
           "데이터 축은 사양에 적힌 것을 쓴다" in measure_card_body(cur, "cost"))
        ok("해석 카드가 같은봉 종가진입을 금지한다",
           "다음 봉" in body and "-168.6bp" in body)

        mb = measure_card_body(cur, "cost")
        ok("측정 카드가 사양을 못 바꾸게 한다", "바꾸지 마라" in mb)
        ok("측정 카드가 FAIL 을 정상으로 본다", "FAIL 도 정상적인 결말" in mb)
        ok("비용 단계가 실측 스프레드를 요구한다",
           "실측 스프레드" in mb and "가정값 금지" in mb)
        ok("강건 단계에서만 홀드아웃을 연다",
           "홀드아웃" in _stage_bar("robust")
           and "홀드아웃" not in _stage_bar("exists"))

        txt = render(latest()[o.id])
        ok("보고에 미달 지점이 보인다",
           "비용 에서 기준 미달" in txt and "배포 후보 아님" in txt)

    print("자체점검 통과" if fails == 0 else f"자체점검 실패 {fails}건")
    return fails


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--self-check", action="store_true")
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("order", help="목표를 접수한다")
    a.add_argument("request")

    for name, helptext in (("show", "주문 하나를 본다"),
                           ("confirm", "해석을 승인한다"),
                           ("advance", "다음 단계 카드를 낸다")):
        q = sub.add_parser(name, help=helptext)
        q.add_argument("id")

    r = sub.add_parser("revise", help="해석을 고친다")
    r.add_argument("id")
    r.add_argument("--spec", required=True)

    d = sub.add_parser("interpret-done", help="에이전트: 해석 제출")
    d.add_argument("--id", required=True)
    d.add_argument("--spec", required=True)
    d.add_argument("--rationale", default="")

    s = sub.add_parser("stage-done", help="에이전트: 단계 판정 제출")
    s.add_argument("--id", required=True)
    s.add_argument("--stage", required=True, choices=STAGE_KEYS)
    s.add_argument("--verdict", required=True,
                   choices=["PASS", "FAIL", "BLOCKED"],
                   help="FAIL=기준 미달(팩은 만든다) / BLOCKED=측정 불가")
    s.add_argument("--numbers", default="{}")
    s.add_argument("--finding", required=True)
    s.add_argument("--script", default="")
    s.add_argument("--sessions", action="append", default=[])

    sub.add_parser("status", help="전체 주문 목록")

    a2 = p.parse_args(argv)
    if a2.self_check:
        return _selfcheck()

    if a2.cmd == "order":
        o = new_order(a2.request)
        print(f"접수 {o.id}")
        _o, msg = advance(o.id)
        print("  " + msg)
        return 0
    if a2.cmd == "show":
        o = latest().get(a2.id)
        print(render(o) if o else f"모르는 주문: {a2.id}")
        return 0 if o else 1
    if a2.cmd == "confirm":
        o = confirm(a2.id)
        print(f"해석 승인. 다음 단계: {stage_title(o.stage)}")
        _o, msg = advance(a2.id)
        print("  " + msg)
        return 0
    if a2.cmd == "revise":
        o = revise(a2.id, json.loads(a2.spec))
        print(render(o))
        return 0
    if a2.cmd == "interpret-done":
        o = interpret_done(a2.id, json.loads(a2.spec), a2.rationale)
        print(f"해석 제출됨. 사람 확인 대기: {o.id}")
        return 0
    if a2.cmd == "stage-done":
        o = stage_done(a2.id, a2.stage, a2.verdict,
                       numbers=json.loads(a2.numbers), finding=a2.finding,
                       script=a2.script, sessions=a2.sessions)
        print(f"{stage_title(a2.stage)} {a2.verdict}. 상태: {o.status}")
        if o.status == "OPEN":
            _o, msg = advance(a2.id)
            print("  " + msg)
        return 0
    if a2.cmd == "advance":
        _o, msg = advance(a2.id)
        print(msg)
        return 0
    if a2.cmd == "status":
        cur = latest()
        if not cur:
            print("주문 없음")
            return 0
        for oid in sorted(cur):
            print(render(cur[oid]))
            print()
        return 0
    p.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
