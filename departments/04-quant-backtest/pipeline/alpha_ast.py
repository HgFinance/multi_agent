#!/usr/bin/env python3
"""알파 수식 AST - 에이전트가 **코드가 아니라 수식 트리**를 낸다.

담당: 재일 (퀀트·백테스트본부 QNT)
근거: 재일님 결정 2026-08-14 "에이전트가 금융공학자로서 창의적으로 알파를 만드는
      자기개선 순환" + 같은 날 문헌 조사

▶ 왜 AST 인가 (2026-08-14 문헌 4건)
  지금 실행면 어휘는 완성된 신호 **14종**이고, 기획안은 그중 하나를 고를 수만
  있다. 그래서 "호가 압력이 가격 변화와 같이 움직이면서 스프레드가 좁은 종목"
  같은 가설은 **낼 칸 자체가 없어** 접수에서 반려된다(UNMAPPED_VOCAB).
  실측: 제안 42건 중 가격 밖 근거를 든 것이 2건(4.8%)이었다 - 어휘가 아니라
  **표현 수단이 상상의 경계**였다.

  코드 생성(RD-Agent 식)은 답이 아니다. 실험마다 스크립트가 달라져 input_hash
  가 성립하지 않고, 재현이 무너지면 **좋은 것과 나쁜 것을 못 가린다**(오늘만
  세 번 겪었다 - 추세 필터 미적용·단위 사고·walk-forward 백지).

  문헌이 가리키는 답은 **수식 트리**다:
    · AlphaAgent(KDD'25): AST 를 규제 장치로 쓴다 - ① 기존 알파와의 **AST
      유사도**로 독창성 강제 ② 가설-팩터 의미 정합 ③ **AST 구조 제약**으로
      과설계(=과적합) 방지. CSI 500·S&P 500 에서 decay 저항 우수.
    · QuantaAlpha: 심볼릭 표현으로 **통제성**을 유지하면서 다양성 확보.
    · 하이브리드 LLM-RL: 평균 IC 0.0515(베이스라인 대비 +75%).
    · WorldQuant Alpha101 이 이 표현으로 101개 알파를 전부 적었다.

  즉 **AST 는 창의성 도구인 동시에 규제 도구**다. 재현성과 창의성은 여기서
  트레이드오프가 아니다.

▶ 다중검정은 이미 막아 두었다
  표현을 열면 우연히 좋은 것이 반드시 나온다(Harvey 2016: 새 팩터는 t>=3.0,
  40년간 330개+ 신호가 보고돼 다중검정이 심각하다). 우리는 그 방어를 **먼저**
  지었다 - deflated_sharpe · pbo · trial_pressure · 사전등록 지문 · walk-forward.
  순서를 제대로 밟았으므로 지금이 어휘를 풀 적기다.

▶ 이 모듈이 지키는 것
  ① **결정론** - 같은 (AST, 시장, 기준일)이면 언제나 같은 점수. 종목 순회는
     정렬된 목록을 쓰고, dict 순서에 의존하지 않는다.
  ② **PIT** - `PITView` 만 받는다. 기준일 초과를 꺼내는 경로가 존재하지 않는다.
  ③ **지어내지 않는다** - 값을 못 구한 종목은 **점수를 내지 않는다**. 0 으로
     채우면 "데이터가 없었다" 가 "중립이었다" 로 둔갑해 순위 중앙에 들어온다.
  ④ **지문** - 정규화한 트리의 해시가 곧 이 알파의 정체다. `input_hash` 에
     이것이 들어가면 같은 수식은 같은 실험이 된다.

자체 점검: python departments/04-quant-backtest/pipeline/alpha_ast.py
"""

from __future__ import annotations

import hashlib
import json
import math
import sys
from pathlib import Path

MODULE_VERSION = "quant-alpha-ast-v1"

# ── 입력 필드 ────────────────────────────────────────────────────────────────
#
# 여기 없는 이름은 **거부한다.** 조용히 0 이나 결측으로 흘리면 그 수식이
# 무엇을 읽었는지 원장이 말하지 못한다.
#
# `close`·`notional` 은 일봉, 나머지는 미시구조 일별 집계다. 미시구조를 쓰는
# 수식은 `needs_micro` 가 자동으로 켜진다(`fields_of` 참고) - 가설이 데이터
# 요구를 따로 적을 필요가 없다.
PRICE_FIELDS = ("close", "notional", "returns")
MICRO_FIELDS = ("spread_bps", "depth_imbalance",
                # Legacy column name.  The value is signed *trade* volume /
                # total trade volume, not Cont-Kukanov-Stoikov quote-event OFI
                # (new/cancel/price changes at the best bid and ask).  Keep the
                # name for dataset compatibility, but do not interpret it as
                # full order-book OFI or order-sign persistence.
                "order_flow_imbalance",
                "trade_intensity", "realized_volatility",
                # ▶ 체결 거래대금·수량 (2026-08-14 추가, 데이터셋 v2)
                #   원천(`market.market_ticks`)에는 처음부터 있었는데 집계가
                #   `sum(quantity)` 를 계산해 놓고 버렸고 거래대금은 안 만들었다.
                #   `traded_value` 단위는 **백만원** - 일봉 `notional` 과 같은
                #   눈금이라 같은 문턱을 쓸 수 있다.
                "traded_value", "traded_volume",
                # ▶ **일중 구간 피처** (2026-08-14 추가, 데이터셋 v3)
                #   위 필드들은 하루를 **하나로 평균**한 값이다. 오전에 팔고
                #   오후에 산 종목은 OFI 가 중립으로 찍혀 정보가 상쇄된다.
                #   실측(2026-08-13, 2,420종목): 마감 30분 OFI 표준편차 0.4424 vs
                #   하루 전체 0.2538(1.74배), 둘의 상관 0.3713 - **서로 다른
                #   것을 잰다.** 일별 OFI 첫 실험이 IC +0.012(t 0.79)로 죽은 것이
                #   이 압축 때문이다.
                #
                #   여전히 (종목, 일자) 한 행이라 실행면은 그대로다 - 일중에
                #   *거래*하는 것은 별개 작업이고 여기 포함되지 않는다.
                "ofi_close",          # 14:50~15:30 주문흐름불균형(t+1 시가 체결과 시점 일치)
                "ofi_open",           # 09:00~09:30
                "ofi_intraday_std",   # 4구간 OFI 의 표준편차(방향이 오락가락한 정도)
                "close_vs_vwap",      # 종가/VWAP - 1
                "spread_close_ratio")  # 마감 스프레드 / 개장 스프레드
