#!/usr/bin/env python3
"""데이터셋 정제소 - **계약을 받아 실험용 판(panel)을 굳힌다.**

소유: 재일 (퀀트·백테스트본부 QNT)
근거: 2026-08-14 설계 결정 "정제 방법을 정하는 AI + 결정론적으로 실행하는 공장"

▶ 이 파일의 자리
  `dataset_contract` 가 **무엇을 할지** 정하고, 여기가 **그대로 한다.**
  판단은 위에서 끝났으므로 여기에는 조건 분기와 산술만 있다 - 이 파일에
  "적당히" 가 들어가는 순간 실험마다 정제가 달라진다.

      리서치 → 계약(dataset_contract) → **정제소(여기)** → 검증 → 아티팩트 → 퀀트

▶ 순수 함수로 두는 이유
  DB 도 파일도 만지지 않는다. 행 목록을 받아 행 목록을 돌려준다. 그래야
  자체 점검이 DB 없이 돌고, 같은 입력이 언제나 같은 판을 낸다 - 재현성은
  나중에 붙이는 것이 아니라 이 성질에서 나온다.

▶ **버린 행은 반드시 센다** (2026-08-13 교훈)
  이관에서 중복 접힘 4.8% 와 inner join 이 행을 조용히 버린 적이 있고,
  행수 대조가 없어서 안 보였다. 그래서 모든 단계가 `StepResult` 로
  들어온 행·나간 행·버린 이유를 남긴다. **합계가 안 맞으면 승인하지 않는다.**

자체 점검: python departments/04-quant-backtest/pipeline/dataset_refinery.py
"""

from __future__ import annotations

import hashlib
import sys
from dataclasses import dataclass, field

from dataset_contract import DatasetContract, validate  # noqa: E402

MODULE_VERSION = "quant-dataset-refinery-v1"

# 정수배 판정. `strategy_templates._adjustment_break` 와 같은 기준을 쓴다 -
# 두 곳이 다른 기준을 쓰면 자르는 곳과 고치는 곳이 어긋난다.
SPLIT_MIN_RATIO = 2.0
SPLIT_TOLERANCE = 0.02


@dataclass
class StepResult:
    """한 단계가 무엇을 했는가. **버린 것을 숨기지 않는다.**"""

    name: str
    rows_in: int
    rows_out: int
    why: str = ""

    @property
    def dropped(self) -> int:
        return self.rows_in - self.rows_out


@dataclass
class RefineryReport:
    contract_fingerprint: str
    steps: list[StepResult] = field(default_factory=list)
    symbols_in: int = 0
    symbols_out: int = 0
    rows_in: int = 0
    rows_out: int = 0
    content_hash: str = ""
    findings: dict = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return not self.findings

    def accounting_holds(self) -> bool:
        """**단계별 행수가 사슬로 이어지는가.** 끊기면 어딘가 조용히 버렸다."""
        if not self.steps:
            return self.rows_in == self.rows_out
        if self.steps[0].rows_in != self.rows_in:
            return False
        if self.steps[-1].rows_out != self.rows_out:
            return False
        return all(a.rows_out == b.rows_in
                   for a, b in zip(self.steps, self.steps[1:]))


# ── 단계들 (전부 순수 함수) ─────────────────────────────────────────────────
#
# 행은 dict 다: instrument_id · trade_date · open/high/low/close · volume ·
# notional · observed_at. 키가 없으면 그 단계는 건드리지 않는다 - 없는 열을
# 가정하고 지어내지 않는다.


def _key(r: dict) -> tuple:
    return (r.get("instrument_id"), r.get("trade_date"))


def dedup_key(rows: list[dict]) -> tuple[list[dict], str]:
    """같은 (종목, 날짜) 중복 접기. **뒤에 온 것을 남긴다**(정정이 뒤에 온다)."""
    seen: dict[tuple, dict] = {}
    for r in rows:
        seen[_key(r)] = r
    return list(seen.values()), "같은 (종목,날짜) 중복을 접었다(뒤 행 우선)"


