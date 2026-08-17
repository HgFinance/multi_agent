#!/usr/bin/env python3
"""실험 데이터셋 계약 - **리서치의 의도와 실행면 사이의 유일한 문서.**

소유: 재일 (퀀트·백테스트본부 QNT)
근거: 2026-08-14 설계 결정 "정제하는 AI 가 아니라, 정제 방법을 정하는 AI +
      결정론적으로 실행하는 공장"

▶ 왜 이 칸이 필요한가 (2026-08-14 실측)
  공장에 **실험용 데이터셋을 정제하는 공정이 없다.** 지금은 모든 실험이 같은
  파케이(`krx-basket-daily/v3`)를 통째로 읽고 시그널 안에서 그때그때 거른다:
  유동성 필터도, 액면분할 끊기도, 웜업도 전부 `strategy_templates` 실행 중에
  일어난다. 그래서 세 가지가 동시에 무너진다.

    ① **같은 정제를 매 실험이 다시 한다** - 결과가 아니라 비용만 반복된다.
    ② **실험마다 정제가 달라질 수 있다** - 무엇을 걸렀는지 원장에 안 남는다.
    ③ **누출 검사가 백테스트 안에 있다** - 데이터셋이 이미 오염된 채로
       실험이 시작되면 그 뒤 어떤 검사도 늦다.

  López de Prado 의 메타전략 패러다임이 말하는 생산 사슬(데이터 큐레이터 →
  피처 분석가 → 전략가 → 백테스트 전문가)에서 **데이터 큐레이터 칸**이다.

▶ 이 파일이 하는 일과 안 하는 일
  한다   : 계약을 **정의**하고, 지문을 만들고, **돌리기 전에 거부**한다.
  안 한다: 데이터를 읽거나 만들지 않는다. 그건 러너(`spec_dataset_builder`)
           몫이다. 여기는 순수 함수만 둔다 - DB 없이 검사가 돌아야
           "돌려보기 전에 막는다" 가 성립한다.

▶ 재사용 정제 vs 가설 전용 변환
  계약이 이 둘을 **필드로 가른다**. 앞은 데이터셋 층에서 한 번 하고 여러
  실험이 나눠 쓰고, 뒤는 실험마다 다시 한다.

    재사용(canonical) : 중복 제거·시각 정렬·수정주가·시장시간·유니버스 PIT
    가설 전용(experiment): 피처 창·라벨 지평·표본 분할·비용 가정

  가르는 기준은 "가설이 바뀌면 값이 바뀌는가" 다. 수정주가는 어느 가설이든
  같은 값이라 canonical 이고, 10초 forward return 은 지평이 곧 가설이라
  experiment 다.

자체 점검: python departments/04-quant-backtest/pipeline/dataset_contract.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import asdict, dataclass, field

MODULE_VERSION = "quant-dataset-contract-v1"

# ── 통제 어휘 ────────────────────────────────────────────────────────────────
#
# 자유 문자열을 받으면 계약이 문서가 아니라 낙서가 된다. 값을 늘리는 것은
# 결정이고, 결정은 이 표를 고치는 것으로만 한다.

CLEANING_RULES = frozenset({
    "DEDUP_KEY",            # 같은 (시각, 종목) 중복 행 접기
    "SORT_BY_EVENT_TIME",   # 사건시각 오름차순 정렬
    "DROP_NON_TRADING",     # 시장 미개장일 제거
    "ADJUST_CORPORATE_ACTION",  # 수정주가 적용(분할·병합·배당)
    "CUT_AT_UNADJUSTED_GAP",    # 미조정 갭 앞에서 시계열 절단(차선책)
    "DROP_MISSING",         # 결측 행 제거 - **채우지 않는다**
    "WINSORIZE_EXTREME",    # 극단값 절단(분포 꼬리)
})

# ▶ 결측을 **채우는** 규칙은 어휘에 없다. 앞뒤로 채우면 그 자체가 지어낸
#   데이터이고, 미래값으로 채우면 그대로 누출이다. 없으면 없는 채로 둔다.

SPLIT_KINDS = frozenset({"WALK_FORWARD", "HOLDOUT_TAIL", "SINGLE_WINDOW"})

UNIVERSE_RULES = frozenset({
    "PIT_MEMBERSHIP",   # 각 날짜에 실제로 존재하던 종목만
    "CURRENT_MEMBERSHIP",  # 오늘 살아있는 종목 - **생존편향**
})


class ContractError(ValueError):
    """계약이 성립하지 않는다. 돌리기 전에 던진다."""


@dataclass(frozen=True)
class FeatureRef:
    """피처 **참조**. 정의가 아니라 가리키기다.

    정의는 `quant.feature_specs`(코드·수식·입력계약·가용지연)에 있고 여기서는
    그것을 **어느 창으로 쓰는가**만 정한다. 수식을 여기 적으면 같은 피처가
    실험마다 다른 뜻이 된다 - 레지스트리를 둔 이유가 사라진다.
    """

    feature_code: str
    spec_version: str
    window_days: int
    # 관측이 실제로 손에 들어오기까지의 지연(거래일). 0 이면 당일 종가로 안다.
    availability_lag_days: int = 0

    def __post_init__(self) -> None:
        if not self.feature_code or not self.spec_version:
            raise ContractError("피처는 코드와 사양 버전을 함께 가리켜야 한다 - "
                                "버전 없이 가리키면 나중에 어느 정의였는지 모른다")
        if self.window_days < 1:
            raise ContractError(f"{self.feature_code}: 관측 창은 1일 이상이어야 한다")
        if self.availability_lag_days < 0:
            raise ContractError(f"{self.feature_code}: 가용 지연이 음수다 - "
                                f"미래에 알게 되는 값을 과거에 쓴다는 뜻이다")


@dataclass(frozen=True)
class LabelRef:
    """라벨. **지평이 곧 가설**이므로 실험 전용이다."""

    name: str
    horizon_days: int

    def __post_init__(self) -> None:
        if self.horizon_days < 1:
            raise ContractError(
                f"{self.name}: 예측 지평은 1일 이상이어야 한다. 0 이면 그 시점에 "
                f"이미 아는 값을 맞히는 것이고, 음수면 과거를 예측하는 것이다")


@dataclass(frozen=True)
class SplitPolicy:
    """표본 분할. **금지구간(embargo)이 라벨 지평보다 짧으면 학습이 시험을 본다.**

    ▶ 왜 embargo 인가 (López de Prado, purging & embargo)
      라벨이 t..t+h 를 본다면, 학습 마지막 날 t 의 라벨은 **시험 구간 앞머리와
      겹친다.** 그 겹침만큼 시험은 이미 본 것을 맞히는 셈이다. 그래서 두 구간
      사이를 최소 h 만큼 비운다.
    """

    kind: str
    n_windows: int = 1
    embargo_days: int = 0

    def __post_init__(self) -> None:
        if self.kind not in SPLIT_KINDS:
            raise ContractError(f"모르는 분할 방식: {self.kind} - "
                                f"사용 가능: {sorted(SPLIT_KINDS)}")
        if self.n_windows < 1:
            raise ContractError("창은 1개 이상이어야 한다")
        if self.embargo_days < 0:
            raise ContractError("금지구간은 음수일 수 없다")


@dataclass(frozen=True)
class CostModel:
    """비용 가정. **계약에 넣는 이유는 지문에 들어가야 하기 때문이다.**

    비용을 바꾸면 같은 신호가 다른 결론을 낸다. 지문 밖에 두면 "비용을 낮춰
    통과시켰다" 가 원장에 안 남는다.
    """

    fee_bps: float
    slippage_bps: float
    tax_bps: float = 0.0

    def __post_init__(self) -> None:
        for name, v in (("fee_bps", self.fee_bps),
                        ("slippage_bps", self.slippage_bps),
                        ("tax_bps", self.tax_bps)):
            if v < 0:
                raise ContractError(f"{name} 이 음수다 - 비용이 수익이 된다")


@dataclass(frozen=True)
class DatasetContract:
    """한 실험이 쓸 데이터셋의 전부. **이것만 보고 다시 만들 수 있어야 한다.**"""

    # ── 정체성 ──────────────────────────────────────────────────────────
    hypothesis_id: str
    # 어느 canonical 데이터셋 위에 서는가. 재사용 정제는 여기서 이미 끝나 있다.
    source_dataset: str          # 예: "krx-basket-daily"
    source_version: str          # 예: "v3"

    # ── 관측 범위 ───────────────────────────────────────────────────────
    universe_key: str
    universe_rule: str           # UNIVERSE_RULES
    start_date: str              # YYYY-MM-DD
    end_date: str

    # ── 가설 전용 변환 ──────────────────────────────────────────────────
    features: tuple[FeatureRef, ...]
    labels: tuple[LabelRef, ...]
    split: SplitPolicy
    costs: CostModel

    # ── 재사용 정제(무엇이 이미 적용됐는가) ─────────────────────────────
    cleaning: tuple[str, ...] = ()

    # 계약을 만든 주체. 사람이든 에이전트든 남긴다.
    created_by: str = "unknown"
    notes: str = ""

    # 파생값(지문 계산에서 제외하지 않는다 - 계약의 일부다)
    _extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.universe_rule not in UNIVERSE_RULES:
            raise ContractError(f"모르는 유니버스 규칙: {self.universe_rule}")
        bad = sorted(set(self.cleaning) - CLEANING_RULES)
        if bad:
            raise ContractError(
                f"어휘에 없는 정제 규칙: {bad} - 사용 가능: {sorted(CLEANING_RULES)}. "
                f"자유 문자열을 받으면 무엇을 걸렀는지 원장이 말하지 못한다")
        if not self.features:
            raise ContractError("피처가 없는 실험 데이터셋은 만들 이유가 없다")
        if not self.labels:
            raise ContractError("라벨이 없으면 무엇을 맞히는지 정의되지 않는다")
        if self.start_date >= self.end_date:
            raise ContractError(f"기간이 뒤집혔다: {self.start_date} >= {self.end_date}")

    # ── 지문 ────────────────────────────────────────────────────────────
    def fingerprint(self) -> str:
        """계약의 내용 해시. **실험 지문의 재료 중 하나다.**

        같은 계약이면 같은 데이터셋이어야 하고, 계약이 한 글자라도 다르면
        다른 데이터셋이어야 한다. `sort_keys` 로 필드 순서에 흔들리지 않게 한다.
        """
        payload = json.dumps(asdict(self), sort_keys=True, ensure_ascii=False,
                             default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    @property
    def max_feature_reach(self) -> int:
        """의사결정 시점에서 **과거로 가장 멀리 뻗는** 거리(거래일)."""
        return max(f.window_days + f.availability_lag_days for f in self.features)

    @property
    def max_label_reach(self) -> int:
        """의사결정 시점에서 **미래로 가장 멀리 뻗는** 거리(거래일)."""
        return max(lb.horizon_days for lb in self.labels)


# ── 검증 관문 ────────────────────────────────────────────────────────────────
#
# ▶ 왜 데이터셋 단계에서 막는가
#   지금은 누출 검사가 백테스트 안(`walk_forward`)에 있다. 그건 이미 만들어진
#   데이터셋을 쓰는 시점이라 **오염된 데이터셋은 그때 이미 존재한다.** 관문을
#   앞으로 당기면 오염된 것이 애초에 안 만들어진다.
#
#   각 검사는 **데이터 없이** 판정한다. 데이터를 봐야 아는 것(행수·분포)은
#   러너가 만든 뒤에 보고, 여기서는 계약만으로 아는 모순을 잡는다.


def check_temporal(c: DatasetContract) -> list[str]:
    """시간 축 모순. **미래를 보고 있는가.**"""
    bad = []
    for f in c.features:
        if f.availability_lag_days < 0:
            bad.append(f"{f.feature_code}: 가용 지연이 음수")
    for lb in c.labels:
        if lb.horizon_days < 1:
            bad.append(f"{lb.name}: 예측 지평이 미래가 아니다")
    return bad


def check_leakage(c: DatasetContract) -> list[str]:
    """누출. **학습 구간이 시험 구간의 답을 알고 있는가.**

    데이터 없이 잡을 수 있는 두 가지를 본다:
      ① 금지구간이 라벨 지평보다 짧다 → 창 경계에서 라벨이 겹친다
      ② 유니버스가 오늘 기준이다 → 생존편향(사라진 종목이 처음부터 없다)
    """
    bad = []
    if c.split.kind != "SINGLE_WINDOW" and c.split.embargo_days < c.max_label_reach:
        bad.append(
            f"금지구간 {c.split.embargo_days}일 < 라벨 지평 {c.max_label_reach}일 - "
            f"학습 마지막 날의 라벨이 시험 구간을 덮는다(López de Prado embargo)")
    if c.universe_rule == "CURRENT_MEMBERSHIP":
        bad.append(
            "유니버스가 오늘 기준이다 - 상장폐지·편출된 종목이 과거에서도 "
            "사라져 생존편향이 들어간다. PIT_MEMBERSHIP 이어야 한다")
    return bad


def check_reusable_cleaning(c: DatasetContract) -> list[str]:
    """재사용 정제가 **선언돼 있는가.**

    빠뜨리면 조용히 안 된 채로 지나간다 - 무엇을 안 했는지가 원장에 안 남는
    것이 문제이지, 안 하는 것 자체가 항상 틀린 것은 아니다. 그래서 수정주가는
    **둘 중 하나를 반드시 고르게** 한다(적용하거나, 못 해서 자르거나).
    """
    bad = []
    ca = {"ADJUST_CORPORATE_ACTION", "CUT_AT_UNADJUSTED_GAP"} & set(c.cleaning)
    if not ca:
        bad.append(
            "수정주가 정책이 없다 - ADJUST_CORPORATE_ACTION(적용) 이나 "
            "CUT_AT_UNADJUSTED_GAP(못 하니 자름) 중 하나를 선언해야 한다. "
            "미조정 분할은 +900% 수익률로 읽혀 모멘텀이 그 종목을 1등으로 뽑는다")
    if "DROP_MISSING" not in c.cleaning:
        bad.append("결측 정책이 없다 - 채우는 규칙은 어휘에 없으므로 "
                   "DROP_MISSING 을 선언하거나 계약을 다시 봐야 한다")
    return bad


def check_sanity(c: DatasetContract) -> list[str]:
    """상식. **표본이 사양을 받치는가.**"""
    bad = []
    reach = c.max_feature_reach + c.max_label_reach
    if c.split.kind == "WALK_FORWARD":
        # 창 하나가 최소한 뻗는 거리의 3배는 돼야 통계가 성립한다
        need = reach * 3 * c.split.n_windows
        span = _rough_trading_days(c.start_date, c.end_date)
        if span < need:
            bad.append(
                f"기간이 짧다: 거래일 약 {span}일인데 창 {c.split.n_windows}개 × "
                f"뻗는 거리 {reach}일 × 3 = {need}일이 필요하다")
    return bad


GATES = (("스키마", lambda c: []),      # dataclass __post_init__ 이 이미 본다
         ("시간축", check_temporal),
         ("누출", check_leakage),
         ("재사용 정제", check_reusable_cleaning),
         ("표본 상식", check_sanity))


def validate(c: DatasetContract) -> dict:
    """전 관문. **하나라도 걸리면 데이터셋을 만들지 않는다.**

    사유를 관문별로 나눠 돌려준다 - 뭉뚱그리면 리서치가 무엇을 고쳐야 할지
    모른다(오늘 하루 종일 본 실패 방식이다).
    """
    findings = {}
    for name, fn in GATES:
        got = fn(c)
        if got:
            findings[name] = got
    return {"ok": not findings, "fingerprint": c.fingerprint(),
            "findings": findings}


def _rough_trading_days(start: str, end: str) -> int:
    """어림 거래일. 정확한 캘린더는 러너가 보고, 여기서는 자릿수만 본다."""
    from datetime import date

    y1, m1, d1 = (int(x) for x in start.split("-"))
    y2, m2, d2 = (int(x) for x in end.split("-"))
    days = (date(y2, m2, d2) - date(y1, m1, d1)).days
    return int(days * 250 / 365)


# ── 자체 점검 ────────────────────────────────────────────────────────────────


def _base(**kw) -> DatasetContract:
    args = dict(
        hypothesis_id="h1", source_dataset="krx-basket-daily", source_version="v3",
        universe_key="krx_all", universe_rule="PIT_MEMBERSHIP",
        start_date="2016-01-04", end_date="2026-08-10",
        features=(FeatureRef("mom_20d", "1", 20),),
        labels=(LabelRef("fwd_ret_20d", 20),),
        split=SplitPolicy("WALK_FORWARD", n_windows=5, embargo_days=20),
        costs=CostModel(fee_bps=1.5, slippage_bps=5.0, tax_bps=20.0),
        cleaning=("DEDUP_KEY", "SORT_BY_EVENT_TIME", "DROP_NON_TRADING",
                  "ADJUST_CORPORATE_ACTION", "DROP_MISSING"),
        created_by="test")
    args.update(kw)
    return DatasetContract(**args)


def _check_good_contract_passes():
    """정상 계약이 막히면 관문이 아니라 벽이다."""
    r = validate(_base())
    assert r["ok"], r["findings"]
    assert len(r["fingerprint"]) == 16


def _check_embargo_shorter_than_label_is_leakage():
    """**금지구간 < 라벨 지평 = 학습이 시험 답을 본다.**"""
    r = validate(_base(split=SplitPolicy("WALK_FORWARD", 5, embargo_days=5),
                       labels=(LabelRef("fwd_ret_20d", 20),)))
    assert not r["ok"] and "누출" in r["findings"], r
    assert "embargo" in r["findings"]["누출"][0]

    # 단일 창은 경계가 없으므로 금지구간을 요구하지 않는다
    ok = validate(_base(split=SplitPolicy("SINGLE_WINDOW", 1, 0)))
    assert ok["ok"], ok["findings"]


def _check_current_universe_is_survivorship():
    """오늘 기준 유니버스는 사라진 종목을 과거에서도 지운다."""
    r = validate(_base(universe_rule="CURRENT_MEMBERSHIP"))
    assert not r["ok"] and any("생존편향" in x for x in r["findings"]["누출"]), r


def _check_corporate_action_policy_is_mandatory():
    """**미조정 분할은 +900% 수익률이 된다** - 정책을 안 고르면 막는다."""
    r = validate(_base(cleaning=("DEDUP_KEY", "DROP_MISSING")))
    assert not r["ok"], r
    assert any("수정주가" in x for x in r["findings"]["재사용 정제"]), r

    # 못 하니 자른다고 선언하면 통과한다 - 차선책도 선언이면 기록에 남는다
    ok = validate(_base(cleaning=("DEDUP_KEY", "DROP_MISSING",
                                  "CUT_AT_UNADJUSTED_GAP")))
    assert ok["ok"], ok["findings"]


def _check_unknown_cleaning_rule_is_rejected():
    """자유 문자열을 받으면 계약이 낙서가 된다."""
    try:
        _base(cleaning=("FILL_FORWARD",))
    except ContractError as e:
        assert "어휘에 없는" in str(e), e
    else:
        raise AssertionError("모르는 정제 규칙이 통과했다")


def _check_fill_is_not_in_the_vocabulary():
    """**결측을 채우는 규칙은 어휘에 없다** - 채우면 지어낸 데이터다."""
    for word in ("FILL_FORWARD", "FILL_BACKWARD", "INTERPOLATE", "FILL_ZERO"):
        assert word not in CLEANING_RULES, f"{word} 가 어휘에 들어왔다"


def _check_zero_horizon_label_is_rejected():
    """지평 0 은 이미 아는 값을 맞히는 것이다."""
    for h in (0, -5):
        try:
            LabelRef("x", h)
        except ContractError:
            pass
        else:
            raise AssertionError(f"지평 {h} 가 통과했다")


def _check_negative_availability_lag_is_rejected():
    """가용 지연이 음수면 미래에 알 값을 과거에 쓰는 것이다."""
    try:
        FeatureRef("f", "1", 20, availability_lag_days=-1)
    except ContractError as e:
        assert "미래" in str(e), e
    else:
        raise AssertionError("음수 가용 지연이 통과했다")


def _check_feature_must_pin_a_spec_version():
    """버전 없이 가리키면 나중에 어느 정의였는지 모른다."""
    try:
        FeatureRef("mom_20d", "", 20)
    except ContractError:
        pass
    else:
        raise AssertionError("사양 버전 없이 통과했다")


def _check_fingerprint_moves_with_every_field():
    """**계약이 다르면 데이터셋도 달라야 한다.** 안 그러면 지문이 거짓말이다."""
    base = _base()
    f0 = base.fingerprint()
    assert f0 == _base().fingerprint(), "같은 계약인데 지문이 흔들린다"

    variants = [
        _base(start_date="2017-01-04"),
        _base(universe_key="krx200"),
        _base(features=(FeatureRef("mom_20d", "2", 20),)),      # 사양 버전만 다름
        _base(features=(FeatureRef("mom_20d", "1", 21),)),      # 창만 다름
        _base(labels=(LabelRef("fwd_ret_20d", 21),)),
        _base(split=SplitPolicy("WALK_FORWARD", 6, 20)),
        _base(costs=CostModel(1.5, 5.0, 25.0)),                 # 세금만 다름
        _base(cleaning=("DEDUP_KEY", "DROP_MISSING", "CUT_AT_UNADJUSTED_GAP")),
    ]
    for v in variants:
        assert v.fingerprint() != f0, f"지문이 안 바뀐다: {v}"


def _check_reach_is_measured_from_decision_time():
    """뻗는 거리는 **가용 지연을 포함**한다 - 안 그러면 웜업이 모자란다."""
    c = _base(features=(FeatureRef("f1", "1", 20, availability_lag_days=2),
                        FeatureRef("f2", "1", 60)))
    assert c.max_feature_reach == 60, c.max_feature_reach
    c2 = _base(features=(FeatureRef("f1", "1", 60, availability_lag_days=5),))
    assert c2.max_feature_reach == 65, c2.max_feature_reach


def _check_short_period_is_flagged():
    """표본이 사양을 못 받치면 그 결과는 증거가 아니다."""
    r = validate(_base(start_date="2026-01-02", end_date="2026-08-10"))
    assert not r["ok"] and "표본 상식" in r["findings"], r


def _check_findings_are_split_by_gate():
    """뭉뚱그리면 리서치가 무엇을 고쳐야 할지 모른다."""
    r = validate(_base(universe_rule="CURRENT_MEMBERSHIP",
                       cleaning=("DEDUP_KEY",)))
    assert set(r["findings"]) >= {"누출", "재사용 정제"}, r["findings"]


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_good_contract_passes();            print("  정상 계약 통과          OK")
    _check_embargo_shorter_than_label_is_leakage()
    print("  금지구간 < 라벨 = 누출   OK")
    _check_current_universe_is_survivorship()
    print("  오늘 유니버스 = 생존편향 OK")
    _check_corporate_action_policy_is_mandatory()
    print("  수정주가 정책 필수       OK")
    _check_unknown_cleaning_rule_is_rejected()
    print("  어휘 밖 정제 규칙 거부   OK")
    _check_fill_is_not_in_the_vocabulary()
    print("  채우기는 어휘에 없다     OK")
    _check_zero_horizon_label_is_rejected()
    print("  지평 0 라벨 거부         OK")
    _check_negative_availability_lag_is_rejected()
    print("  음수 가용지연 거부       OK")
    _check_feature_must_pin_a_spec_version()
    print("  피처는 사양 버전을 박음  OK")
    _check_fingerprint_moves_with_every_field()
    print("  모든 필드가 지문을 움직임 OK")
    _check_reach_is_measured_from_decision_time()
    print("  뻗는 거리 = 창 + 가용지연 OK")
    _check_short_period_is_flagged()
    print("  짧은 표본 표시           OK")
    _check_findings_are_split_by_gate()
    print("  사유는 관문별로 나뉜다   OK")
    print("데이터셋 계약 13개 영역 통과.")