FIELDS = PRICE_FIELDS + MICRO_FIELDS

# ── 연산자 ───────────────────────────────────────────────────────────────────
#
# 세 부류다. 부류를 섞지 않는 것이 평가기를 단순하게 만든다:
#   소스(ts_*)  : 필드 시계열 -> 종목별 스칼라   (창 n 을 받는다)
#   변환        : 종목별 스칼라 -> 종목별 스칼라
#   결합        : 종목별 스칼라 둘 -> 하나
SOURCE_OPS = ("ts_last", "ts_mean", "ts_std", "ts_sum", "ts_delta",
              "ts_return", "ts_max", "ts_min", "ts_rank", "ts_corr")
UNARY_OPS = ("neg", "abs", "log", "sign", "sqrt", "rank", "zscore")
BINARY_OPS = ("add", "sub", "mul", "div")
ALL_OPS = SOURCE_OPS + UNARY_OPS + BINARY_OPS

# 창 길이 한도. 1일은 시계열이 아니고, 250일 초과는 표본이 받치지 못한다.
MIN_WINDOW, MAX_WINDOW = 1, 250
# 복잡도 상한 (AlphaAgent 의 구조 제약). 깊고 큰 트리는 과적합으로 간다.
MAX_DEPTH, MAX_NODES = 6, 40

# Research intake runs in a smaller image.  Fail closed if its shared grammar surface
# ever drifts from this evaluator's executable catalog.
_CONTRACTS = (Path(__file__).resolve().parents[3] / "departments" /
              "01-research" / "contracts")
sys.path.insert(0, str(_CONTRACTS))
from alpha_ast_surface import (  # noqa: E402
    ALL_OPS as CONTRACT_ALL_OPS,
    FIELDS as CONTRACT_FIELDS,
    MAX_DEPTH as CONTRACT_MAX_DEPTH,
    MAX_NODES as CONTRACT_MAX_NODES,
)
if (FIELDS, ALL_OPS, MAX_DEPTH, MAX_NODES) != (
        CONTRACT_FIELDS, CONTRACT_ALL_OPS, CONTRACT_MAX_DEPTH, CONTRACT_MAX_NODES):
    raise RuntimeError("research/quant alpha AST surface drift")


class AlphaExprError(ValueError):
    """수식이 성립하지 않는다. **돌리기 전에** 던진다."""


# ── 검증 ─────────────────────────────────────────────────────────────────────

def validate(node, *, _depth: int = 1) -> dict:
    """수식 트리를 검사하고 정규화된 사본을 돌려준다.

    검사를 실행 시점이 아니라 **접수 시점**에 두는 이유: 잘못된 수식이 실험을
    만들어 놓고 중간에 죽으면 원장에 반쪽짜리 흔적이 남는다. 여기서 막으면
    애초에 안 만들어진다.
    """
    if _depth > MAX_DEPTH:
        raise AlphaExprError(f"수식 깊이가 {MAX_DEPTH} 를 넘는다 - 과설계는 "
                             f"과적합으로 간다(AlphaAgent 구조 제약)")
    if not isinstance(node, dict):
        raise AlphaExprError(f"노드는 dict 여야 한다: {node!r}")

    if "const" in node:
        v = node["const"]
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            raise AlphaExprError(f"상수가 수가 아니다: {v!r}")
        if not math.isfinite(float(v)):
            raise AlphaExprError(f"상수가 유한하지 않다: {v!r}")
        return {"const": float(v)}

    op = node.get("op")
    if op not in ALL_OPS:
        raise AlphaExprError(
            f"모르는 연산자: {op!r} - 사용 가능: {sorted(ALL_OPS)}")

    if op in SOURCE_OPS:
        return _validate_source(node, op)

    if op in UNARY_OPS:
        arg = node.get("arg")
        if arg is None:
            raise AlphaExprError(f"{op} 는 인자 하나가 필요하다")
        return {"op": op, "arg": validate(arg, _depth=_depth + 1)}

    args = node.get("args")
    if not isinstance(args, (list, tuple)) or len(args) != 2:
        raise AlphaExprError(f"{op} 는 인자 둘이 필요하다: {args!r}")
    return {"op": op, "args": [validate(a, _depth=_depth + 1) for a in args]}


