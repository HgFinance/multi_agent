#!/usr/bin/env python3
"""피처 분석가 - **전략가에게 넘기기 전에 피처 하나하나를 따로 검정한다.**

소유: 재일 (퀀트·백테스트본부 QNT)
근거: 재일님 지시 2026-08-13 "피쳐 엔지니어링 도입, 미시구조 데이터 있으니까"

▶ 왜 이 공정이 따로 있어야 하나 (López de Prado, 메타전략 패러다임)
  "성공한 모든 퀀트 회사는 메타전략 패러다임을 적용한다 - 조립라인의 과업을
  하위 과업으로 나누고 **각각의 품질을 독립적으로 측정·감시**한다." 그가 제시한
  생산 사슬은 데이터 큐레이터 -> **피처 분석가** -> 전략가 -> 백테스트 전문가
  -> 배포팀 -> 감독이다.

  우리 공장에는 그 칸이 비어 있었다. 원시 봉에서 곧장 전략 템플릿 8종으로
  건너뛰었고, 그래서 **어휘가 8개에 잠겼다** - 새 리드가 와도 표현할 자리가
  없었다(실측: 오버나이트 수익·MAX 효과 리드가 대응 어휘 없어 묶여 있었다).

  피처를 따로 검정하면 두 가지가 생긴다.
    ① 전략이 실패했을 때 **신호가 없어서인지 조합이 나빠서인지** 갈린다.
       지금은 둘이 섞여서 "momentum 이 안 된다" 로만 남는다.
    ② 전략가가 고를 수 있는 **어휘가 열린다** - 검정을 통과한 피처는
       그 자체로 새 엣지 후보다.

▶ 검정 기준은 관대하지 않다
  단일 피처의 t 값 문턱은 **3.0** 이다(`signal_ic.MIN_T_STAT`). 관행인 2.0 이
  아닌 이유는 다중검정 문헌 때문이다 - 우리는 피처를 여러 개 동시에 재고,
  2.0 을 쓰면 순수한 잡음에서도 통과가 나온다.

  그리고 **부호를 본다.** |t| 로 재면 역방향 신호가 "통과" 로 찍힌다
  (실측으로 방향 정규화 전 음의 t 가 통과로 나왔다). 역방향은
  역방향이라고 적는다 - 그것도 정보다.

▶ PIT 를 같이 싣는다
  이 피처의 원천 구간은 이관분이 섞여 있고, `microstructure_builder` 가
  "이관 구간에서는 PIT 를 주장하지 않는다" 고 못박아 뒀다. IC 가 아무리 좋아도
  PIT 가 아니면 **거래 가능한 신호가 아니다.** 카탈로그에 그 사실을 함께 싣지
  않으면 좋은 숫자만 보고 전략을 세우게 된다.

▶ 못 잰 것을 0 으로 채우지 않는다
  그날 호가가 없던 종목의 스프레드는 0 이 아니라 없음이다. 순위에서 빼고
  `n_names` 에 남긴다 - 0 으로 채우면 거래가 없던 종목이 1등이 된다.

사용
  python pipeline/feature_catalog.py              # 자체 점검
  python pipeline/feature_catalog.py --measure    # 원장에 대고 검정
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

MODULE_VERSION = "quant-feature-catalog-v1"

# 검정에 필요한 최소 표본. 이보다 적으면 **판정하지 않는다**(통과도 탈락도 아님).
MIN_PERIODS = 8
MIN_NAMES = 30

# `market.microstructure_features` 의 피처. (컬럼, 가설 방향, 메커니즘)
#   방향 +1 = 값이 클수록 미래 수익이 높다는 가설
#   방향 -1 = 값이 클수록 미래 수익이 낮다는 가설
#   방향  0 = 방향 가설 없음(양쪽 다 열어 두고 잰다)
MICRO_FEATURES = (
    ("order_flow_imbalance", +1,
     "매수 주도 체결이 우세하면 그 압력이 다음 구간까지 이어진다(주문흐름 관성)"),
    ("depth_imbalance", +1,
     "최우선호가 잔량이 매수 쪽으로 기울면 단기 상승 압력이다"),
    ("depth_imbalance_l1", +1,
     "최우선호가 잔량 불균형은 즉시 체결 가능한 유동성 압력을 나타낸다"),
    ("depth_imbalance_l10", +1,
     "10단계 누적 잔량 불균형은 표면 아래의 깊은 유동성 지지를 나타낸다"),
    ("depth_imbalance_slope", 0,
     "최우선호가와 깊은 호가의 방향 차이는 호가장 공간 구조의 취약성을 나타낸다"),
    ("size_weighted_ofi", +1,
     "큰 체결이 주도한 방향은 작은 체결 수만 많은 흐름보다 정보성이 높다는 가설"),
    ("book_depth_notional_l1", 0,
     "최우선호가의 가격×잔량 합계는 즉시 주문충격을 흡수할 유동성 수용력이다"),
    ("book_depth_notional_l10", 0,
     "10단계 가격×잔량 합계는 표면 아래까지 포함한 전체 호가 수용력이다"),
    ("spread_bps", -1,
     "스프레드가 넓으면 거래비용이 크고, 비용을 넘는 초과수익이 남기 어렵다"),
    ("trade_intensity", 0,
     "체결 빈도는 정보 도착의 대리변수다 - 방향은 사전에 정하지 않는다"),
    ("realized_volatility", -1,
     "고변동 종목이 위험 대비 낮은 수익을 낸다(저변동 이상현상)"),
    ("volume_zscore", 0,
     "거래량 급증은 정보 유입일 수도 과열일 수도 있다 - 방향 미정"),
)


@dataclass
class FeatureQuality:
    """피처 하나의 독립 검정 결과. **판정하지 못한 것을 탈락으로 적지 않는다.**"""

    name: str
    horizon: int
    direction: int = 0
    mechanism: str = ""
    ic: float | None = None
    t_stat: float | None = None
    periods: int = 0
    avg_names: float = 0.0
    coverage_pct: float | None = None
    pit: str = ""               # 원천의 PIT 근거. 없으면 거래 신호가 아니다
    note: str = ""

    @property
    def judged(self) -> bool:
        """검정이 성립했나. 표본이 모자라면 통과도 탈락도 아니다."""
        return (self.t_stat is not None and self.periods >= MIN_PERIODS
                and self.avg_names >= MIN_NAMES)

    @property
    def verdict(self) -> str:
        """통과 / 역방향 / 미달 / 미판정. **|t| 로 재지 않는다.**"""
        if not self.judged:
            return "미판정"
        try:
            from signal_ic import MIN_T_STAT      # noqa: PLC0415 (실행면이 정본)
        except Exception:  # noqa: BLE001
            MIN_T_STAT = 3.0
        t = self.t_stat
        if t >= MIN_T_STAT:
            return "통과"
        if t <= -MIN_T_STAT:
            return "역방향"
        return "미달"

    @property
    def pit_known(self) -> bool:
        """PIT 근거를 **읽기는 했나.** `?` 는 못 읽었다는 뜻이다."""
        return bool(self.pit) and self.pit != "?"

    @property
    def tradeable(self) -> bool:
        """거래 가능한 신호인가. **PIT 근거가 없으면 아무리 좋아도 아니다.**

        못 읽은 것도 거래 불가다 - 다만 사유가 다르므로 `pit_known` 으로
        갈라 적는다. "없다" 와 "모른다" 를 섞으면 고칠 것이 안 보인다.
        """
        return (self.verdict in ("통과", "역방향") and self.pit_known
                and not self.pit.upper().startswith("NONE")
                and self.pit.upper() != "UNKNOWN")

    def as_dict(self) -> dict:
        return {"name": self.name, "horizon": self.horizon,
                "verdict": self.verdict, "tradeable": self.tradeable,
                "ic": self.ic, "t_stat": self.t_stat,
                "periods": self.periods, "avg_names": self.avg_names,
                "coverage_pct": self.coverage_pct, "pit": self.pit,
                "direction": self.direction, "note": self.note}


@dataclass
class Catalog:
    """검정을 마친 피처 목록. 전략가는 **여기 있는 것만** 고를 수 있다."""

    features: list = field(default_factory=list)
    horizon: int = 5
    feature_set_version: str = ""

    def passing(self) -> list:
        return [f for f in self.features if f.verdict in ("통과", "역방향")]

    def usable(self) -> list:
        """전략가에게 넘길 것. 검정 통과 **그리고** PIT 근거가 있는 것."""
        return [f for f in self.features if f.tradeable]

    def summary(self) -> str:
        if not self.features:
            return "피처 카탈로그: 잰 것이 없다"
        version = f" {self.feature_set_version}" if self.feature_set_version else ""
        lines = [f"[피처 카탈로그{version} h={self.horizon}일] "
                 f"검정 {len(self.features)}종 · 통과/역방향 {len(self.passing())}종 "
                 f"· 거래가능 {len(self.usable())}종"]
        for f in sorted(self.features, key=lambda x: -(x.t_stat or 0)):
            t = f"t={f.t_stat:+.2f}" if f.t_stat is not None else "t=미측정"
            ic = f"IC={f.ic:+.4f}" if f.ic is not None else "IC=미측정"
            lines.append(
                f"  {f.name:<22} {f.verdict:<5} {ic} {t} "
                f"기간 {f.periods} 폭 {f.avg_names:.0f}"
                + (f" PIT={f.pit}" if f.pit_known
                   else " **PIT 못 읽음**" if f.pit == "?"
                   else " **PIT 근거 없음**"))
            if f.note:
                lines.append(f"      {f.note}")
        if any(f.pit == "?" for f in self.features):
            lines.append("  ▶ **PIT 근거를 읽지 못했다.** 이것은 '근거가 없다' 와 "
                         "다르다 - 조회를 먼저 고쳐라. 못 읽은 것을 없는 것으로 "
                         "적으면 멀쩡한 피처가 전부 거래 불가로 찍힌다.")
        elif not self.usable():
            lines.append("  ▶ **거래 가능한 피처가 0 종이다.** 검정을 통과해도 "
                         "PIT 근거가 없으면 그 성적은 미래를 본 값일 수 있다 - "
                         "원천의 pit_provenance 를 먼저 세워라.")
        return "\n".join(lines)


def rank_ic(rows_by_date: dict, fwd_by_date: dict, *, direction: int = 0):
    """날짜별 (피처값, 미래수익) -> 횡단면 순위상관 시계열.

    **겹치지 않는 표본만 쓴다** - 호출부가 날짜를 이미 h 간격으로 잘라 준다.
    겹치면 t 가 부풀어 없는 유의성이 생긴다(실측 1.40 -> 3.72).
    """
    from signal_ic import spearman              # noqa: PLC0415 (실행면이 정본)

    out, names = [], []
    for d in sorted(rows_by_date):
        f, r = rows_by_date.get(d) or {}, fwd_by_date.get(d) or {}
        common = [s for s in f if s in r and f[s] is not None and r[s] is not None]
        if len(common) < MIN_NAMES:
            continue
        rho = spearman([float(f[s]) for s in common],
                       [float(r[s]) for s in common])
        if rho is None:
            continue
        out.append(rho * (direction if direction else 1))
        names.append(len(common))
    return out, names


def summarize(ic_series, names) -> tuple:
    """(평균 IC, t 값, 기간 수, 평균 폭). 표본이 없으면 (None, None, 0, 0).

    ▶ **분산이 사실상 0 이면 t 를 만들지 않는다** (2026-08-13, 자체점검이 잡았다)
      `sd <= 0` 만 막으면 부동소수 때문에 상수 계열의 분산이 정확히 0 이 아니라
      1e-35 로 나오고, 그러면 **t 가 1e17 이 되어 최고의 피처로 찍힌다.**
      IC 가 매 기간 똑같다는 것은 신호가 무한히 강하다는 뜻이 아니라 표본이
      퇴화했다는 뜻이다(같은 값을 반복해 읽었거나, 순위가 안 바뀌었거나).
      절대·상대 두 기준을 함께 쓴다 - IC 는 보통 0.0x 라 절대 기준만으로는
      스케일이 작은 계열에서 또 새어 나간다.
    """
    n = len(ic_series or [])
    if n < 2:
        return (None, None, n, (sum(names) / len(names)) if names else 0.0)
    mu = sum(ic_series) / n
    var = sum((x - mu) ** 2 for x in ic_series) / (n - 1)
    sd = var ** 0.5
    floor = max(1e-12, 1e-9 * abs(mu))
    t = None if sd <= floor else mu / (sd / (n ** 0.5))
    return (mu, t, n, sum(names) / len(names))


# ── 원장 조회 ────────────────────────────────────────────────────────────────

_SQL_FEATURE = """
select event_time::date d, instrument_id, {col}
  from market.microstructure_features
 where {col} is not null
   and feature_set_version = %s
   and event_time::date = any(%s)