def sort_by_event_time(rows: list[dict]) -> tuple[list[dict], str]:
    """사건시각 정렬. 정렬이 안 되면 창 계산이 통째로 어긋난다."""
    return sorted(rows, key=lambda r: (str(r.get("instrument_id")),
                                       str(r.get("trade_date")))), "정렬"


def drop_non_trading(rows: list[dict]) -> tuple[list[dict], str]:
    """거래가 없던 행 제거. **거래량 0 은 관측이 아니라 빈칸이다.**"""
    out = [r for r in rows if _num(r.get("volume")) not in (None, 0.0)]
    return out, "거래량 0 인 행 제거"


def drop_missing(rows: list[dict]) -> tuple[list[dict], str]:
    """필수 값 결측 제거. **채우지 않는다** - 채우면 지어낸 데이터다."""
    need = ("close",)
    out = [r for r in rows if all(_num(r.get(k)) is not None for k in need)]
    return out, f"필수 열 결측 제거({', '.join(need)})"


def adjust_corporate_action(rows: list[dict], actions: dict
                            ) -> tuple[list[dict], str]:
    """수정주가 적용. **ex_date 이전 가격을 비율로 나눈다.**

    `actions`: {instrument_id: [(ex_date, ratio), ...]}
    ratio 는 "1주가 몇 주가 되었나"(액면분할 10:1 이면 10.0).

    ▶ 왜 이전을 나누는가
      분할 뒤 가격은 이미 낮다. 과거를 그 배수로 낮춰야 시계열이 이어진다.
      뒤를 올리면 오늘 가격이 실제와 달라져 체결 가능성 판단이 깨진다.
    """
    if not actions:
        return list(rows), "적용할 자본변동 없음"
    applied = 0
    out = []
    for r in rows:
        sym, d = r.get("instrument_id"), str(r.get("trade_date"))
        factor = 1.0
        for ex_date, ratio in actions.get(sym, ()):
            # **ex_date 당일은 이미 조정된 가격이다** - 그 앞만 나눈다
            if d < str(ex_date) and ratio:
                factor *= float(ratio)
        if factor != 1.0:
            r = dict(r)
            for col in ("open", "high", "low", "close"):
                v = _num(r.get(col))
                if v is not None:
                    r[col] = v / factor
            applied += 1
        out.append(r)
    return out, f"자본변동 조정 {applied}행"


def cut_at_unadjusted_gap(rows: list[dict]) -> tuple[list[dict], str]:
    """미조정 갭 앞을 버린다. **차선책이다 - 고치는 게 아니라 자른다.**

    종가가 정확히 정수배로 뛴 곳은 급등이 아니라 조정 미적용이다. 그 앞
    구간은 지금 시계열과 이어지지 않으므로 쓰지 않는다.
    """
    by_sym: dict[str, list[dict]] = {}
    for r in rows:
        by_sym.setdefault(str(r.get("instrument_id")), []).append(r)
    out, cut_syms = [], 0
    for sym, rs in by_sym.items():
        rs = sorted(rs, key=lambda r: str(r.get("trade_date")))
        cut = 0
        for i in range(1, len(rs)):
            prev, cur = _num(rs[i - 1].get("close")), _num(rs[i].get("close"))
            if not prev or not cur:
                continue
            for ratio in (cur / prev, prev / cur):
                if ratio >= SPLIT_MIN_RATIO:
                    nearest = round(ratio)
                    if nearest and abs(ratio - nearest) <= SPLIT_TOLERANCE:
                        cut = i          # **마지막 갭 뒤부터 쓴다**
        if cut:
            cut_syms += 1
        out.extend(rs[cut:])
    return out, f"미조정 갭 뒤만 사용({cut_syms}종목 절단)"