def _validate_source(node: dict, op: str) -> dict:
    """소스 연산자 - 필드와 창을 검사한다."""
    if op == "ts_corr":
        fa, fb = node.get("field_a"), node.get("field_b")
        for f in (fa, fb):
            if f not in FIELDS:
                raise AlphaExprError(
                    f"모르는 필드: {f!r} - 사용 가능: {sorted(FIELDS)}")
        n = _window(node, minimum=3)   # 상관은 3점 미만이면 정의되지 않는다
        if fa == fb:
            raise AlphaExprError("같은 필드끼리의 상관은 항상 1 이다 - 신호가 아니다")
        return {"op": op, "field_a": fa, "field_b": fb, "n": n}

    field = node.get("field")
    if field not in FIELDS:
        raise AlphaExprError(
            f"모르는 필드: {field!r} - 사용 가능: {sorted(FIELDS)}")
    minimum = 2 if op in ("ts_std", "ts_delta", "ts_return", "ts_rank") else 1
    return {"op": op, "field": field, "n": _window(node, minimum=minimum)}


def _window(node: dict, *, minimum: int) -> int:
    raw = node.get("n", 1)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        raise AlphaExprError(f"창 n 이 정수가 아니다: {raw!r}") from None
    if not (MIN_WINDOW <= n <= MAX_WINDOW) or n < minimum:
        raise AlphaExprError(
            f"창 n={n} 이 허용 범위 밖이다 [{max(MIN_WINDOW, minimum)}, "
            f"{MAX_WINDOW}] - 자르지 않고 거부한다")
    return n


def count_nodes(node: dict) -> int:
    """노드 수 - 복잡도 상한을 재는 자."""
    if "const" in node:
        return 1
    if node.get("op") in SOURCE_OPS:
        return 1
    if "arg" in node:
        return 1 + count_nodes(node["arg"])
    return 1 + sum(count_nodes(a) for a in node.get("args", ()))


def depth_of(node: dict) -> int:
    if "const" in node or node.get("op") in SOURCE_OPS:
        return 1
    if "arg" in node:
        return 1 + depth_of(node["arg"])
    return 1 + max(depth_of(a) for a in node.get("args", ()))


def parse(node) -> dict:
    """검증 + 복잡도 상한. **접수 관문이 부르는 입구다.**"""
    expr = validate(node)
    n = count_nodes(expr)
    if n > MAX_NODES:
        raise AlphaExprError(f"노드 {n}개 > 상한 {MAX_NODES} - 과설계는 "
                             f"표본이 아니라 잡음을 학습한다")
    return expr


# ── 지문 ─────────────────────────────────────────────────────────────────────

def _canonical(node: dict) -> dict:
    """교환법칙을 정규화한다 - `a+b` 와 `b+a` 는 **같은 알파**다.

    정규화하지 않으면 같은 수식이 다른 지문을 받아 중복 실험이 새 실험으로
    등록되고, 시도 횟수(다중검정 보정의 분모)가 틀어진다.
    """
    if "const" in node or node.get("op") in SOURCE_OPS:
        return node
    if "arg" in node:
        return {"op": node["op"], "arg": _canonical(node["arg"])}
    args = [_canonical(a) for a in node["args"]]
    if node["op"] in ("add", "mul"):     # 교환 가능한 것만 정렬한다
        args.sort(key=lambda a: json.dumps(a, sort_keys=True))
    return {"op": node["op"], "args": args}


def fingerprint(expr: dict) -> str:
    """정규화 트리의 해시. **이것이 알파의 정체다.**"""
    payload = json.dumps(_canonical(expr), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def fields_of(expr: dict) -> set:
    """수식이 읽는 필드 집합 - 데이터 요구를 **수식이 스스로 말한다.**"""
    if "const" in expr:
        return set()
    op = expr.get("op")
    if op == "ts_corr":
        return {expr["field_a"], expr["field_b"]}
    if op in SOURCE_OPS:
        return {expr["field"]}
    if "arg" in expr:
        return fields_of(expr["arg"])
    out: set = set()
    for a in expr.get("args", ()):
        out |= fields_of(a)
    return out


def needs_micro(expr: dict) -> bool:
    """미시구조를 읽는가. 실행면이 이 값을 보고 데이터셋을 더 싣는다."""
    return bool(fields_of(expr) & set(MICRO_FIELDS))


def min_history(expr: dict) -> int:
    """이 수식이 요구하는 최소 이력(거래일). 웜업이 이 값을 따른다."""
    if "const" in expr:
        return 1
    op = expr.get("op")
    if op in SOURCE_OPS:
        return int(expr.get("n", 1))
    if "arg" in expr:
        return min_history(expr["arg"])
    return max(min_history(a) for a in expr.get("args", ()))


# ── 평가 ─────────────────────────────────────────────────────────────────────

def _series(view, symbol: str, field: str, n: int) -> list:
    """필드 시계열. **PITView 만 통한다** - 기준일 초과는 꺼낼 경로가 없다."""
    if field == "close":
        return view.closes(symbol, n)
    if field == "notional":
        return view.notionals(symbol, n)
    if field == "returns":
        return view.daily_returns(symbol, n)
    return view.micro(symbol, field, n)


def _agg(op: str, xs: list) -> float | None:
    """시계열 -> 스칼라. **못 구하면 None** (0 으로 채우지 않는다)."""
    if not xs:
        return None
    if op == "ts_last":
        return xs[-1]
    if op == "ts_mean":
        return sum(xs) / len(xs)
    if op == "ts_sum":
        return sum(xs)
    if op == "ts_max":
        return max(xs)
    if op == "ts_min":
        return min(xs)
    if op == "ts_std":
        if len(xs) < 2:
            return None
        mu = sum(xs) / len(xs)
        return math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))
    if op == "ts_delta":
        return xs[-1] - xs[0] if len(xs) >= 2 else None
    if op == "ts_return":
        if len(xs) < 2 or xs[0] == 0:
            return None
        return xs[-1] / xs[0] - 1.0
    if op == "ts_rank":
        # 마지막 값이 창 안에서 몇 분위인가 [0,1]. 동점은 평균 순위.
        if len(xs) < 2:
            return None
        last = xs[-1]
        less = sum(1 for x in xs if x < last)
        same = sum(1 for x in xs if x == last)
        return (less + (same - 1) / 2.0) / (len(xs) - 1)
    return None