"""

_SQL_FWD = """
with px as (
  select instrument_id, bucket_time::date d, close
    from market.market_bars
   where interval_code = '1D' and close > 0
     and bucket_time::date >= %s and bucket_time::date <= %s
)
select a.d, a.instrument_id, b.close / a.close - 1.0
  from px a join px b
    on b.instrument_id = a.instrument_id and b.d = %s::date + (a.d - %s::date)
 where false
"""


def _dates(cur, horizon: int, feature_set_version: str) -> list:
    cur.execute("""select distinct event_time::date
                     from market.microstructure_features
                    where feature_set_version = %s order by 1""",
                (feature_set_version,))
    all_days = [r[0] for r in cur.fetchall()]
    # 겹치지 않게 h 간격으로 자른다. 마지막 h 일은 미래수익이 없으므로 뺀다.
    return all_days[:-horizon:horizon] if len(all_days) > horizon else []


def _forward_returns(cur, days: list, horizon: int) -> dict:
    """기준일 -> {종목: h거래일 후 수익률}. **거래일 기준**으로 센다."""
    cur.execute("""
        select instrument_id, bucket_time::date d, close
          from market.market_bars
         where interval_code = '1D' and close > 0
           and bucket_time::date between %s and %s
         order by instrument_id, d""", (min(days), max(days) + __import__(
            "datetime").timedelta(days=horizon * 3)))
    series: dict = {}
    for iid, d, close in cur.fetchall():
        series.setdefault(iid, []).append((d, float(close)))
    out: dict = {d: {} for d in days}
    for iid, pts in series.items():
        idx = {d: i for i, (d, _) in enumerate(pts)}
        for d in days:
            i = idx.get(d)
            if i is None or i + horizon >= len(pts):
                continue
            a, b = pts[i][1], pts[i + horizon][1]
            if a > 0:
                out[d][iid] = b / a - 1.0
    return out


def _pit_of(conn, cur, days) -> str:
    """원천의 PIT 근거. **못 읽은 것과 근거가 없는 것을 구분한다.**

    ▶ 두 가지를 여기서 틀렸다 (2026-08-13, 첫 실측에서 드러났다)
      ① 예외를 삼키면서 **롤백을 안 했다.** 트랜잭션이 오염된 채로 다음
         조회가 돌아 첫 피처(`order_flow_imbalance`)가 `InFailedSqlTransaction`
         으로 연쇄 실패했다. 남의 실패를 자기 실패로 보고한 셈이다.
      ② 실패를 빈 문자열로 돌려줬다. 카탈로그는 빈 문자열을 "PIT 근거 없음"
         으로 읽으므로, **못 읽은 것이 없는 것으로 둔갑**해 전 피처에
         "거래 불가" 가 찍혔다. 미측정 != 0 을 내가 어겼다.
      ③ `basis` 는 **jsonb 다** - `coalesce(basis,'')` 로 읽으려다 죽은
         것이 ①의 실제 원인이었다. 진짜 PIT 서술자는 `observed_at_kind`
         (NONE/EVENT/RECEIVED)와 `derivation` 이다.

    선언은 **구간별**이라 우리가 잰 창과 겹치는지를 봐야 한다. 전체에 한 값을
    붙이면 이관 구간과 실시간 구간이 섞인다 - 실측으로 미시구조 원천은
    2026-05-18~07-10 이 `observed_at_kind=NONE` 이고 원장이 직접
    "PIT 없음 - 이 구간은 시점 재현 실험에 쓸 수 없다" 고 적어 놨다.
    """
    try:
        cur.execute("""
            select coalesce(observed_at_kind, ''), source_table,
                   range_start, range_end
              from market.pit_provenance
             where range_end >= %s and range_start <= %s
             order by (coalesce(observed_at_kind,'') = 'NONE') desc""",
                    (min(days), max(days)))
        rows = cur.fetchall()
    except Exception:  # noqa: BLE001
        conn.rollback()          # 오염된 트랜잭션을 다음 조회에 넘기지 않는다
        return "?"               # 못 읽었다 - 없다고 말하지 않는다
    if not rows:
        # 선언이 없는 구간이다. **없음도 아니고 있음도 아니다** - 아무도
        # 이 구간의 관측 시각을 확인해 주지 않았다는 뜻이므로 주장하지 않는다.
        return "?"
    kind, src, lo, hi = rows[0]
    if str(kind).upper() == "NONE":
        # 겹치는 구간에 PIT 없음 선언이 하나라도 있으면 그 창은 시점 재현이
        # 안 된다. 좋은 IC 가 나와도 미래를 본 값일 수 있다.
        return f"NONE({src} {lo}~{hi})"
    return str(kind) or "?"


def measure(conn, *, horizon: int = 5, features=MICRO_FEATURES,
            feature_set_version: str = "ms-daily-v5") -> Catalog:
    """원장에 대고 피처를 하나씩 검정한다."""
    cat = Catalog(horizon=horizon, feature_set_version=feature_set_version)
    cur = conn.cursor()
    days = _dates(cur, horizon, feature_set_version)
    if not days:
        return cat
    fwd = _forward_returns(cur, days, horizon)
    pit = _pit_of(conn, cur, days)

    for col, direction, mech in features:
        try:
            cur.execute(_SQL_FEATURE.format(col=col), (feature_set_version, days))
            by_date: dict = {}
            for d, iid, v in cur.fetchall():
                by_date.setdefault(d, {})[iid] = v
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            cat.features.append(FeatureQuality(
                col, horizon, direction, mech,
                note=f"조회 실패: {type(exc).__name__}: {str(exc)[:90]}"))
            continue
        series, names = rank_ic(by_date, fwd, direction=direction)
        ic, t, n, avg = summarize(series, names)
        cat.features.append(FeatureQuality(
            col, horizon, direction, mech, ic=ic, t_stat=t,
            periods=n, avg_names=avg, pit=pit,
            note=("방향 가설 없음 - 부호를 그대로 읽는다" if direction == 0 else "")))
    return cat


# ── 자체 점검 ────────────────────────────────────────────────────────────────

def _check_reverse_signal_is_not_a_pass():
    """**역방향을 통과로 찍지 않는다.** |t| 로 재면 뒤집힌 신호가 합격한다.

    실측: 방향 정규화 전 음의 t 가 통과로 나왔다. 역방향은 역방향으로
    적어야 전략가가 부호를 뒤집어 쓸 수 있다 - 그것도 정보다.
    """
    good = FeatureQuality("x", 5, t_stat=4.2, periods=12, avg_names=100)
    bad = FeatureQuality("y", 5, t_stat=-7.09, periods=12, avg_names=100)
    weak = FeatureQuality("z", 5, t_stat=2.1, periods=12, avg_names=100)
    assert good.verdict == "통과", good.verdict
    assert bad.verdict == "역방향", bad.verdict
    assert weak.verdict == "미달", "관행 2.0 을 문턱으로 쓰면 잡음이 통과한다"


def _check_thin_sample_is_not_a_verdict():
    """**표본이 모자라면 통과도 탈락도 아니다.** 미달로 적으면 좋은 피처를 버린다."""
    thin = FeatureQuality("x", 5, t_stat=9.9, periods=3, avg_names=100)
    narrow = FeatureQuality("y", 5, t_stat=9.9, periods=12, avg_names=8)
    assert not thin.judged and thin.verdict == "미판정"
    assert not narrow.judged and narrow.verdict == "미판정"
    assert FeatureQuality("z", 5).verdict == "미판정"


def _check_pit_gates_tradeability():
    """**PIT 근거가 없으면 아무리 좋아도 거래 신호가 아니다.**

    미시구조 원천은 이관분이 섞여 있고 빌더가 "이관 구간에서는 PIT 를 주장하지
    않는다" 고 못박았다. 좋은 IC 만 보고 전략을 세우면 미래를 본 값으로
    설계하게 된다.
    """
    ok = FeatureQuality("x", 5, t_stat=4.0, periods=12, avg_names=100,
                        pit="EXCHANGE_TIMESTAMP")
    no_pit = FeatureQuality("x", 5, t_stat=4.0, periods=12, avg_names=100, pit="")
    none_pit = FeatureQuality("x", 5, t_stat=4.0, periods=12, avg_names=100,
                              pit="NONE")
    assert ok.tradeable and not no_pit.tradeable and not none_pit.tradeable
    # 카탈로그가 그 사실을 크게 말한다 - 조용히 빠지면 아무도 안 본다
    c = Catalog(features=[no_pit], horizon=5)
    assert "거래 가능한 피처가 0 종" in c.summary()
    assert "PIT 근거 없음" in c.summary()

    # **"못 읽음" 과 "없음" 을 섞지 않는다** (2026-08-13, 첫 실측이 잡았다)
    #   `_pit_of` 가 롤백 없이 예외를 삼키고 빈 문자열을 돌려줬다. 카탈로그는
    #   그것을 "근거 없음" 으로 읽어 **멀쩡한 피처 6종 전부에 거래 불가**를
    #   찍었다. PIT 가 없는 게 아니라 내가 못 읽은 것이었다.
    unknown = FeatureQuality("x", 5, t_stat=4.0, periods=12, avg_names=100,
                             pit="?")
    assert not unknown.pit_known and not unknown.tradeable
    body = Catalog(features=[unknown], horizon=5).summary()
    assert "PIT 못 읽음" in body and "조회를 먼저 고쳐라" in body, body
    assert "근거가 없다' 와 다르다" in body


def _check_direction_flips_the_sign_not_the_magnitude():
    """방향 가설이 -1 이면 부호를 뒤집어 잰다. 크기를 건드리면 안 된다."""
    f = {"d1": {"a": 1.0, "b": 2.0, "c": 3.0}}
    r = {"d1": {"a": -0.01, "b": -0.02, "c": -0.03}}
    # 값이 클수록 수익이 낮다 -> 방향 -1 로 재면 양의 IC 가 나와야 한다
    up, _ = rank_ic(_wide(f), _wide(r), direction=-1)
    down, _ = rank_ic(_wide(f), _wide(r), direction=+1)
    assert up and down and abs(up[0] + down[0]) < 1e-9, (up, down)
    assert up[0] > 0 > down[0]


def _wide(d: dict) -> dict:
    """검정용 - 종목 수를 MIN_NAMES 이상으로 늘린 사본."""
    out = {}
    for day, m in d.items():
        big = dict(m)
        base = list(m.items())
        for i in range(MIN_NAMES):
            k, v = base[i % len(base)]
            big[f"{k}_{i}"] = v + i * 1e-6 * (1 if v >= 0 else -1)
        out[day] = big
    return out


def _check_missing_is_dropped_not_zeroed():
    """**못 잰 값을 0 으로 채우지 않는다.** 채우면 거래 없던 종목이 1등이 된다."""
    f = _wide({"d1": {"a": 1.0, "b": 2.0}})
    f["d1"]["ghost"] = None
    r = _wide({"d1": {"a": 0.01, "b": 0.02}})
    r["d1"]["ghost"] = 0.5          # 없는 피처인데 수익은 좋은 유령
    series, names = rank_ic(f, r)
    assert series, "표본이 있는데 못 쟀다"
    assert names[0] == len(_wide({"d1": {"a": 1.0, "b": 2.0}})["d1"]), \
        "유령을 표본에 넣었다"


def _check_two_periods_minimum_for_t():
    """기간 1개로 t 를 만들지 않는다 - 표준편차가 없다."""
    ic, t, n, avg = summarize([0.05], [100])
    assert ic is None and t is None and n == 1 and avg == 100
    # **상수 계열은 t 가 없다.** 부동소수 때문에 분산이 정확히 0 이 아니라
    # 1e-35 로 나오면 t 가 1e17 이 되어 최고의 피처로 찍힌다(이 검사가 잡았다).
    ic2, t2, n2, _ = summarize([0.05, 0.05, 0.05], [10, 10, 10])
    assert n2 == 3 and ic2 is not None
    assert t2 is None, f"상수 계열인데 t 를 만들었다: {t2}"
    # 스케일이 작아도 진짜 변동이 있으면 t 가 나와야 한다(과잉 차단 금지)
    _, t3, _, _ = summarize([0.01, 0.03, 0.02, 0.04], [50] * 4)
    assert t3 is not None and 1.0 < t3 < 20.0, t3


def _selfcheck() -> int:
    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_reverse_signal_is_not_a_pass()
    print("  역방향은 통과가 아니다     OK")
    _check_thin_sample_is_not_a_verdict()
    print("  표본 부족 = 미판정         OK")
    _check_pit_gates_tradeability()
    print("  PIT 없으면 거래 불가       OK")
    _check_direction_flips_the_sign_not_the_magnitude()
    print("  방향은 부호만 뒤집는다     OK")
    _check_missing_is_dropped_not_zeroed()
    print("  결측은 빼고 센다           OK")
    _check_two_periods_minimum_for_t()
    print("  기간 1개로 t 안 만든다     OK")
    print("피처 카탈로그 6개 영역 통과.")
    return 0


def _cli(argv) -> int:
    if "--measure" not in argv:
        return _selfcheck()
    sys.path.insert(0, "/app/departments/01-research/collectors")
    import psycopg2                              # noqa: PLC0415
    from source_registry import load_project_env  # noqa: PLC0415

    h = int(argv[argv.index("--horizon") + 1]) if "--horizon" in argv else 5
    fsv = (argv[argv.index("--feature-set-version") + 1]
           if "--feature-set-version" in argv else "ms-daily-v5")
    conn = psycopg2.connect(load_project_env()["TIMESCALE_DATABASE_URL"],
                            connect_timeout=30)
    try:
        print(measure(conn, horizon=h, feature_set_version=fsv).summary())
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(_cli(sys.argv[1:]))
