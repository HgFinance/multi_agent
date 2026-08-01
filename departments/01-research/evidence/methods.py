#!/usr/bin/env python3
"""분석 방법론 레지스트리 - 리서치본부가 무슨 기법을 왜 쓰는지의 단일 출처.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-02 "각 분석가가 분석할 때 쓰는 방법론(논문·기법)을
      찾아서 스스로 도입해 가며 분석의 질이 성장했으면 좋겠다".

▶ 왜 코드로 된 레지스트리인가
  방법론을 문서에만 적으면 (1) 구현과 어긋나고 (2) "도입했다"와 "검증했다"가
  섞이며 (3) 데이터가 없어 못 쓰는 것과 안 쓰기로 한 것이 구분되지 않는다.
  여기서는 셋을 강제로 분리한다:
    ADOPTED   - 구현됨. module 이 실제 존재하고 분석가가 호출한다.
    CANDIDATE - 도입 가치는 있으나 아직 구현 안 됨.
    BLOCKED   - 데이터가 없어 지금은 불가. blocked_by 에 무엇이 없는지 적는다.
  **성과 검증(선순환)은 별도다** - 도입했다고 좋아진 게 아니다.
  research.analyst_calibration 에 표본이 쌓인 뒤에야 기여를 말할 수 있고,
  그 전까지 validated 는 False 로 둔다. 이 구분을 흐리면 '도입했으니
  좋아졌다'는 자기충족 서사가 된다.

▶ 규율
  - 인용(citation)이 없는 방법은 등재하지 않는다. 출처 없는 임의 규칙은
    기법이 아니라 취향이다.
  - 부분 구현은 반드시 partial_reason 에 무엇이 빠졌는지 적는다. 9개 중
    6개만 계산해 놓고 "F-Score" 라 부르면 그건 다른 지표다.

실행: python evidence/methods.py     # 자체 점검 + 현황 출력
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

REGISTRY_VERSION = "research-method-registry-v1"

STATUS_ADOPTED = "ADOPTED"
STATUS_CANDIDATE = "CANDIDATE"
STATUS_BLOCKED = "BLOCKED"
_STATUSES = (STATUS_ADOPTED, STATUS_CANDIDATE, STATUS_BLOCKED)

ANALYSTS = ("RES-03", "RES-04", "RES-05", "RES-06", "RES-07", "RES-09")


@dataclass(frozen=True)
class Method:
    key: str
    name: str
    analyst: str                 # RES-xx
    citation: str                # 논문·1차 출처. 없으면 등재 불가
    status: str
    inputs: tuple[str, ...] = () # 필요한 데이터 (없으면 BLOCKED 사유가 된다)
    module: str | None = None    # ADOPTED 면 구현 위치
    partial_reason: str | None = None
    blocked_by: str | None = None
    validated: bool = False      # calibration 표본으로 기여가 확인됐는가
    note: str = ""

    def __post_init__(self):
        if self.status not in _STATUSES:
            raise ValueError(f"{self.key}: 알 수 없는 status {self.status}")
        if self.analyst not in ANALYSTS:
            raise ValueError(f"{self.key}: 알 수 없는 분석가 {self.analyst}")
        if not self.citation.strip():
            raise ValueError(f"{self.key}: 인용 없는 방법은 등재하지 않는다")
        if self.status == STATUS_ADOPTED and not self.module:
            raise ValueError(f"{self.key}: ADOPTED 인데 구현 위치가 없다")
        if self.status == STATUS_BLOCKED and not self.blocked_by:
            raise ValueError(f"{self.key}: BLOCKED 인데 사유가 없다")
        if self.validated and self.status != STATUS_ADOPTED:
            raise ValueError(f"{self.key}: 구현되지 않은 방법이 검증될 수 없다")


METHODS: tuple[Method, ...] = (
    # ── RES-05 펀더멘털 ────────────────────────────────────────────────────
    Method(
        key="piotroski_f_score",
        name="Piotroski F-Score (재무건전성 9신호)",
        analyst="RES-05",
        citation="Piotroski, J. (2000). Value Investing: The Use of Historical "
                 "Financial Statement Information... Journal of Accounting Research 38.",
        status=STATUS_ADOPTED,
        module="evidence/fundamental_scores.py:f_score",
        inputs=("당기순이익", "자산총계", "유동자산", "유동부채", "비유동부채",
                "매출액", "자본금"),
        partial_reason=(
            "9신호 중 6개만 계산한다. 현금흐름표(CFO)가 DART 주요계정 API 에 "
            "없어 CFO>0·발생액(CFO>ROA) 2개가 불가하고, 매출총이익이 없어 "
            "매출총이익률 변화 1개가 불가하다. 신주발행 신호는 자본금 증가로 "
            "대용한다(무상증자·액면분할이 섞이는 근사). 점수는 반드시 "
            "'x/6(가용)' 으로 표기한다 - 6점을 9점 척도로 읽으면 과소평가다."),
        note="백로그: DART 현금흐름표(fnlttSinglAcntAll) 수집하면 9/9 가능",
    ),
    Method(
        key="altman_z_score",
        name="Altman Z-Score (부실 예측)",
        analyst="RES-05",
        citation="Altman, E. (1968). Financial Ratios, Discriminant Analysis and "
                 "the Prediction of Corporate Bankruptcy. Journal of Finance 23.",
        status=STATUS_ADOPTED,
        module="evidence/fundamental_scores.py:altman_z",
        inputs=("유동자산", "유동부채", "이익잉여금", "법인세차감전 순이익",
                "자산총계", "부채총계", "매출액", "시가총액(가격x주식수)"),
        partial_reason=(
            "상장 제조업 원식(1968)의 X4 는 자기자본 시가/부채 장부가다. "
            "발행주식수를 아직 보관하지 않아 X4 를 자본총계/부채총계(장부가) "
            "로 대용한다 - 원식보다 보수적으로 나오는 경향이 있어 판정 구간을 "
            "그대로 쓰지 않고 '참고' 로만 표기한다."),
        note="백로그: reference.instruments 에 상장주식수 적재 시 원식 복원",
    ),
    Method(
        key="beneish_m_score",
        name="Beneish M-Score (이익조작 탐지)",
        analyst="RES-05",
        citation="Beneish, M. (1999). The Detection of Earnings Manipulation. "
                 "Financial Analysts Journal 55(5).",
        status=STATUS_BLOCKED,
        inputs=("매출채권", "매출총이익", "감가상각비", "판관비", "CFO"),
        blocked_by="8개 변수 중 5개가 DART 주요계정에 없다(매출채권·감가상각비 등). "
                   "전체 재무제표 API 수집이 선행돼야 한다",
    ),
    # ── RES-03 미시구조 ────────────────────────────────────────────────────
    Method(
        key="amihud_illiquidity",
        name="Amihud 비유동성 (|수익률|/거래대금)",
        analyst="RES-03",
        citation="Amihud, Y. (2002). Illiquidity and stock returns: cross-section "
                 "and time-series effects. Journal of Financial Markets 5(1).",
        status=STATUS_ADOPTED,
        module="evidence/liquidity.py:amihud_illiquidity",
        inputs=("일별 종가", "일별 거래대금"),
        note="충격비용의 대용치 - 값이 클수록 같은 거래대금이 가격을 더 민다",
    ),
    Method(
        key="roll_effective_spread",
        name="Roll 유효 스프레드 (연속 가격변화 자기공분산)",
        analyst="RES-03",
        citation="Roll, R. (1984). A Simple Implicit Measure of the Effective "
                 "Bid-Ask Spread in an Efficient Market. Journal of Finance 39(4).",
        status=STATUS_ADOPTED,
        module="evidence/liquidity.py:roll_spread",
        inputs=("연속 종가",),
        partial_reason="자기공분산이 양수면 모형이 성립하지 않는다 - 그 경우 "
                       "0 이나 임의값으로 채우지 않고 None(판정 불가)을 낸다.",
    ),
    Method(
        key="vpin_toxicity",
        name="VPIN (주문흐름 독성)",
        analyst="RES-03",
        citation="Easley, D., López de Prado, M., O'Hara, M. (2012). Flow Toxicity "
                 "and Liquidity in a High-Frequency World. Review of Financial Studies 25.",
        status=STATUS_CANDIDATE,
        inputs=("체결 단위 거래량", "거래량 버킷"),
        note="틱 데이터는 있으나 거래량 버킷·분류 구현이 선행. 장중 실측 후 판단",
    ),
    # ── RES-07 레짐 ────────────────────────────────────────────────────────
    Method(
        key="markov_regime_switching",
        name="2상태 마르코프 국면전환 (수익률·변동성)",
        analyst="RES-07",
        citation="Hamilton, J. (1989). A New Approach to the Economic Analysis of "
                 "Nonstationary Time Series and the Business Cycle. Econometrica 57(2).",
        status=STATUS_CANDIDATE,
        inputs=("지수 일별 수익률 2년+",),
        note="statsmodels.tsa.regime_switching 설치돼 있어 구현 가능. 다만 KOSPI "
             "일별 히스토리 적재 길이가 짧아 표본이 쌓인 뒤 도입한다",
    ),
    Method(
        key="vkospi_fear_gauge",
        name="VKOSPI 공포 게이지 (내재변동성 국면)",
        analyst="RES-07",
        citation="Whaley, R. (2000). The Investor Fear Gauge. Journal of "
                 "Portfolio Management 26(3). (VIX 계열 지수의 해석 근거)",
        status=STATUS_ADOPTED,
        module="collectors/volatility_index_collector.py",
        inputs=("LS t1511 업종코드 205",),
        note="등락 단면·SMA 상회가 '실현된 가격의 뒷모습'이라면 VKOSPI 는 "
             "옵션시장이 보는 앞으로 30일이다 - 축이 달라 같은 하락도 국면이 "
             "갈린다. 실측 2026-08-02: 84.35, 52주 18.03~97.99",
    ),
    Method(
        key="fear_greed_composite",
        name="공포탐욕 복합지수 (한국 시장판)",
        analyst="RES-07",
        citation="CNN Business Fear & Greed Index 방법론(7개 구성요소 백분위 "
                 "평균)을 국내 데이터로 재구성. 구성요소 근거는 각 하위 지표 인용.",
        status=STATUS_CANDIDATE,
        inputs=("등락 단면", "SMA 대비 위치", "실현변동성", "K200 풋콜비율"),
        note="풋콜비율은 derivatives_collector 가 적재 중 - 월요일 첫 장중 "
             "스냅샷 이후 구성 가능. 구성요소를 백분위로 정규화해 평균한다",
    ),
    # ── RES-04 기술 ────────────────────────────────────────────────────────
    Method(
        key="time_series_momentum",
        name="시계열 모멘텀 (자기 과거수익 부호)",
        analyst="RES-04",
        citation="Moskowitz, T., Ooi, Y.H., Pedersen, L. (2012). Time series "
                 "momentum. Journal of Financial Economics 104(2).",
        status=STATUS_CANDIDATE,
        inputs=("일별 종가 12개월",),
        note="퀀트본부 MOM-20 과 중복되지 않게 역할을 나눈다 - 여기서는 신호가 "
             "아니라 '현재 상태 서술'로 쓴다",
    ),
    Method(
        key="deflated_sharpe",
        name="Deflated Sharpe Ratio (다중검정 보정)",
        analyst="RES-04",
        citation="Bailey, D., López de Prado, M. (2014). The Deflated Sharpe Ratio. "
                 "Journal of Portfolio Management 40(5).",
        status=STATUS_CANDIDATE,
        inputs=("전략 수익률", "시행 횟수"),
        note="퀀트본부 walk_forward 판정에 붙이는 것이 더 맞다 - 소유 이관 검토",
    ),
    # ── RES-06 감성 ────────────────────────────────────────────────────────
    Method(
        key="stale_news_detection",
        name="구문 반복(진부한 뉴스) 탐지",
        analyst="RES-06",
        citation="Tetlock, P. (2011). All the News That's Fit to Reprint: Do "
                 "Investors React to Stale Information? Review of Financial Studies 24.",
        status=STATUS_CANDIDATE,
        inputs=("기사 본문·제목 유사도",),
        note="스토리 군집(story_cluster)이 이미 중복을 묶는다 - 여기에 '새로운 "
             "정보량' 축을 더하는 형태로 확장",
    ),
    # ── RES-09 지정학 ──────────────────────────────────────────────────────
    Method(
        key="gpr_threat_act_split",
        name="GPR 위협/실제 분리 판독",
        analyst="RES-09",
        citation="Caldara, D., Iacoviello, M. (2022). Measuring Geopolitical Risk. "
                 "American Economic Review 112(4).",
        status=STATUS_ADOPTED,
        module="agents/geopolitical_analyst.py:compute_geo_readout",
        inputs=("GPRD", "GPRD_THREAT", "GPRD_ACT"),
        note="논문 자체가 위협/실제를 분리 제공한다 - 우리는 driver 로 판정",
    ),
    Method(
        key="local_projection_irf",
        name="지역투영 충격반응 (지정학 충격 -> KOSPI)",
        analyst="RES-09",
        citation="Jordà, Ò. (2005). Estimation and Inference of Impulse Responses "
                 "by Local Projections. American Economic Review 95(1).",
        status=STATUS_CANDIDATE,
        inputs=("GPR 일별", "KOSPI 일별 수익률 5년+"),
        note="'지정학 충격이 실제로 한국 시장에 얼마나 왔나'를 수치로 만든다 - "
             "전달 경로를 가설이 아니라 추정치로 말할 수 있게 되는 지점",
    ),
)


def by_analyst(status: str | None = None) -> dict[str, list[Method]]:
    out: dict[str, list[Method]] = {a: [] for a in ANALYSTS}
    for m in METHODS:
        if status is None or m.status == status:
            out[m.analyst].append(m)
    return out


def adopted_for(analyst: str) -> list[Method]:
    return [m for m in METHODS if m.analyst == analyst and m.status == STATUS_ADOPTED]


def summary_line() -> str:
    n = {s: sum(1 for m in METHODS if m.status == s) for s in _STATUSES}
    v = sum(1 for m in METHODS if m.validated)
    return (f"방법 {len(METHODS)}개 - 도입 {n[STATUS_ADOPTED]} / 후보 "
            f"{n[STATUS_CANDIDATE]} / 데이터막힘 {n[STATUS_BLOCKED]} / "
            f"성과검증 {v}")


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

def _check_keys_unique():
    keys = [m.key for m in METHODS]
    assert len(keys) == len(set(keys)), "method key 중복"
    print("  키 유일성                OK")


def _check_invariants_enforced():
    import dataclasses

    base = dict(key="x", name="n", analyst="RES-05", citation="c",
                status=STATUS_ADOPTED, module="m.py:f")
    Method(**base)                                    # 정상
    for bad, why in (
        (dict(base, citation="  "), "인용 없는 등재"),
        (dict(base, module=None), "ADOPTED 인데 구현 없음"),
        (dict(base, status=STATUS_BLOCKED, module=None), "BLOCKED 인데 사유 없음"),
        (dict(base, status="ㅇㅇ"), "알 수 없는 status"),
        (dict(base, analyst="RES-99"), "알 수 없는 분석가"),
        (dict(base, status=STATUS_CANDIDATE, module=None, validated=True),
         "미구현인데 검증됨"),
    ):
        try:
            Method(**bad)
            raise AssertionError(f"통과하면 안 되는 등재: {why}")
        except ValueError:
            pass
    assert dataclasses.is_dataclass(Method)
    print("  등재 불변식              OK")


def _check_partial_documented():
    """부분 구현은 무엇이 빠졌는지 반드시 적는다 - 이름만 빌리는 것을 막는다."""
    for m in METHODS:
        if m.key in ("piotroski_f_score", "altman_z_score"):
            assert m.partial_reason and len(m.partial_reason) > 30, m.key
    # BLOCKED 는 사유에 '무엇이 없는지'가 있어야 한다
    for m in METHODS:
        if m.status == STATUS_BLOCKED:
            assert "없" in m.blocked_by or "선행" in m.blocked_by, m.key
    print("  부분·차단 사유 명시      OK")


def _check_no_unearned_validation():
    """도입 != 검증. calibration 표본 전에는 validated 가 켜지면 안 된다."""
    assert not any(m.validated for m in METHODS), \
        "아직 성과 표본이 없다 - validated 를 켜면 자기충족 서사가 된다"
    print("  검증 주장 없음           OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(f"{REGISTRY_VERSION} 자체 점검")
    _check_keys_unique()
    _check_invariants_enforced()
    _check_partial_documented()
    _check_no_unearned_validation()
    print(f"방법론 레지스트리 4개 영역 통과. {summary_line()}\n")

    for analyst, ms in by_analyst().items():
        if not ms:
            continue
        print(f"[{analyst}]")
        for m in sorted(ms, key=lambda x: (x.status, x.key)):
            mark = {"ADOPTED": "●", "CANDIDATE": "○", "BLOCKED": "×"}[m.status]
            print(f"  {mark} {m.name}")
            if m.status == STATUS_BLOCKED:
                print(f"      막힘: {m.blocked_by[:80]}")
            elif m.partial_reason:
                print(f"      부분: {m.partial_reason[:80]}")