def _cross_rank(vals: dict) -> dict:
    """횡단면 백분위 [0,1]. 동점은 평균 순위 - 결정론을 위해 정렬을 고정한다."""
    if not vals:
        return {}
    items = sorted(vals.items(), key=lambda kv: (kv[1], kv[0]))
    n = len(items)
    if n == 1:
        return {items[0][0]: 0.5}
    out, i = {}, 0
    while i < n:
        j = i
        while j + 1 < n and items[j + 1][1] == items[i][1]:
            j += 1
        avg = (i + j) / 2.0 / (n - 1)
        for k in range(i, j + 1):
            out[items[k][0]] = avg
        i = j + 1
    return out


def _zscore(vals: dict) -> dict:
    if len(vals) < 2:
        return {}
    xs = list(vals.values())
    mu = sum(xs) / len(xs)
    sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))
    if sd <= 0:
        return {}                     # 산포가 0 이면 순위가 없다 - 안 만든다
    return {k: (v - mu) / sd for k, v in vals.items()}


def evaluate(expr: dict, view, symbols=None) -> dict:
    """수식 -> 종목별 점수. **값을 못 구한 종목은 빠진다.**

    반환은 `{종목: 점수}` 이고 `signal_scores` 와 같은 계약이라 기존 실행면이
    그대로 받는다.
    """
    syms = sorted(symbols if symbols is not None else view.symbols)
    return _eval(expr, view, syms)


def _eval(expr: dict, view, syms: list) -> dict:
    if "const" in expr:
        c = float(expr["const"])
        return {s: c for s in syms}

    op = expr["op"]

    if op == "ts_corr":
        n = int(expr["n"])
        out = {}
        for s in syms:
            xs = _series(view, s, expr["field_a"], n)
            ys = _series(view, s, expr["field_b"], n)
            m = min(len(xs), len(ys))
            if m < 3:
                continue          # 표본이 모자라면 그 종목은 점수가 없다
            v = _pearson(xs[-m:], ys[-m:])
            if v is not None:
                out[s] = v
        return out

    if op in SOURCE_OPS:
        n = int(expr["n"])
        field = expr["field"]
        out = {}
        for s in syms:
            v = _agg(op, _series(view, s, field, n))
            if v is not None and math.isfinite(v):
                out[s] = float(v)
        return out

    if op in UNARY_OPS:
        inner = _eval(expr["arg"], view, syms)
        if op == "rank":
            return _cross_rank(inner)
        if op == "zscore":
            return _zscore(inner)
        out = {}
        for s, v in inner.items():
            try:
                if op == "neg":
                    r = -v
                elif op == "abs":
                    r = abs(v)
                elif op == "sign":
                    r = float((v > 0) - (v < 0))
                elif op == "log":
                    if v <= 0:
                        continue      # 정의역 밖 - 지어내지 않는다
                    r = math.log(v)
                else:                 # sqrt
                    if v < 0:
                        continue
                    r = math.sqrt(v)
            except (ValueError, OverflowError):
                continue
            if math.isfinite(r):
                out[s] = r
        return out

    a = _eval(expr["args"][0], view, syms)
    b = _eval(expr["args"][1], view, syms)
    out = {}
    for s in sorted(set(a) & set(b)):     # **교집합만** - 한쪽이 없으면 점수 없음
        x, y = a[s], b[s]
        if op == "add":
            r = x + y
        elif op == "sub":
            r = x - y
        elif op == "mul":
            r = x * y
        else:
            if y == 0:
                continue                  # 0 나눗셈은 무한이 아니라 미산출이다
            r = x / y
        if math.isfinite(r):
            out[s] = r
    return out