def winsorize_extreme(rows: list[dict], *, pct: float = 0.005
                      ) -> tuple[list[dict], str]:
    """극단 수익률 절단 - **아직 구현 안 됐다. 조용히 넘기지 않는다.**

    ▶ 왜 통과시키지 않는가 (2026-08-14)
      처음에 이 함수를 `return list(rows), "극단값 절단"` 으로 두었다. 즉
      **아무것도 안 하면서 했다고 보고**했다. 계약이 `WINSORIZE_EXTREME` 를
      선언하면 보고서에는 절단했다고 적히고 판은 안 바뀐다 - 원장이 거짓을
      말하는 상태이고, 이 저장소가 하루 종일 고쳐온 그 실패 방식이다.

      절단 기준(분위? 표준편차 몇 배? 종목별? 전체?)은 **판단**이라 여기서
      임의로 정할 수 없다. 정해지기 전까지는 선언 자체를 막는다.
    """
    raise NotImplementedError(
        "WINSORIZE_EXTREME 은 아직 구현되지 않았다 - 절단 기준(분위/배수, "
        "종목별/전체)이 판단 사항이라 정해지기 전에는 쓸 수 없다. "
        "계약에서 빼거나, 기준을 정하고 여기를 구현해라")


def _num(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# 계약 어휘 -> 실행 함수. **계약에 있는 것만 돈다.**
_STEPS = {
    "DEDUP_KEY": lambda rows, ctx: dedup_key(rows),
    "SORT_BY_EVENT_TIME": lambda rows, ctx: sort_by_event_time(rows),
    "DROP_NON_TRADING": lambda rows, ctx: drop_non_trading(rows),
    "ADJUST_CORPORATE_ACTION":
        lambda rows, ctx: adjust_corporate_action(rows, ctx.get("actions") or {}),
    "CUT_AT_UNADJUSTED_GAP": lambda rows, ctx: cut_at_unadjusted_gap(rows),
    "DROP_MISSING": lambda rows, ctx: drop_missing(rows),
    "WINSORIZE_EXTREME": lambda rows, ctx: winsorize_extreme(rows),
}

# **순서는 우리가 정한다.** 계약이 집합으로 주더라도 적용 순서가 다르면
# 결과가 달라진다 - 순서까지 계약이면 실험마다 순서가 달라진다.
_ORDER = ("DEDUP_KEY", "SORT_BY_EVENT_TIME", "DROP_NON_TRADING",
          "ADJUST_CORPORATE_ACTION", "CUT_AT_UNADJUSTED_GAP",
          "DROP_MISSING", "WINSORIZE_EXTREME")


# ── 굳힌 뒤 검사 (데이터를 보고 판정) ───────────────────────────────────────


# 연간 기대 폐지율(KRX 어림). 이보다 **한참** 낮으면 표본이 의심스럽다.
# 임계를 여기 하나로 두는 이유: 창 길이마다 손으로 정하면 1년 창에서
# 오탐이 나고(연 1~2% 폐지는 정상) 사람이 관문을 무시하기 시작한다.
DELISTING_PER_YEAR = 0.010
DELISTING_ALARM_FRACTION = 0.5      # 기대의 절반 미만이면 알린다


def check_delistings_exist(rows: list[dict], *,
                           per_year: float = DELISTING_PER_YEAR) -> list[str]:
    """**사라진 종목이 기대보다 한참 적으면 생존편향이다** (2026-08-14 실측).

    `krx-basket-daily/v3` 전수 조사: 10.6년 동안 3,924종목 **전부**가 마지막
    달까지 거래했다 - 폐지 0건. 오늘 목록을 잡고 과거로 역채운 서명이다.
    그 표본에는 파산이 없으므로 **낙폭이 실제보다 얕게 나온다.**

    ▶ 임계는 **기간에 비례**한다 (2026-08-14 정정)
      처음엔 고정 2% 로 뒀더니 2025년 1년 창에서 1.1%(42/3833)를 잡았다.
      연 1~2% 폐지는 KRX 에서 정상이므로 그건 오탐이다. 오탐이 한 번 나면
      다음부터 사람이 관문을 안 본다 - 관문은 정확해야 살아남는다.
    """
    if not rows:
        return []
    last_by_sym: dict[str, str] = {}
    first_seen = "9999-99-99"
    for r in rows:
        sym, d = str(r.get("instrument_id")), str(r.get("trade_date"))
        if d > last_by_sym.get(sym, ""):
            last_by_sym[sym] = d
        if d < first_seen:
            first_seen = d
    if not last_by_sym:
        return []
    final = max(last_by_sym.values())
    years = max(_year_span(first_seen, final), 0.25)
    expected = per_year * years
    floor = expected * DELISTING_ALARM_FRACTION

    gone = sum(1 for d in last_by_sym.values() if d[:7] < final[:7])
    ratio = gone / len(last_by_sym)
    if ratio < floor:
        return [f"{years:.1f}년 동안 사라진 종목이 {gone}/{len(last_by_sym)}"
                f"({ratio:.2%}) 뿐이다 - 기대 {expected:.1%}(연 {per_year:.1%})의 "
                f"절반에도 못 미친다. 상장폐지가 빠진 생존편향 표본으로 보이고, "
                f"그 표본에는 파산이 없으므로 **낙폭이 실제보다 얕게 나온다**"]
    return []


def _year_span(start: str, end: str) -> float:
    from datetime import date
    try:
        y1, m1, d1 = (int(x) for x in str(start)[:10].split("-"))
        y2, m2, d2 = (int(x) for x in str(end)[:10].split("-"))
        return (date(y2, m2, d2) - date(y1, m1, d1)).days / 365.25
    except (ValueError, TypeError):
        return 1.0


def check_no_future_observation(rows: list[dict]) -> list[str]:
    """**관측시각이 거래일보다 앞설 수 없다.** 앞서면 미래를 본 것이다."""
    bad = []
    for r in rows:
        obs, td = r.get("observed_at"), r.get("trade_date")
        if obs and td and str(obs)[:10] < str(td)[:10]:
            bad.append(f"{r.get('instrument_id')} {td}: 관측 {obs} 이 거래일보다 앞선다")
            if len(bad) >= 3:
                break
    return bad


def check_monotonic_per_symbol(rows: list[dict]) -> list[str]:
    """종목별 날짜가 오름차순인가. 어긋나면 창 계산이 통째로 틀린다."""
    seen: dict[str, str] = {}
    for r in rows:
        sym, d = str(r.get("instrument_id")), str(r.get("trade_date"))
        if sym in seen and d < seen[sym]:
            return [f"{sym}: 날짜가 뒤로 갔다 {seen[sym]} -> {d}"]
        seen[sym] = d
    return []


DATA_GATES = (("생존편향", check_delistings_exist),
              ("미래 관측", check_no_future_observation),
              ("시간 단조", check_monotonic_per_symbol))


# ── 정제 본체 ────────────────────────────────────────────────────────────────


def refine(rows: list[dict], contract: DatasetContract, *,
           actions: dict | None = None) -> tuple[list[dict], RefineryReport]:
    """계약대로 정제한다. **계약이 관문을 못 넘으면 시작도 안 한다.**"""
    pre = validate(contract)
    rep = RefineryReport(contract_fingerprint=pre["fingerprint"])
    if not pre["ok"]:
        rep.findings = dict(pre["findings"])
        return [], rep

    rep.rows_in = len(rows)
    rep.symbols_in = len({str(r.get("instrument_id")) for r in rows})

    ctx = {"actions": actions or {}}
    cur = list(rows)
    declared = set(contract.cleaning)
    for name in _ORDER:
        if name not in declared:
            continue
        before = len(cur)
        cur, why = _STEPS[name](cur, ctx)
        rep.steps.append(StepResult(name, before, len(cur), why))

    rep.rows_out = len(cur)
    rep.symbols_out = len({str(r.get("instrument_id")) for r in cur})
    rep.content_hash = _panel_hash(cur)

    # 굳힌 뒤 검사
    for gate, fn in DATA_GATES:
        got = fn(cur)
        if got:
            rep.findings[gate] = got
    if not rep.accounting_holds():
        rep.findings["행수 대조"] = ["단계별 행수가 이어지지 않는다 - "
                                     "어딘가 조용히 버렸다"]
    return cur, rep


def _panel_hash(rows: list[dict]) -> str:
    """판의 내용 해시. 정렬해서 계산하므로 순서에 흔들리지 않는다."""
    h = hashlib.sha256()
    for k in sorted(f"{r.get('instrument_id')}|{r.get('trade_date')}|"
                    f"{r.get('close')}" for r in rows):
        h.update(k.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()[:16]


# ── 자체 점검 ────────────────────────────────────────────────────────────────


def _c(**kw):
    from dataset_contract import (CostModel, DatasetContract, FeatureRef,
                                  LabelRef, SplitPolicy)
    args = dict(
        hypothesis_id="h1", source_dataset="krx-basket-daily", source_version="v3",
        universe_key="krx_all", universe_rule="PIT_MEMBERSHIP",
        start_date="2016-01-04", end_date="2026-08-10",
        features=(FeatureRef("mom_20d", "1", 20),),
        labels=(LabelRef("fwd_ret_20d", 20),),
        split=SplitPolicy("WALK_FORWARD", 5, 20),
        costs=CostModel(1.5, 5.0, 20.0),
        cleaning=("DEDUP_KEY", "SORT_BY_EVENT_TIME", "DROP_NON_TRADING",
                  "CUT_AT_UNADJUSTED_GAP", "DROP_MISSING"),
        created_by="test")
    args.update(kw)
    return DatasetContract(**args)


def _rows(*specs):
    out = []
    for sym, date, close, vol in specs:
        out.append({"instrument_id": sym, "trade_date": date, "close": close,
                    "open": close, "high": close, "low": close,
                    "volume": vol, "notional": (close or 0) * (vol or 0),
                    "observed_at": date})
    return out


def _check_bad_contract_never_runs():
    """**관문을 못 넘으면 정제를 시작하지 않는다.** 오염된 판을 만들 이유가 없다."""
    bad = _c(universe_rule="CURRENT_MEMBERSHIP")
    out, rep = refine(_rows(("A", "2020-01-02", 100.0, 10)), bad)
    assert out == [] and not rep.approved, rep
    assert "누출" in rep.findings, rep.findings
    assert not rep.steps, "계약이 막혔는데 단계가 돌았다"


def _check_every_dropped_row_is_accounted():
    """**버린 행은 반드시 센다** - 조용히 버리면 나중에 못 찾는다."""
    rows = _rows(("A", "2020-01-02", 100.0, 10),
                 ("A", "2020-01-02", 101.0, 10),   # 중복
                 ("A", "2020-01-03", 0.0, 0),      # 거래 0
                 ("A", "2020-01-06", None, 5),     # 결측
                 ("A", "2020-01-07", 102.0, 7))
    out, rep = refine(rows, _c())
    assert rep.accounting_holds(), [(s.name, s.rows_in, s.rows_out) for s in rep.steps]
    assert rep.rows_in == 5 and rep.rows_out == len(out)
    named = {s.name: s.dropped for s in rep.steps}
    assert named["DEDUP_KEY"] == 1, named
    assert named["DROP_NON_TRADING"] == 1, named
    assert named["DROP_MISSING"] == 1, named


def _check_only_declared_steps_run():
    """계약에 없는 정제는 돌지 않는다 - 몰래 고치면 계약이 거짓이 된다."""
    out, rep = refine(_rows(("A", "2020-01-02", 100.0, 10),
                            ("A", "2020-01-02", 100.0, 10)),
                      _c(cleaning=("SORT_BY_EVENT_TIME", "DROP_MISSING",
                                   "CUT_AT_UNADJUSTED_GAP")))
    assert [s.name for s in rep.steps] == ["SORT_BY_EVENT_TIME",
                                           "CUT_AT_UNADJUSTED_GAP",
                                           "DROP_MISSING"], \
        [s.name for s in rep.steps]
    assert len(out) == 2, "중복 제거를 선언 안 했는데 접혔다"


def _check_step_order_is_fixed_not_contract_order():
    """**순서는 우리가 정한다.** 계약이 순서를 흔들면 실험마다 결과가 달라진다."""
    a = _c(cleaning=("DROP_MISSING", "DEDUP_KEY", "SORT_BY_EVENT_TIME"))
    b = _c(cleaning=("SORT_BY_EVENT_TIME", "DEDUP_KEY", "DROP_MISSING"))
    rows = _rows(("A", "2020-01-02", 100.0, 10), ("A", "2020-01-02", 100.0, 10))
    _, ra = refine(rows, a)
    _, rb = refine(rows, b)
    assert [s.name for s in ra.steps] == [s.name for s in rb.steps]


def _check_corporate_action_divides_the_past_only():
    """**ex_date 앞만 나눈다.** 뒤를 건드리면 오늘 가격이 실제와 달라진다."""
    rows = _rows(("A", "2020-01-02", 1000.0, 10),
                 ("A", "2020-01-03", 1000.0, 10),
                 ("A", "2020-01-06", 100.0, 10))    # 10:1 분할 후
    actions = {"A": [("2020-01-06", 10.0)]}
    out, rep = refine(rows, _c(cleaning=("SORT_BY_EVENT_TIME",
                                         "ADJUST_CORPORATE_ACTION",
                                         "DROP_MISSING")),
                      actions=actions)
    by_date = {r["trade_date"]: r["close"] for r in out}
    assert by_date["2020-01-02"] == 100.0, by_date
    assert by_date["2020-01-03"] == 100.0, by_date
    assert by_date["2020-01-06"] == 100.0, by_date   # 당일은 안 건드린다
    # 조정 뒤에는 정수배 갭이 사라진다
    assert not [s for s in rep.steps if s.name == "CUT_AT_UNADJUSTED_GAP"]


def _check_cut_keeps_only_after_the_gap():
    """미조정 갭이 있으면 그 앞을 버린다 - 자르는 것이지 고치는 것이 아니다."""
    rows = _rows(("A", "2020-01-02", 1000.0, 10),
                 ("A", "2020-01-03", 100.0, 10),   # ×0.1 미조정 갭
                 ("A", "2020-01-06", 101.0, 10))
    out, _ = refine(rows, _c(cleaning=("SORT_BY_EVENT_TIME",
                                       "CUT_AT_UNADJUSTED_GAP", "DROP_MISSING")))
    dates = sorted(r["trade_date"] for r in out)
    assert dates == ["2020-01-03", "2020-01-06"], dates


def _check_survivorship_is_detected():
    """**폐지가 0건이면 막는다** - 실측으로 3,924종목 전부가 살아있었다."""
    # 6.6년인데 전원 생존: 막힌다
    rows = _rows(*[(f"S{i}", d, 100.0, 10)
                   for i in range(100) for d in ("2020-01-02", "2026-08-10")])
    _, rep = refine(rows, _c())
    assert "생존편향" in rep.findings, rep.findings

    # 같은 기간에 기대만큼 사라짐(6.6년 × 1% = 6.6% > 절반 임계): 통과
    rows2 = _rows(*[(f"S{i}", d, 100.0, 10)
                    for i in range(93) for d in ("2020-01-02", "2026-08-10")],
                  *[(f"D{i}", "2020-01-02", 100.0, 10) for i in range(7)])
    _, rep2 = refine(rows2, _c())
    assert "생존편향" not in rep2.findings, rep2.findings


def _check_survivorship_threshold_scales_with_span():
    """**짧은 창에서 오탐이 나면 안 된다** (2026-08-14 정정).

    고정 2% 임계를 쓰니 2025년 1년 창의 1.1%(실물 42/3833)를 잡았다.
    연 1~2% 폐지는 정상이므로 그건 오탐이고, 오탐이 한 번 나면 다음부터
    사람이 관문을 안 본다.
    """
    # 1년 창에서 1.1% 폐지: 기대 1.0%의 절반(0.5%)을 넘으므로 통과해야 한다
    one_year = _rows(*[(f"S{i}", d, 100.0, 10)
                       for i in range(99) for d in ("2025-01-02", "2025-12-30")],
                     *[("D0", "2025-01-02", 100.0, 10)])
    got = check_delistings_exist(one_year)
    assert not got, ("1년 창의 1% 폐지는 정상인데 잡혔다", got)

    # 같은 비율이라도 10년 창이면 한참 모자라므로 잡아야 한다
    ten_year = _rows(*[(f"S{i}", d, 100.0, 10)
                       for i in range(99) for d in ("2016-01-04", "2026-08-10")],
                     *[("D0", "2016-01-04", 100.0, 10)])
    assert check_delistings_exist(ten_year), "10년 창의 1% 폐지는 잡아야 한다"


def _check_future_observation_is_caught():
    """관측이 거래일보다 앞서면 미래를 본 것이다."""
    rows = _rows(("A", "2020-01-02", 100.0, 10), ("B", "2019-01-02", 100.0, 10))
    rows[0]["observed_at"] = "2019-12-31"
    _, rep = refine(rows, _c())
    assert "미래 관측" in rep.findings, rep.findings


def _check_hash_is_order_independent_but_value_sensitive():
    """같은 판이면 같은 해시, 값이 다르면 다른 해시."""
    a = _rows(("A", "2020-01-02", 100.0, 10), ("B", "2020-01-02", 200.0, 10))
    b = list(reversed(a))
    assert _panel_hash(a) == _panel_hash(b)
    c = _rows(("A", "2020-01-02", 100.5, 10), ("B", "2020-01-02", 200.0, 10))
    assert _panel_hash(a) != _panel_hash(c)


def _check_unimplemented_step_fails_loudly():
    """**안 한 것을 했다고 보고하면 원장이 거짓이 된다** (2026-08-14).

    `winsorize_extreme` 을 "행 그대로 돌려주고 절단했다고 적기" 로 두었다가
    고쳤다. 계약이 선언했는데 실제로는 아무 일도 안 일어나는 상태가 가장
    나쁘다 - 안 된 것보다 **안 된 줄 모르는 것**이 나쁘다.
    """
    try:
        refine(_rows(("A", "2020-01-02", 100.0, 10)),
               _c(cleaning=("DROP_MISSING", "CUT_AT_UNADJUSTED_GAP",
                            "WINSORIZE_EXTREME")))
    except NotImplementedError as e:
        assert "판단" in str(e), e
    else:
        raise AssertionError("구현 안 된 단계가 조용히 통과했다")


def _check_empty_input_does_not_crash():
    """빈 입력은 오류가 아니라 빈 판이다 - 다만 승인은 아니다."""
    out, rep = refine([], _c())
    assert out == [] and rep.rows_out == 0
    assert rep.accounting_holds()


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_bad_contract_never_runs();     print("  막힌 계약은 안 돈다      OK")
    _check_every_dropped_row_is_accounted()
    print("  버린 행 전부 대조       OK")
    _check_only_declared_steps_run();     print("  선언한 정제만 돈다       OK")
    _check_step_order_is_fixed_not_contract_order()
    print("  순서는 고정             OK")
    _check_corporate_action_divides_the_past_only()
    print("  수정주가는 과거만 나눔   OK")
    _check_cut_keeps_only_after_the_gap()
    print("  미조정 갭 앞을 버림     OK")
    _check_survivorship_is_detected();    print("  생존편향 탐지            OK")
    _check_survivorship_threshold_scales_with_span()
    print("  임계가 기간에 비례      OK")
    _check_future_observation_is_caught(); print("  미래 관측 탐지           OK")
    _check_hash_is_order_independent_but_value_sensitive()
    print("  판 해시(순서 무관)      OK")
    _check_unimplemented_step_fails_loudly()
    print("  미구현은 큰소리로 실패   OK")
    _check_empty_input_does_not_crash();  print("  빈 입력 안전             OK")
    print("데이터셋 정제소 12개 영역 통과.")
