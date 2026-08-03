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
from dataclasses import dataclass

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
            "9신호 중 8개를 계산한다(2026-08-02 현금흐름표 수집으로 6->8). "
            "남은 1개(매출총이익률 변화)는 매출총이익이 DART 어느 API 에도 "
            "표준 계정으로 없어 불가하다. 신주발행 신호는 자본금 증가로 "
            "대용한다(무상증자·액면분할이 섞이는 근사). 점수는 반드시 "
            "'x/8(가용)' 으로 표기한다 - 8점을 9점 척도로 읽으면 과소평가다. "
            "CFO 계정명은 회사마다 10가지로 갈려(실측) 총액만 고르는 규칙을 "
            "is_cfo_account 에 두었다 - '창출된 현금'·'자산부채 변동'은 총액이 "
            "아니라 섞이면 점수가 조용히 틀린다."),
        note="현금흐름 수집: collectors/opendart_cashflow.py (2019020, 회사당 1호출)",
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
        blocked_by="8개 변수 중 매출채권·감가상각비·판관비가 아직 없다. "
                   "CFO 는 2026-08-02 확보(opendart_cashflow) - 전체 재무제표를 "
                   "CF 뿐 아니라 BS/IS 까지 넓히면 나머지도 열린다",
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
        key="flight_to_quality",
        name="안전자산 도피 (국채 대비 주식 상대강도)",
        analyst="RES-07",
        citation="Baur, D.G., Lucey, B.M. (2009). Flights and Contagion - An "
                 "Empirical Analysis of Stock-Bond Correlations. Journal of "
                 "Financial Stability 5(4).",
        status=STATUS_ADOPTED,
        module="evidence/style_ratios.py",
        inputs=("LS t1511 업종 606 국채선물지수", "LS t1511 업종 101 KOSPI200"),
        note="같은 -1% 하락이어도 돈이 국채로 갔는지 아무 데도 안 갔는지는 다른 "
             "국면이다. 수준이 아니라 국채선물/KOSPI200 비율의 20거래일 상대강도로 "
             "본다. capability_audit 이 '접근 가능한데 미사용'으로 집어낸 계열",
    ),
    Method(
        key="style_rotation_ratio",
        name="스타일 로테이션 (배당·가치 대비 시장)",
        analyst="RES-07",
        citation="Fama, E.F., French, K.R. (1993). Common risk factors in the "
                 "returns on stocks and bonds. Journal of Financial Economics "
                 "33(1). (배당수익률을 가치 팩터의 시장 대용치로 사용)",
        status=STATUS_ADOPTED,
        module="evidence/style_ratios.py",
        inputs=("LS t1511 업종 213/214 고배당·배당성장", "업종 122 초대형주제외",
                "업종 101 KOSPI200"),
        note="고배당지수가 올랐다는 사실만으로는 아무 말도 못 한다 - 시장이 같이 "
             "올랐을 수 있다. 스타일은 KOSPI200 대비에서만 의미가 생기므로 비율로 "
             "계산하고, 날짜가 짝이 맞는 관측만 나눈다(분자·분모가 다른 날이면 "
             "그건 스타일이 아니라 하루치 등락이다)",
    ),
    Method(
        key="low_volatility_preference",
        name="저변동성 선호 (방어적 국면 관측)",
        analyst="RES-07",
        citation="Blitz, D.C., van Vliet, P. (2007). The Volatility Effect - "
                 "Lower Risk Without Lower Return. Journal of Portfolio "
                 "Management 34(1).",
        status=STATUS_ADOPTED,
        module="evidence/style_ratios.py",
        inputs=("LS t1511 업종 526 KRX 최소변동성지수", "업종 101 KOSPI200"),
        note="VKOSPI 가 '앞으로 무섭다'는 옵션시장의 예상이라면 최소변동성 상대강도는 "
             "'이미 방어로 옮겼다'는 실제 자금 이동이다 - 예상과 행동은 다른 축이다",
    ),
    Method(
        key="barrier_touch_probability",
        name="배리어 터치 확률 (주장의 사전 확률)",
        analyst="RES-07",
        citation="Harrison, J.M. (1985). Brownian Motion and Stochastic Flow "
                 "Systems. Wiley. (반사원리 기반 first-passage 확률)",
        status=STATUS_ADOPTED,
        module="evidence/forecast.py:touch_probability",
        inputs=("일별 종가 20+", "주장의 baseline·threshold·horizon"),
        partial_reason="드리프트 0·로그정규·실현변동성=미래변동성 세 가정이 전부 "
                       "근사다. 그래서 이 확률은 '정답'이 아니라 **비교 가능한 "
                       "기준선**이다 - 분석가가 이 기준선보다 잘 맞히는지가 "
                       "calibration 이 답할 질문이다. 라벨 주장(REGIME_FLIP 등)은 "
                       "이 모형의 대상이 아니라 확률을 내지 않는다.",
        note="확률이 없으면 Brier Score·Calibration Error 를 원리적으로 못 센다 - "
             "발동 여부만 세면 과신하는 분석가와 소심한 분석가가 구분되지 않는다",
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


def _check_adopted_module_exists():
    """ADOPTED 가 가리키는 파일과 함수가 **실제로 있는가**.

    2026-08-02 계획 대조에서 적발: amihud_illiquidity·roll_effective_spread 가
    ADOPTED 로 등재돼 있었는데 module 이 가리키는 evidence/liquidity.py 가
    저장소에 없었다. __post_init__ 은 module 문자열이 비었는지만 봤기 때문에
    **레지스트리가 실제 역량보다 부풀어 있었다** - 이 레지스트리가 막으려던
    바로 그 상태다("도입했다"와 "됐다고 적었다"의 혼동).

    파일 존재와 함수 심볼까지 본다. import 는 하지 않는다 - 무거운 의존
    (pydantic·psycopg2)을 끌어오면 레지스트리 점검이 환경에 묶인다.
    """
    import re as _re
    from pathlib import Path as _P

    base = _P(__file__).resolve().parent.parent      # departments/01-research
    missing_file, missing_func = [], []
    for m in METHODS:
        if m.status != STATUS_ADOPTED:
            continue
        path, _, func = (m.module or "").partition(":")
        p = base / path
        if not p.exists():
            missing_file.append(f"{m.key} -> {path}")
            continue
        if func:
            src = p.read_text(encoding="utf-8")
            if not _re.search(rf"^\s*(?:def|class)\s+{_re.escape(func)}\b",
                              src, _re.MULTILINE):
                missing_func.append(f"{m.key} -> {path}:{func}")

    assert not missing_file, (
        f"ADOPTED 인데 구현 파일이 없다: {missing_file} - 파일을 만들거나 "
        f"CANDIDATE 로 내려라(둘 중 하나이지 이대로 둘 수는 없다)")
    assert not missing_func, f"ADOPTED 인데 함수가 없다: {missing_func}"
    print(f"  ADOPTED 구현 실재        OK ({sum(1 for m in METHODS if m.status == STATUS_ADOPTED)}종)")


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
    _check_adopted_module_exists()
    _check_no_unearned_validation()
    print(f"방법론 레지스트리 5개 영역 통과. {summary_line()}\n")

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