def _pearson(xs: list, ys: list) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((a - mx) * (b - my) for a, b in zip(xs, ys))
    sxx = sum((a - mx) ** 2 for a in xs)
    syy = sum((b - my) ** 2 for b in ys)
    if sxx <= 0 or syy <= 0:
        return None                       # 한쪽이 상수면 상관이 정의되지 않는다
    return sxy / math.sqrt(sxx * syy)


# ── 유사도 (AlphaAgent 독창성 강제) ──────────────────────────────────────────

def _node_bag(expr: dict, prefix: str = "") -> list:
    """트리를 라벨 다중집합으로 편다 - 유사도의 재료."""
    if "const" in expr:
        return [f"{prefix}const"]
    op = expr["op"]
    if op == "ts_corr":
        return [f"{prefix}{op}({expr['field_a']},{expr['field_b']},{expr['n']})"]
    if op in SOURCE_OPS:
        return [f"{prefix}{op}({expr['field']},{expr['n']})"]
    if "arg" in expr:
        return [f"{prefix}{op}"] + _node_bag(expr["arg"], prefix)
    out = [f"{prefix}{op}"]
    for a in expr["args"]:
        out += _node_bag(a, prefix)
    return out


def similarity(a: dict, b: dict) -> float:
    """두 알파의 구조 유사도 [0,1]. **중복 알파를 걸러내는 자.**

    ▶ 왜 필요한가 (2026-08-14 실측 + AlphaAgent)
      서가에 신호 9종이 있는데 실질은 2.5개였다 - 서로 다른 이름으로 같은 것을
      재고 있었다. 지금은 중복을 **잴 방법이 아예 없어서** 같은 것을 여러 번
      시험한다. 문헌도 같은 것을 지목한다: LLM 기반 알파 마이닝의 실패 원인이
      동질화(homogenization)이고, AlphaAgent 는 AST 유사도로 그것을 막는다.

    자카드 유사도를 쓴다 - 노드 라벨(연산자·필드·창)의 다중집합 교집합 비율.
    창 길이만 다른 수식(20일 vs 21일)은 높은 유사도를 받아 걸러진다.
    """
    ba, bb = _node_bag(_canonical(a)), _node_bag(_canonical(b))
    if not ba or not bb:
        return 0.0
    from collections import Counter

    ca, cb = Counter(ba), Counter(bb)
    inter = sum((ca & cb).values())
    union = sum((ca | cb).values())
    return inter / union if union else 0.0


# ── 가설-팩터 정합 (결정론 부분) ────────────────────────────────────────────
#
# ▶ 무엇을 막나
#   "경제 논리는 그럴듯한데 수식은 딴 것" - AlphaAgent 가 **의미 표류**라
#   부르는 실패다. 논리가 매수 압력을 말하는데 수식은 종가만 읽으면, 그
#   실험이 무엇을 검증했는지 아무도 모른다. 결과가 좋아도 그 논리의 증거가
#   아니다.
#
# ▶ 왜 LLM 이 아니라 결정론인가 (2026-08-14 문헌)
#   LLM 자기검증은 **성능을 떨어뜨린다**("대부분 도메인에서 self-critique 는
#   성능 붕괴, 건전한 외부 검증은 성능 향상" - 검증자로 쓸 때 위양성률이
#   높다). 자기선호 편향도 측정돼 있다(데이터셋에 따라 -38%~+90%, 원인은
#   perplexity·친숙도) - 리서치가 자기 수식을 스스로 채점하면 통과율만 오른다.
#
#   반면 문헌이 드는 좋은 검증자는 **심볼릭 라이브러리 같은 결정론**이다.
#   그래서 여기서는 기계가 확실히 아는 것만 본다: **수식이 읽는 필드가 논리에
#   등장하는가.** 미묘한 의미 일치는 판단하지 않는다 - 못 하는 것을 하는
#   척하면 멀쩡한 가설을 잘못 죽인다.
#
# ▶ 어휘는 넉넉히
#   좁게 잡으면 표현만 다른 정상 가설을 막는다. 한국어·영어를 함께 두고,
#   **전부 미언급일 때만** 불일치로 본다(일부 미언급은 경고).
FIELD_CONCEPTS = {
    "close": ("가격", "종가", "주가", "수익률", "상승", "하락", "모멘텀", "추세",
              "반전", "price", "return", "momentum", "trend", "reversal"),
    "notional": ("거래대금", "유동성", "거래량", "회전", "규모", "liquidity",
                 "volume", "turnover", "adv", "illiquid"),
    "returns": ("수익률", "등락", "변동", "모멘텀", "반전", "return",
                "momentum", "reversal", "volatility"),
    "spread_bps": ("스프레드", "호가", "유동성", "거래비용", "슬리피지", "체결비용",
                   "spread", "liquidity", "cost", "slippage", "bid", "ask"),
    "depth_imbalance": ("호가", "잔량", "깊이", "불균형", "매수", "매도", "수급",
                        "depth", "imbalance", "book", "quote"),
    "order_flow_imbalance": ("체결", "체결흐름", "매수", "매도", "수급",
                             "signed trade", "trade flow", "ofi", "imbalance",
                             "pressure", "flow"),
    "trade_intensity": ("체결", "거래빈도", "강도", "빈도", "활발", "intensity",
                        "trade", "frequency", "activity"),
    "realized_volatility": ("변동성", "실현변동성", "리스크", "위험",
                            "volatility", "risk", "vol"),
    "traded_value": ("거래대금", "대금", "유동성", "거래량", "규모", "회전",
                     "급증", "liquidity", "value", "turnover", "adv", "notional"),
    "traded_volume": ("거래량", "체결량", "수량", "물량", "급증",
                      "volume", "quantity", "size"),
    # 일중 구간 피처. 어휘를 넉넉히 둔다 - 좁게 잡으면 표현만 다른 정상
    # 가설을 막는다(마감 압력을 "종가 무렵", "장 후반" 이라 쓸 수 있다).
    "ofi_close": ("마감", "종가", "장 후반", "후반", "막판", "매수압력", "압력",
                  "주문", "수급", "close", "closing", "eod", "late", "pressure",
                  "order flow", "imbalance"),
    "ofi_open": ("개장", "시가", "장 초반", "초반", "아침", "갭", "밤사이",
                 "매수압력", "압력", "주문", "수급", "open", "opening",
                 "morning", "gap", "overnight", "order flow", "imbalance"),
    "ofi_intraday_std": ("일중", "변동", "분산", "오락", "일관", "지속", "안정",
                         "주문", "수급", "intraday", "dispersion", "consistency",
                         "persistence", "variability", "order flow"),
    "close_vs_vwap": ("vwap", "종가", "평균단가", "매수압력", "압력", "마감",
                      "close", "average price", "pressure"),
    "spread_close_ratio": ("스프레드", "호가", "유동성", "거래비용", "수축",
                           "일중", "spread", "liquidity", "cost", "intraday"),
}


