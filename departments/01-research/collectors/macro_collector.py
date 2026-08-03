#!/usr/bin/env python3
"""Sprint J2: 거시경제 지표 수집 (ECOS, FRED).

소유: 재일 (리서치본부)
근거: docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md 3.1(거시경제), 5.2, 8.2
      docs/03-data/RESEARCH_DATA_SOURCES_AND_LIBRARIES.md 5.x
      supabase/migrations/20260729000300_research_quant_strategy.sql
        (research.macro_series, research.macro_observations)

▶ vintage_date - 이 파일의 핵심 쟁점이다
  macro_observations 의 unique 키가 (series_id, period, vintage_date, revision) 다.
  거시지표는 발표 후 개정되므로 "어느 시점 기준의 값인가"가 PIT 재현에 필수다
  (가이드 5.2 - macro_observations 는 Revision Append).

  **FRED 는 vintage 를 실제로 준다.** 관측마다 realtime_start/realtime_end 가 있고
  ALFRED 기반이라 특정 시점에 알 수 있었던 값을 그대로 받는다. 그 값을 vintage_date 로 쓴다.

  **ECOS 는 vintage 를 주지 않는다.** 응답에 개정 이력이 없어 "지금 조회한 최신값"만
  얻는다. 그래서 vintage_date 를 **수집일**로 두고 metadata.vintage_source 에
  `collection_date` 를 남긴다 - FRED 의 `realtime_start` 와 의미가 다르다는 것을
  구분할 수 있어야 한다. ECOS 값으로 과거 시점 Backtest 를 하면 개정 후 수치를
  쓰게 되므로, 이 한계를 모른 채 쓰면 미래 정보가 새어든다.

▶ KOSIS 는 2026-07-31 키 갱신·실측 통과로 도입됐다(fetch_kosis). 한때 err=11 로
  제외했던 기록은 Registry NOT_AUTHORIZED_OBSERVED 주석에 남아 있다.
  Source Registry 의 NOT_AUTHORIZED_OBSERVED 에 기록했다.

자체 점검(호출 없음): python departments/01-research/collectors/macro_collector.py
실제 수집:            python departments/01-research/collectors/macro_collector.py --collect
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from source_registry import SourceRegistry, load_project_env  # noqa: E402

COLLECTOR_VERSION = "research-macro-collector-v1"

KST = timezone(timedelta(hours=9))

# frequency 는 NOT NULL 이다. ECOS cycle 과 FRED frequency 를 공통 값으로 정규화한다.
FREQ_DAILY = "DAILY"
FREQ_MONTHLY = "MONTHLY"
FREQ_QUARTERLY = "QUARTERLY"
FREQ_ANNUAL = "ANNUAL"

ECOS_CYCLE_TO_FREQ = {"D": FREQ_DAILY, "M": FREQ_MONTHLY, "Q": FREQ_QUARTERLY, "A": FREQ_ANNUAL}

VINTAGE_FROM_PROVIDER = "provider_realtime"   # FRED
VINTAGE_FROM_COLLECTION = "collection_date"   # ECOS - 개정 이력 없음


class MacroError(RuntimeError):
    """거시지표 수집 실패. 빈 결과로 바꾸지 않는다."""


@dataclass(frozen=True)
class SeriesSpec:
    """수집할 시계열 하나. 확장은 이 목록에 한 줄 추가로 끝난다."""

    source_code: str          # Source Registry 의 source_id
    external_series_code: str
    name: str
    frequency: str
    unit: str | None = None
    country_code: str | None = None
    # ECOS 전용. STAT_CODE 안에서 특정 항목만 고를 때 쓴다.
    item_name: str | None = None
    seasonal_adjustment: str | None = None


@dataclass(frozen=True)
class Observation:
    external_series_code: str
    period: date
    value: Decimal | None
    published_at: datetime
    observed_at: datetime
    vintage_date: date
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MacroResult:
    series: tuple[SeriesSpec, ...]
    observations: tuple[Observation, ...]
    skipped: int
    failures: tuple[str, ...] = ()

    def summary(self) -> str:
        by_src: dict[str, int] = {}
        for o in self.observations:
            src = o.metadata.get("source", "?")
            by_src[src] = by_src.get(src, 0) + 1
        s = f"시계열 {len(self.series)}개 / 관측 {len(self.observations)}건 {by_src}, 제외 {self.skipped}"
        if self.failures:
            s += f", ⚠ 실패 {len(self.failures)}"
        return s


# 수집 대상. 가이드 3.1의 "금리, 환율, 물가, 고용, 생산, 수출입" 중 실측으로 확인한 것만
# 넣는다. 검증 안 된 통계표 코드를 추측해서 넣으면 조용히 빈 결과가 된다.
SERIES: tuple[SeriesSpec, ...] = (
    SeriesSpec("ecos", "722Y001.0101000", "한국은행 기준금리", FREQ_MONTHLY,
               unit="연%", country_code="KR", item_name="한국은행 기준금리"),
    SeriesSpec("fred", "DGS10", "US 10-Year Treasury Yield", FREQ_DAILY,
               unit="percent", country_code="US"),
    SeriesSpec("fred", "DFF", "US Federal Funds Effective Rate", FREQ_DAILY,
               unit="percent", country_code="US"),
    SeriesSpec("fred", "CPIAUCSL", "US CPI All Urban Consumers", FREQ_MONTHLY,
               unit="index", country_code="US", seasonal_adjustment="SA"),
    # KOSIS (2026-07-31 키 유효 확인 - 가이드 3.1 물가 도메인)
    SeriesSpec("kosis", "101:DT_1J22042:0", "소비자물가 총지수 전년동월비", FREQ_MONTHLY,
               unit="%", country_code="KR", item_name="전년동월비(%)"),
    SeriesSpec("kosis", "101:DT_1J22042:4", "근원물가(식료품·에너지 제외) 전년동월비",
               FREQ_MONTHLY, unit="%", country_code="KR", item_name="전년동월비(%)"),
)


class _Limiter:
    def __init__(self, per_sec: float) -> None:
        self._i = 1.0 / per_sec
        self._last = 0.0

    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self._i:
            time.sleep(self._i - gap)
        self._last = time.monotonic()


def _get_json(url: str, *, label: str, timeout: int = 25) -> dict:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # 인증키가 URL 에 있는 API 라 응답 본문을 그대로 올리지 않는다.
        raise MacroError(f"{label} HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise MacroError(f"{label} 연결 실패: {e.reason}") from None
    except json.JSONDecodeError:
        raise MacroError(f"{label} 응답이 JSON 이 아니다") from None


def parse_value(raw) -> Decimal | None:
    """숫자 문자열을 Decimal 로. FRED 는 결측을 `.` 로 준다."""
    if raw is None:
        return None
    text = str(raw).strip().replace(",", "")
    if not text or text in (".", "-"):
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"값을 숫자로 읽을 수 없다: {raw!r}") from None


def parse_period(raw: str, frequency: str) -> date:
    """ECOS TIME / FRED date 를 기간 시작일로 정규화한다.

    기간 시작일을 쓰는 이유 - macro_observations.period 가 date 하나이고, 월/분기
    데이터를 종료일로 두면 발표 시점과 헷갈린다. 시작일이 관례상 명확하다.
    """
    text = (raw or "").strip()
    if not text:
        raise ValueError("기간이 비었다")

    # FRED: YYYY-MM-DD
    m = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if m:
        return date(int(m[1]), int(m[2]), int(m[3]))

    if frequency == FREQ_ANNUAL and re.fullmatch(r"\d{4}", text):
        return date(int(text), 1, 1)
    if frequency == FREQ_MONTHLY and re.fullmatch(r"\d{6}", text):
        return date(int(text[:4]), int(text[4:6]), 1)
    if frequency == FREQ_QUARTERLY and re.fullmatch(r"\d{4}Q?[1-4]", text, re.I):
        q = int(text[-1])
        return date(int(text[:4]), (q - 1) * 3 + 1, 1)
    if frequency == FREQ_DAILY and re.fullmatch(r"\d{8}", text):
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    raise ValueError(f"기간 형식을 해석할 수 없다: {raw!r} (frequency={frequency})")


def fetch_kosis(spec: SeriesSpec, api_key: str, *, start: date, end: date,
                observed_at: datetime, limiter: _Limiter) -> list[Observation]:
    """KOSIS 관측 (2026-07-31 키 유효 확인 후 도입 - err11 시절의 제외 사유 해소).

    external_series_code = "orgId:tblId:C1" (예: "101:DT_1J22042:0" = 소비자물가
    총지수), item_name = ITM_NM 필터("전년동월비(%)" 등). 실측 규격:
    itmId/objL1 은 ALL 로 받고 우리가 거른다 - 개별 코드 지정(T10 등)은 err 21.
    vintage 는 응답에 없다 - LST_CHN_DE(최종수정일)를 기록만 하고 vintage 는
    관측일로 둔다(제공자 vintage 아님을 metadata 에 명시).
    """
    org, tbl, c1 = spec.external_series_code.split(":")
    limiter.wait()
    q = urllib.parse.urlencode({
        "method": "getList", "apiKey": api_key, "itmId": "ALL", "objL1": "ALL",
        "format": "json", "jsonVD": "Y", "prdSe": "M",
        "startPrdDe": start.strftime("%Y%m"), "endPrdDe": end.strftime("%Y%m"),
        "orgId": org, "tblId": tbl,
    })
    d = _get_json(f"https://kosis.kr/openapi/Param/statisticsParameterData.do?{q}",
                  label=f"KOSIS {tbl}")
    if isinstance(d, dict):
        raise MacroError(f"KOSIS {tbl}: {d.get('errMsg', d)}")

    out = []
    for r in d:
        if r.get("C1") != c1 or r.get("ITM_NM") != spec.item_name:
            continue
        prd = str(r.get("PRD_DE") or "")
        if len(prd) != 6:
            raise MacroError(f"KOSIS {tbl}: PRD_DE 형식 이상 {prd!r}")
        period = parse_period(prd, spec.frequency)  # KOSIS PRD_DE = YYYYMM (ECOS 와 동일)
        out.append(Observation(
            external_series_code=spec.external_series_code,
            period=period,
            value=parse_value(r.get("DT")),
            # 발표 시각을 API 가 주지 않는다 - 관측일 기준이며 정밀도를 남긴다
            published_at=observed_at,
            observed_at=observed_at,
            vintage_date=observed_at.date(),
            metadata={
                "source": "kosis",
                "vintage_source": "OBSERVED_NOT_PROVIDER",
                "published_at_precision": "UNKNOWN",
                "lst_chn_de": str(r.get("LST_CHN_DE") or ""),
                "item": spec.item_name, "c1_nm": str(r.get("C1_NM") or ""),
            },
        ))
    if not out:
        raise MacroError(f"KOSIS {tbl}: (C1={c1}, {spec.item_name}) 관측 0건 - 필터 확인")
    return out


def fetch_fred(spec: SeriesSpec, api_key: str, *, start: date, end: date,
               observed_at: datetime, limiter: _Limiter) -> list[Observation]:
    """FRED 관측. realtime_start 가 실제 vintage 다."""
    limiter.wait()
    q = urllib.parse.urlencode({
        "series_id": spec.external_series_code, "api_key": api_key, "file_type": "json",
        "observation_start": start.isoformat(), "observation_end": end.isoformat(),
    })
    d = _get_json(f"https://api.stlouisfed.org/fred/series/observations?{q}",
                  label=f"FRED {spec.external_series_code}")
    rows = d.get("observations")
    if rows is None:
        raise MacroError(f"FRED {spec.external_series_code}: observations 가 없다")

    out = []
    for r in rows:
        period = parse_period(r.get("date", ""), spec.frequency)
        rs = (r.get("realtime_start") or "").strip()
        if not rs:
            raise MacroError(f"FRED {spec.external_series_code}: realtime_start 가 없다")
        vintage = parse_period(rs, FREQ_DAILY)
        out.append(Observation(
            external_series_code=spec.external_series_code,
            period=period,
            value=parse_value(r.get("value")),
            # 발표 시각을 모른다. vintage 날짜를 published_at 으로 쓰고 정밀도를 남긴다.
            published_at=datetime.combine(vintage, dtime(0, 0), tzinfo=timezone.utc),
            observed_at=observed_at,
            vintage_date=vintage,
            metadata={
                "source": "fred",
                "vintage_source": VINTAGE_FROM_PROVIDER,
                "published_at_precision": "DAY",
                "realtime_start": rs,
                "realtime_end": (r.get("realtime_end") or "").strip(),
            },
        ))
    return out


def fetch_ecos(spec: SeriesSpec, api_key: str, *, start: date, end: date,
               observed_at: datetime, limiter: _Limiter) -> list[Observation]:
    """ECOS 관측.

    external_series_code 는 `STAT_CODE.ITEM_CODE1` 형태다 - 한 통계표에 항목이 여러 개라
    STAT_CODE 만으로는 시계열이 특정되지 않는다.
    """
    stat_code, _, item_code = spec.external_series_code.partition(".")
    if not stat_code:
        raise MacroError(f"ECOS series code 형식 오류: {spec.external_series_code!r}")

    cycle = next((c for c, f in ECOS_CYCLE_TO_FREQ.items() if f == spec.frequency), None)
    if cycle is None:
        raise MacroError(f"ECOS 에 매핑할 cycle 이 없다: frequency={spec.frequency}")

    fmt = {"D": "%Y%m%d", "M": "%Y%m", "Q": "%Y%m", "A": "%Y"}[cycle]
    limiter.wait()
    url = (
        f"https://ecos.bok.or.kr/api/StatisticSearch/{urllib.parse.quote(api_key)}"
        f"/json/kr/1/1000/{stat_code}/{cycle}/{start.strftime(fmt)}/{end.strftime(fmt)}"
    )
    d = _get_json(url, label=f"ECOS {stat_code}")

    if "RESULT" in d:  # ECOS 는 오류를 RESULT 로 준다
        raise MacroError(f"ECOS {stat_code}: {d['RESULT'].get('MESSAGE')}")
    body = d.get("StatisticSearch")
    if body is None:
        raise MacroError(f"ECOS {stat_code}: StatisticSearch 가 없다 (keys={sorted(d)})")

    collected_on = observed_at.astimezone(KST).date()
    out = []
    for r in body.get("row") or []:
        if item_code and str(r.get("ITEM_CODE1", "")).strip() != item_code:
            continue
        if spec.item_name and str(r.get("ITEM_NAME1", "")).strip() != spec.item_name:
            continue
        out.append(Observation(
            external_series_code=spec.external_series_code,
            period=parse_period(r.get("TIME", ""), spec.frequency),
            value=parse_value(r.get("DATA_VALUE")),
            published_at=datetime.combine(collected_on, dtime(0, 0), tzinfo=KST),
            observed_at=observed_at,
            # ECOS 는 개정 이력을 주지 않는다. 수집일을 vintage 로 두고 그 사실을 남긴다.
            vintage_date=collected_on,
            metadata={
                "source": "ecos",
                "vintage_source": VINTAGE_FROM_COLLECTION,
                "vintage_note": (
                    "ECOS 응답에 개정 이력이 없어 vintage_date 가 수집일이다. "
                    "FRED 의 realtime_start 와 의미가 다르며, 과거 시점 Backtest 에 쓰면 "
                    "개정 후 수치를 사용하게 된다"
                ),
                "published_at_precision": "DAY",
                "stat_code": str(r.get("STAT_CODE", "")).strip(),
                "stat_name": str(r.get("STAT_NAME", "")).strip(),
                "item_name1": str(r.get("ITEM_NAME1", "")).strip(),
                "unit_name": str(r.get("UNIT_NAME", "")).strip(),
            },
        ))
    return out


def collect_macro(*, start: date, end: date, specs=SERIES,
                  observed_at: datetime | None = None) -> MacroResult:
    env = load_project_env()
    registry = SourceRegistry(env=env)
    obs = observed_at or datetime.now(timezone.utc)
    limiters = {"fred": _Limiter(5.0), "ecos": _Limiter(2.0)}

    usable: list[SeriesSpec] = []
    observations: list[Observation] = []
    failures: list[str] = []
    skipped = 0

    for spec in specs:
        # 사용 불가 Source 는 조용히 건너뛰지 않고 실패로 기록한다
        if not registry.is_available(spec.source_code):
            failures.append(
                f"{spec.source_code}/{spec.external_series_code}: "
                f"{registry.status(spec.source_code).value}"
            )
            continue
        try:
            if spec.source_code == "fred":
                rows = fetch_fred(spec, env["FRED_API_KEY"].strip(), start=start, end=end,
                                  observed_at=obs, limiter=limiters["fred"])
            elif spec.source_code == "ecos":
                rows = fetch_ecos(spec, env["ECOS_API_KEY"].strip(), start=start, end=end,
                                  observed_at=obs, limiter=limiters["ecos"])
            elif spec.source_code == "kosis":
                rows = fetch_kosis(spec, env["KOSIS_API_KEY"].strip(), start=start, end=end,
                                   observed_at=obs,
                                   limiter=limiters.setdefault("kosis", _Limiter(2.0)))
            else:
                failures.append(f"{spec.source_code}: 수집기가 없다")
                continue
        except (MacroError, ValueError) as e:
            failures.append(f"{spec.source_code}/{spec.external_series_code}: {e}")
            continue

        if not rows:
            # 빈 결과를 성공으로 취급하지 않는다
            failures.append(f"{spec.source_code}/{spec.external_series_code}: 관측 0건")
            continue
        usable.append(spec)
        observations.extend(rows)

    # (series, period, vintage) 중복 제거. 같은 vintage 에 값이 두 개면 상충이다.
    deduped: dict[tuple, Observation] = {}
    for o in observations:
        k = (o.external_series_code, o.period, o.vintage_date)
        if k in deduped:
            skipped += 1
            continue
        deduped[k] = o

    return MacroResult(tuple(usable), tuple(deduped.values()), skipped, tuple(failures))


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

def _check_period_parsing():
    assert parse_period("2026-07-28", FREQ_DAILY) == date(2026, 7, 28)
    assert parse_period("202601", FREQ_MONTHLY) == date(2026, 1, 1)
    assert parse_period("2026", FREQ_ANNUAL) == date(2026, 1, 1)
    assert parse_period("2026Q3", FREQ_QUARTERLY) == date(2026, 7, 1)
    assert parse_period("20260728", FREQ_DAILY) == date(2026, 7, 28)
    for raw, freq in (("", FREQ_MONTHLY), ("26-01", FREQ_MONTHLY), ("2026Q5", FREQ_QUARTERLY),
                      ("202601", FREQ_ANNUAL)):
        try:
            parse_period(raw, freq)
            raise AssertionError(f"{raw!r}/{freq} 가 통과했다")
        except ValueError:
            pass
    print("  기간 정규화                 OK")


def _check_value_parsing():
    assert parse_value("4.6100000000") == Decimal("4.6100000000")
    assert parse_value("2.5") == Decimal("2.5")
    assert parse_value("-0.25") == Decimal("-0.25")
    assert parse_value("1,234.5") == Decimal("1234.5")
    # FRED 결측은 `.` 다. 0 으로 떨어뜨리지 않는다
    assert parse_value(".") is None
    assert parse_value("") is None
    assert parse_value(None) is None
    try:
        parse_value("N/A")
        raise AssertionError("숫자 아닌 값이 통과했다")
    except ValueError:
        pass
    print("  값 파싱 (결측 구분)         OK")


def _check_vintage_semantics():
    """FRED 와 ECOS 의 vintage 의미가 다르다는 것을 계약으로 고정한다."""
    obs = datetime(2026, 7, 30, 5, 0, tzinfo=timezone.utc)
    lim = _Limiter(1000.0)

    class _F:
        pass
    import urllib.request as ur
    saved = ur.urlopen

    class _Resp:
        def __init__(self, payload): self._p = json.dumps(payload).encode()
        def read(self): return self._p
        def __enter__(self): return self
        def __exit__(self, *a): return False

    try:
        ur.urlopen = lambda *a, **k: _Resp({"observations": [
            {"date": "2026-07-28", "value": "4.61",
             "realtime_start": "2026-07-29", "realtime_end": "2026-07-29"}]})
        f = fetch_fred(SERIES[1], "k", start=date(2026, 7, 1), end=date(2026, 7, 30),
                       observed_at=obs, limiter=lim)
        assert len(f) == 1
        assert f[0].vintage_date == date(2026, 7, 29), "FRED vintage 는 realtime_start 다"
        assert f[0].metadata["vintage_source"] == VINTAGE_FROM_PROVIDER
        assert f[0].period == date(2026, 7, 28)

        ur.urlopen = lambda *a, **k: _Resp({"StatisticSearch": {"row": [
            {"STAT_CODE": "722Y001", "STAT_NAME": "기준금리", "ITEM_NAME1": "한국은행 기준금리",
             "ITEM_CODE1": "0101000", "TIME": "202601", "DATA_VALUE": "2.5", "UNIT_NAME": "연%"}]}})
        e = fetch_ecos(SERIES[0], "k", start=date(2026, 1, 1), end=date(2026, 7, 30),
                       observed_at=obs, limiter=lim)
        assert len(e) == 1
        # ECOS 는 개정 이력이 없어 수집일이 vintage 다
        assert e[0].vintage_date == obs.astimezone(KST).date()
        assert e[0].metadata["vintage_source"] == VINTAGE_FROM_COLLECTION
        assert "개정 후 수치" in e[0].metadata["vintage_note"]
        assert e[0].period == date(2026, 1, 1)
    finally:
        ur.urlopen = saved
    print("  vintage 의미 구분           OK")


def _check_ecos_item_filter():
    """한 STAT_CODE 에 항목이 여러 개다. 지정한 항목만 골라야 한다."""
    import urllib.request as ur
    saved = ur.urlopen

    class _Resp:
        def __init__(self, p): self._p = json.dumps(p).encode()
        def read(self): return self._p
        def __enter__(self): return self
        def __exit__(self, *a): return False

    rows = [
        {"STAT_CODE": "722Y001", "ITEM_CODE1": "0101000", "ITEM_NAME1": "한국은행 기준금리",
         "TIME": "202601", "DATA_VALUE": "2.5", "UNIT_NAME": "연%", "STAT_NAME": "x"},
        {"STAT_CODE": "722Y001", "ITEM_CODE1": "0102000", "ITEM_NAME1": "정부대출금금리",
         "TIME": "202601", "DATA_VALUE": "2.524", "UNIT_NAME": "연%", "STAT_NAME": "x"},
    ]
    try:
        ur.urlopen = lambda *a, **k: _Resp({"StatisticSearch": {"row": rows}})
        out = fetch_ecos(SERIES[0], "k", start=date(2026, 1, 1), end=date(2026, 2, 1),
                         observed_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
                         limiter=_Limiter(1000.0))
        assert len(out) == 1, f"항목 필터가 동작하지 않았다: {len(out)}"
        assert out[0].value == Decimal("2.5")

        # ECOS 오류 응답은 예외다
        ur.urlopen = lambda *a, **k: _Resp({"RESULT": {"CODE": "INFO-200", "MESSAGE": "해당 자료가 없습니다"}})
        try:
            fetch_ecos(SERIES[0], "k", start=date(2026, 1, 1), end=date(2026, 2, 1),
                       observed_at=datetime(2026, 7, 30, tzinfo=timezone.utc),
                       limiter=_Limiter(1000.0))
            raise AssertionError("ECOS 오류가 통과했다")
        except MacroError as e:
            assert "해당 자료가 없습니다" in str(e)
    finally:
        ur.urlopen = saved
    print("  ECOS 항목 필터/오류         OK")


def _check_unavailable_source_reported():
    """사용 불가 Source 는 조용히 건너뛰지 않고 failures 로 보고한다."""
    spec = SeriesSpec("kosis", "X", "테스트", FREQ_MONTHLY)
    r = collect_macro(start=date(2026, 7, 1), end=date(2026, 7, 30), specs=(spec,))
    assert len(r.series) == 0 and len(r.observations) == 0
    assert len(r.failures) == 1 and "kosis" in r.failures[0]
    assert "⚠ 실패" in r.summary()
    print("  사용 불가 Source 보고       OK")


def _collect_and_report(start: date, end: date) -> int:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repository"))
    from reference_repository import SupabaseReferenceRepository

    res = collect_macro(start=start, end=end)
    print(f"  {res.summary()}")
    for f in res.failures:
        print(f"    ⚠ {f}")

    repo = SupabaseReferenceRepository()
    try:
        _, _, src = repo.sync_data_sources()
        ns, us, sid_by_code = repo.upsert_macro_series(list(res.series), source_ids=src)
        print(f"  macro_series: {ns} 신규 / {us} 갱신")
        no, uo = repo.upsert_macro_observations(list(res.observations), series_ids=sid_by_code)
        print(f"  macro_observations: {no} 신규 / {uo} 갱신")
        no2, uo2 = repo.upsert_macro_observations(list(res.observations), series_ids=sid_by_code)
        print(f"  재적재 멱등: {no2} 신규 / {uo2} 갱신")

        with repo._conn.cursor() as cur:
            cur.execute("""
                select s.external_series_code, count(*), min(o.period), max(o.period),
                       count(distinct o.vintage_date)
                from research.macro_observations o
                join research.macro_series s on s.series_id = o.series_id
                group by 1 order by 1""")
            for code, n, mn, mx, nv in cur.fetchall():
                print(f"    {code:18} {n:>4}건  {mn}~{mx}  vintage {nv}종")
        return 0
    finally:
        repo.close()


if __name__ == "__main__":
    if "--collect" in sys.argv:
        a = sys.argv
        def opt(n, d): return a[a.index(n) + 1] if n in a else d
        s = date.fromisoformat(opt("--from", "2026-01-01"))
        e = date.fromisoformat(opt("--to", datetime.now(KST).date().isoformat()))
        print(f"{COLLECTOR_VERSION} 실제 수집 {s} ~ {e}")
        raise SystemExit(_collect_and_report(s, e))

    print(f"{COLLECTOR_VERSION} 자체 점검 (외부 호출 없음)")
    _check_period_parsing()
    _check_value_parsing()
    _check_vintage_semantics()
    _check_ecos_item_filter()
    _check_unavailable_source_reported()
    print("거시경제 5개 영역 통과. 실제 수집은 --collect")