def check_alignment(expr: dict, rationale: str) -> dict:
    """수식이 읽는 필드가 경제 논리에 등장하는가. **결정론이다.**

    반환: {fields, unmentioned, ok, note}
      ok=False 는 **읽는 필드가 논리에 하나도 안 나온다**는 뜻이다 - 둘 중
      하나는 잘못 쓴 것이므로 접수에서 되돌린다. 일부만 빠진 것은 경고다
      (표현이 다를 수 있고, 좁게 잡으면 멀쩡한 가설을 죽인다).
    """
    fields = sorted(fields_of(expr))
    text = str(rationale or "").lower()
    if not fields:
        return {"fields": [], "unmentioned": [], "ok": True,
                "note": "필드를 읽지 않는 수식(상수) - 대조할 것이 없다"}
    if not text.strip():
        return {"fields": fields, "unmentioned": fields, "ok": False,
                "note": "경제 논리가 비어 있다 - 무엇을 검증하는지 알 수 없다"}
    unmentioned = [f for f in fields
                   if not any(c in text for c in FIELD_CONCEPTS.get(f, (f,)))]
    ok = len(unmentioned) < len(fields)
    if ok and unmentioned:
        note = (f"논리에 안 나오는 필드: {unmentioned} - 표현이 다를 수 있어 "
                f"막지 않지만, 그 필드를 왜 읽는지 논리에 적는 편이 좋다")
    elif ok:
        note = "수식이 읽는 필드가 모두 논리에 등장한다"
    else:
        note = (f"수식이 읽는 필드 {fields} 중 **하나도** 논리에 나오지 않는다 - "
                f"논리와 수식이 다른 이야기이거나 둘 중 하나를 잘못 적었다")
    return {"fields": fields, "unmentioned": unmentioned, "ok": ok, "note": note}


# ── 자체 점검 ────────────────────────────────────────────────────────────────

class _FakeView:
    """PITView 최소 대역. 실물과 **같은 메서드 이름**을 쓴다."""

    def __init__(self, closes: dict, micro: dict | None = None):
        self._c = closes                       # {종목: [오래된 -> 최신]}
        self._m = micro or {}

    @property
    def symbols(self):
        return sorted(self._c)

    def closes(self, s, n):
        return list(self._c.get(s, []))[-n:]

    def notionals(self, s, n):
        return [1e3] * min(n, len(self._c.get(s, [])))

    def daily_returns(self, s, n):
        px = self._c.get(s, [])
        rets = [px[i] / px[i - 1] - 1.0 for i in range(1, len(px)) if px[i - 1]]
        return rets[-n:]

    def micro(self, s, feature, n=1):
        return list(self._m.get((s, feature), []))[-n:]


def _check_validate_rejects_nonsense():
    """**모르는 것을 추측해서 돌리지 않는다.**"""
    for bad in ({"op": "magic", "field": "close", "n": 5},
                {"op": "ts_mean", "field": "pe_ratio", "n": 5},
                {"op": "ts_mean", "field": "close", "n": 0},
                {"op": "ts_mean", "field": "close", "n": 9999},
                {"op": "ts_std", "field": "close", "n": 1},
                {"op": "add", "args": [{"const": 1}]},
                {"const": "x"},
                {"op": "ts_corr", "field_a": "close", "field_b": "close", "n": 5}):
        try:
            parse(bad)
        except AlphaExprError:
            pass
        else:
            raise AssertionError(f"말이 안 되는 수식이 통과했다: {bad}")
    # 깊이·노드 상한
    deep = {"const": 1.0}
    for _ in range(MAX_DEPTH + 2):
        deep = {"op": "neg", "arg": deep}
    try:
        parse(deep)
    except AlphaExprError as e:
        assert "깊이" in str(e), e
    else:
        raise AssertionError("깊이 상한이 안 걸렸다")
    print("  말 안 되는 수식 거부      OK")


def _check_fingerprint_is_canonical():
    """`a+b` 와 `b+a` 는 **같은 알파**다 - 아니면 시도 횟수가 틀어진다."""
    a = parse({"op": "add", "args": [{"op": "ts_mean", "field": "close", "n": 5},
                                     {"const": 2.0}]})
    b = parse({"op": "add", "args": [{"const": 2.0},
                                     {"op": "ts_mean", "field": "close", "n": 5}]})
    assert fingerprint(a) == fingerprint(b), "교환법칙 정규화가 안 됐다"
    # 뺄셈은 교환 불가 - 정규화하면 안 된다
    c = parse({"op": "sub", "args": [{"op": "ts_mean", "field": "close", "n": 5},
                                     {"const": 2.0}]})
    d = parse({"op": "sub", "args": [{"const": 2.0},
                                     {"op": "ts_mean", "field": "close", "n": 5}]})
    assert fingerprint(c) != fingerprint(d), "교환 불가 연산을 정렬해 버렸다"
    # 창 하나만 달라도 다른 알파다
    e = parse({"op": "ts_mean", "field": "close", "n": 6})
    assert fingerprint(e) != fingerprint(parse({"op": "ts_mean", "field": "close", "n": 5}))
    print("  지문 정규화(a+b=b+a)     OK")


def _check_declares_its_own_requirements():
    """**수식이 자기 데이터 요구를 말한다** - 가설이 따로 적지 않아도 된다."""
    px = parse({"op": "ts_return", "field": "close", "n": 20})
    assert fields_of(px) == {"close"} and not needs_micro(px)
    assert min_history(px) == 20

    mix = parse({"op": "sub", "args": [
        {"op": "rank", "arg": {"op": "ts_mean", "field": "order_flow_imbalance", "n": 3}},
        {"op": "rank", "arg": {"op": "ts_mean", "field": "spread_bps", "n": 10}}]})
    assert needs_micro(mix), "미시구조를 읽는데 요구를 안 말했다"
    assert min_history(mix) == 10, min_history(mix)
    print("  수식이 요구를 선언한다    OK")


def _check_momentum_is_reproduced_exactly():
    """**기존 momentum 을 AST 로 재현한다** - 값이 같아야 그 위를 믿는다.

    이것이 이 모듈의 수용 기준이다. 새 표현이 옛 결과를 못 맞히면, 그 위에서
    나오는 어떤 성적도 옛것과 비교할 수 없다.
    """
    import strategy_templates as ST

    closes = {"A": [100.0 + i for i in range(40)],
              "B": [200.0 - i * 0.5 for i in range(40)],
              "C": [50.0 + (i % 7) for i in range(40)]}
    view = _FakeView(closes)
    lookback = 20

    # 실행면 momentum: total_return(symbol, lookback)
    want = {}
    for s in view.symbols:
        px = view.closes(s, lookback + 1)
        if len(px) >= lookback + 1 and px[0] > 0:
            want[s] = px[-1] / px[0] - 1.0

    got = evaluate(parse({"op": "ts_return", "field": "close",
                          "n": lookback + 1}), view)
    assert set(got) == set(want), (sorted(got), sorted(want))
    for s in want:
        assert abs(got[s] - want[s]) < 1e-12, (s, got[s], want[s])
    # 순위도 같아야 한다 - 백테스트가 보는 것은 순위다
    assert sorted(got, key=got.get) == sorted(want, key=want.get)
    assert "momentum" in ST.EDGE_VOCAB, "대조군 어휘가 사라졌다"
    print("  momentum 재현(값 동일)    OK")


def _check_missing_is_not_invented():
    """**값이 없으면 점수를 내지 않는다** - 0 은 '중립' 이 아니라 거짓말이다."""
    closes = {"A": [10.0] * 30, "B": [20.0] * 30}
    micro = {("A", "order_flow_imbalance"): [0.3] * 30}   # B 는 미시구조가 없다
    view = _FakeView(closes, micro)
    got = evaluate(parse({"op": "ts_mean", "field": "order_flow_imbalance",
                          "n": 5}), view)
    assert set(got) == {"A"}, got

    # 결합에서도 한쪽이 없으면 빠진다(교집합)
    mixed = parse({"op": "add", "args": [
        {"op": "ts_mean", "field": "order_flow_imbalance", "n": 5},
        {"op": "ts_mean", "field": "close", "n": 5}]})
    assert set(evaluate(mixed, view)) == {"A"}, evaluate(mixed, view)

    # 0 나눗셈·정의역 밖은 미산출
    zero = parse({"op": "div", "args": [{"op": "ts_last", "field": "close", "n": 1},
                                        {"const": 0.0}]})
    assert evaluate(zero, view) == {}
    neg = parse({"op": "log", "arg": {"const": -1.0}})
    assert evaluate(neg, view) == {}
    print("  없는 값 = 미산출          OK")


def _check_deterministic_and_pit():
    """같은 입력이면 같은 점수. 그리고 **기준일 밖을 꺼낼 경로가 없다.**"""
    closes = {f"S{i}": [100.0 + i + j * 0.1 for j in range(30)] for i in range(5)}
    view = _FakeView(closes)
    expr = parse({"op": "rank", "arg": {"op": "ts_return", "field": "close", "n": 10}})
    first = evaluate(expr, view)
    for _ in range(3):
        assert evaluate(expr, view) == first, "같은 입력에 다른 점수가 나왔다"
    # 순위는 [0,1]
    assert all(0.0 <= v <= 1.0 for v in first.values()), first
    # 평가기가 PITView 메서드 말고 다른 경로로 데이터를 안 만진다
    import ast as _ast
    import pathlib
    src = pathlib.Path(__file__).read_text(encoding="utf-8")
    body = _ast.get_source_segment(src, next(
        n for n in _ast.walk(_ast.parse(src))
        if isinstance(n, _ast.FunctionDef) and n.name == "_series")) or ""
    for allowed in ("view.closes", "view.notionals", "view.daily_returns",
                    "view.micro"):
        assert allowed in body, allowed
    assert "market" not in body.lower(), \
        "평가기가 Market 을 직접 만진다 - PITView 를 우회하면 PIT 보장이 깨진다"
    print("  결정론 + PIT 경로 고정    OK")


def _check_alignment_is_deterministic_and_forgiving():
    """**논리와 수식이 다른 이야기면 잡되, 표현 차이로 죽이지 않는다.**

    문헌(2026-08-14): LLM 자기검증은 성능을 떨어뜨리고 자기선호 편향이
    -38%~+90% 다. 그래서 여기서는 기계가 확실히 아는 것만 본다 - "읽는 필드가
    논리에 나오는가". 미묘한 의미 일치는 판단하지 않는다.
    """
    ofi = parse({"op": "ts_mean", "field": "order_flow_imbalance", "n": 3})

    # ① 논리와 수식이 같은 이야기 - 통과
    good = check_alignment(ofi, "호가 매수 압력이 이후 수익을 예측한다")
    assert good["ok"] and not good["unmentioned"], good

    # ② 완전히 다른 이야기 - 잡는다
    bad = check_alignment(ofi, "저PBR 종목이 장기적으로 초과수익을 낸다")
    assert not bad["ok"], bad
    assert "하나도" in bad["note"], bad

    # ③ 일부만 빠진 것은 **막지 않는다**(표현이 다를 수 있다)
    mixed = parse({"op": "sub", "args": [
        {"op": "rank", "arg": {"op": "ts_mean",
                               "field": "order_flow_imbalance", "n": 3}},
        {"op": "rank", "arg": {"op": "ts_mean", "field": "spread_bps", "n": 10}}]})
    partial = check_alignment(mixed, "매수 압력이 크면 이후 오른다")
    assert partial["ok"], partial
    assert "spread_bps" in partial["unmentioned"], partial

    # ④ 영어 논리도 통한다 - 한국어만 보면 멀쩡한 가설을 죽인다
    eng = check_alignment(ofi, "order flow imbalance predicts short-term returns")
    assert eng["ok"], eng

    # ⑤ 논리가 비어 있으면 통과시키지 않는다
    assert not check_alignment(ofi, "")["ok"]
    assert not check_alignment(ofi, None)["ok"]

    # ⑥ 상수 수식은 대조할 것이 없다 - 억지로 막지 않는다
    assert check_alignment(parse({"const": 1.0}), "무엇이든")["ok"]

    # ⑦ **결정론** - 같은 입력이면 같은 판정(LLM 이 개입하지 않는다)
    assert check_alignment(ofi, "매수 압력") == check_alignment(ofi, "매수 압력")
    print("  가설-팩터 정합(결정론)    OK")


def _check_similarity_catches_duplicates():
    """**중복 알파를 잡는다** - 서가 9종이 실질 2.5개였던 실측의 처방."""
    a = parse({"op": "ts_mean", "field": "close", "n": 20})
    b = parse({"op": "ts_mean", "field": "close", "n": 21})   # 창만 다르다
    c = parse({"op": "ts_mean", "field": "spread_bps", "n": 20})
    assert similarity(a, a) == 1.0
    assert similarity(a, b) < 1.0, "창만 다른데 완전히 같다고 했다"
    assert similarity(a, c) < similarity(a, a)
    # 구조가 겹칠수록 높다
    d = parse({"op": "rank", "arg": {"op": "ts_mean", "field": "close", "n": 20}})
    assert similarity(a, d) > similarity(c, d), (similarity(a, d), similarity(c, d))
    print("  중복 알파 유사도          OK")


if __name__ == "__main__":
    import sys as _sys
    if hasattr(_sys.stdout, "reconfigure"):
        _sys.stdout.reconfigure(encoding="utf-8")
    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_validate_rejects_nonsense()
    _check_fingerprint_is_canonical()
    _check_declares_its_own_requirements()
    _check_momentum_is_reproduced_exactly()
    _check_missing_is_not_invented()
    _check_deterministic_and_pit()
    _check_similarity_catches_duplicates()
    _check_alignment_is_deterministic_and_forgiving()
    print(f"알파 AST 8개 영역 통과. 연산자 {len(ALL_OPS)}종 · 필드 {len(FIELDS)}종")
