#!/usr/bin/env python3
"""지정학 리스크 수집 - GPR 일별 지수 + GDELT 테마 보도량·톤.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-01 "국제정치 때문에 시장이 들썩인다 - 이란을
      폭격한다니 마니 하는 것들". 소셜 포스트(Truth Social·X)는 권리·밀도
      양쪽에서 탈락했고(Registry 참고), 그 자리를 **권리 청정한 전용
      데이터**로 채운다.

▶ 두 축을 함께 쓴다 - 서로 못 하는 일을 한다
  1) GPR (Caldara-Iacoviello 지정학 리스크 지수, 1985~ 일별)
     - GPRD_THREAT = "폭격한다니"(위협·언사), GPRD_ACT = 실제 타격.
       재일님이 말한 그 구분이 지수 자체에 분리돼 있다.
     - 40년 히스토리라 **백테스트 팩터로 바로 쓴다**. 다만 게시가 4~5일
       늦어 실시간 경보용이 아니다.
  2) GDELT (전세계 보도 점유율·톤, 15분 갱신)
     - 실시간 축. 테마별 보도량이 평소 대비 몇 배로 뛰었는지로 사건을
       잡는다(실측 - North Korea missile 최근/중앙 3.7배).

▶ 지켜야 할 것
  - **미래 참조 금지**: GPR 은 게시 지연이 있으므로 published_at 을
    period + GPR_LAG_DAYS(보수적 7일)로 둔다. 지연을 0으로 두면 백테스트가
    사건 당일에 지수를 알았다고 착각한다 - 그게 가장 흔한 누수다.
  - **부분 구간 제외**: GDELT 의 마지막 포인트는 오늘(진행 중)이라 값이
    계속 변한다. 저장하지 않는다 (walk-forward 부분창 제외와 같은 규율).
  - **집계만 저장**: GDELT 는 지표(점유율·톤)만 담는다. 기사 본문은 각
    매체 권리라 담지 않는다. 인용 시 GDELT Project 출처 표기 의무.
  - 레이트리밋 - GDELT 는 5초/요청 규정(429 실측)이라 9초 간격으로 문다.

사용
  python collectors/geopolitical_collector.py              # 자체 점검
  python collectors/geopolitical_collector.py --collect              # 최근 90일
  python collectors/geopolitical_collector.py --collect --days 15000 # 전체 백필
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from macro_collector import FREQ_DAILY, Observation, SeriesSpec  # noqa: E402

COLLECTOR_VERSION = "research-geopolitical-v1"
UA = {"User-Agent": "hedgefund-research-collector/0.1 (contact: traderjaeil@gmail.com)"}

GPR_URL = "https://www.matteoiacoviello.com/gpr_files/data_gpr_daily_recent.xls"
GPR_LAG_DAYS = 7          # 실측 게시 지연 4~5일 -> 보수적으로 7 (미래 참조 방지)
GPR_COLUMNS = {
    "GPRD": "GPR 지정학 리스크 지수 (일별 종합)",
    "GPRD_ACT": "GPR 실제 사건 (Acts)",
    "GPRD_THREAT": "GPR 위협·언사 (Threats)",
}

GDELT_API = "https://api.gdeltproject.org/api/v2/doc/doc"
# 규정은 5초/요청이지만 9초에서도 429 가 났다(2026-08-01 실측) - 창 단위로
# 더 빡빡하게 재는 것으로 보인다. 16초 + 지수 백오프로 간다. 하루 2회 도는
# 배치라 총 소요(테마 6 x 2모드 x 16초 ~ 3분)는 문제가 되지 않는다.
GDELT_SLEEP_SECONDS = 16.0
GDELT_LAG_DAYS = 1          # 그날 보도량은 그날이 끝나야 확정된다
# GDELT 전체에 쓰는 시간 상한. 스케줄러 Job 타임아웃이 30분인데 429 백오프가
# 겹치면 그걸 넘길 수 있다 - 넘기면 프로세스가 죽어 **적재 자체가 사라진다**.
# 예산을 넘기면 남은 테마를 건너뛰고 받은 만큼 적재한다(건너뛴 테마는 로그에
# 남긴다 - 조용한 절단 금지). 다음 실행이 같은 120일 창을 다시 훑어 메운다.
GDELT_BUDGET_SECONDS = 12 * 60
DEFAULT_THEMES = Path(__file__).resolve().parent.parent / "config" / "geopolitical_themes.txt"


class GeopoliticalError(RuntimeError):
    """수집 실패. 빈 결과로 바꾸지 않는다."""


# ---------------------------------------------------------------------------
# 순수 함수 - 파싱·규율
# ---------------------------------------------------------------------------

def parse_themes(text: str) -> list[tuple[str, str]]:
    """'코드|질의' 목록. 중복 코드는 첫 줄만 남긴다."""
    out: list[tuple[str, str]] = []
    seen = set()
    for line in text.splitlines():
        body = line.split("#", 1)[0].strip()
        if not body or "|" not in body:
            continue
        code, query = (p.strip() for p in body.split("|", 1))
        if code and query and code not in seen:
            seen.add(code)
            out.append((code, query))
    return out


def gdelt_points(payload: dict, *, today: date) -> list[tuple[date, Decimal]]:
    """timeline 응답 -> (일자, 값). **오늘(진행 중) 포인트는 버린다.**

    GDELT 날짜는 '20260705T000000Z' 형태다. 파싱 실패·비수치는 조용히
    버리지 않고 건너뛰되, 전부 버려지면 호출부가 실패로 판단한다.
    """
    timeline = payload.get("timeline") or []
    if not timeline:
        return []
    out: list[tuple[date, Decimal]] = []
    for p in timeline[0].get("data", []):
        raw = str(p.get("date", ""))
        try:
            d = date(int(raw[0:4]), int(raw[4:6]), int(raw[6:8]))
            v = Decimal(str(p["value"]))
        except (ValueError, KeyError, TypeError, InvalidOperation):
            continue
        if d >= today:          # 진행 중인 날 - 값이 계속 변한다
            continue
        out.append((d, v))
    return out


def availability(period: date, lag_days: int) -> date:
    """관측 시점 + 게시 지연 = 우리가 그 값을 알 수 있게 된 날."""
    return period + timedelta(days=lag_days)


def gpr_series_specs() -> list[SeriesSpec]:
    return [SeriesSpec("gpr", code, name, FREQ_DAILY, unit="index")
            for code, name in GPR_COLUMNS.items()]


def gdelt_series_specs(themes: list[tuple[str, str]]) -> list[SeriesSpec]:
    specs = []
    for code, query in themes:
        specs.append(SeriesSpec("gdelt", code, f"GDELT 보도 점유율 - {query}",
                                FREQ_DAILY, unit="percent_of_coverage"))
        specs.append(SeriesSpec("gdelt", f"{code}_TONE", f"GDELT 보도 톤 - {query}",
                                FREQ_DAILY, unit="tone"))
    return specs


def make_observation(code: str, period: date, value: Decimal, *, lag_days: int,
                     observed_at: datetime, metadata: dict) -> Observation:
    """PIT 3시각을 한 곳에서 만든다 - 지연을 잊는 실수를 구조로 막는다.

    vintage_date 를 수집일이 아니라 '가용일'로 두는 이유: 수집일로 두면
    같은 기간을 다시 훑을 때마다 새 vintage 행이 쌓여(멱등 키에 vintage 가
    있다) 90일 창 x 매일 실행이 곧바로 중복 폭발이 된다. GPR·GDELT 는
    과거 값을 개정하지 않는 계열이라 가용일 고정이 맞다.
    """
    avail = availability(period, lag_days)
    return Observation(
        external_series_code=code,
        period=period,
        value=value,
        published_at=datetime(avail.year, avail.month, avail.day, tzinfo=timezone.utc),
        observed_at=observed_at,
        vintage_date=avail,
        metadata=metadata,
    )


# ---------------------------------------------------------------------------
# 외부 호출
# ---------------------------------------------------------------------------

def _http(url: str, *, label: str, tries: int = 3) -> bytes:
    last = ""
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                        timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if e.code != 429 or i == tries - 1:
                break
            time.sleep(20 * (i + 1))            # 20 / 40 / 60 / 80초
        except Exception as e:                      # noqa: BLE001
            last = f"{type(e).__name__} {e}"
            if i == tries - 1:
                break
            time.sleep(5)
    raise GeopoliticalError(f"{label}: {last}")


def parse_gpr_sheet(header: list, rows: list[list]) -> tuple[list[tuple[date, dict]], int]:
    """GPR 시트(헤더 + 행) -> [(일자, {열: 값})], 파일 최신일.

    순수 함수라 자체 점검이 파일 없이 검사한다. 필요한 열이 없으면 예외 -
    발행처가 열 이름을 바꾸면 조용히 빈 결과가 되는 것이 가장 위험하다.
    """
    idx = {str(h).strip(): i for i, h in enumerate(header)}
    missing = [c for c in ("DAY", *GPR_COLUMNS) if c not in idx]
    if missing:
        raise GeopoliticalError(f"GPR 파일 형식 변경 - 없는 열 {missing}")

    out: list[tuple[date, dict]] = []
    file_max = 0
    for r in rows:
        try:
            day = int(float(r[idx["DAY"]]))
            period = date(day // 10000, day // 100 % 100, day % 100)
        except (ValueError, TypeError, IndexError):
            continue
        file_max = max(file_max, day)
        vals = {}
        for code in GPR_COLUMNS:
            raw = r[idx[code]] if idx[code] < len(r) else None
            if raw is None or raw == "":
                continue
            try:
                v = float(raw)
            except (ValueError, TypeError):
                continue
            if v != v:                            # NaN
                continue
            vals[code] = v
        if vals:
            out.append((period, vals))
    return out, file_max


def fetch_gpr(*, start: date, observed_at: datetime) -> list[Observation]:
    """GPR 일별 파일 -> 관측.

    xlrd 로 직접 읽는다(pandas 아님). 실측 2026-08-02: 수집기 컨테이너에
    pandas 가 없어 이 Job 이 매일 실패하고 있었다 - 열 4개를 읽자고 pandas 를
    넣는 것보다 xlrd 하나가 가볍고 의존성이 적다(파일은 OLE2 .xls 고정).
    """
    import xlrd

    raw = _http(GPR_URL, label="GPR 파일")
    if raw[:2] != b"\xd0\xcf":
        raise GeopoliticalError(
            "GPR 파일이 .xls(OLE2) 가 아니다 - 발행처 형식 변경 확인 필요")
    book = xlrd.open_workbook(file_contents=raw)
    sheet = book.sheet_by_index(0)
    header = sheet.row_values(0)
    rows = [sheet.row_values(i) for i in range(1, sheet.nrows)]

    parsed, file_max = parse_gpr_sheet(header, rows)
    obs: list[Observation] = []
    for period, vals in parsed:
        if period < start:
            continue
        for code, v in vals.items():
            obs.append(make_observation(
                code, period, Decimal(str(round(v, 6))),
                lag_days=GPR_LAG_DAYS, observed_at=observed_at,
                metadata={"source": "gpr", "file_max_day": file_max,
                          "lag_days": GPR_LAG_DAYS}))
    return obs


def fetch_gdelt_theme(code: str, query: str, *, days: int, observed_at: datetime,
                      today: date) -> tuple[list[Observation], list[str]]:
    """모드(보도량·톤)별로 독립 수집한다.

    한 모드가 429 로 빠져도 이미 받은 다른 모드를 버리지 않는다 - 첫 실행에서
    톤 실패가 보도량까지 통째로 날린 실측(2026-08-01)을 고친 것이다.
    """
    obs: list[Observation] = []
    failures: list[str] = []
    span = min(max(days, 7), 365)   # DOC 2.0 timespan 상한이 1년대다
    for mode, suffix in (("timelinevol", ""), ("timelinetone", "_TONE")):
        url = (f"{GDELT_API}?query={urllib.parse.quote(query)}"
               f"&mode={mode}&timespan={span}d&format=json")
        try:
            body = _http(url, label=f"GDELT {code} {mode}")
            if body[:1] not in (b"{", b"["):
                raise GeopoliticalError(
                    f"GDELT {code} {mode}: JSON 아님 - "
                    f"{body[:80].decode('utf-8', 'replace')}")
            pts = gdelt_points(json.loads(body), today=today)
            if not pts:
                raise GeopoliticalError(f"GDELT {code} {mode}: 유효 포인트 0")
        except GeopoliticalError as e:
            failures.append(str(e))
            time.sleep(GDELT_SLEEP_SECONDS)
            continue
        for period, value in pts:
            obs.append(make_observation(
                f"{code}{suffix}", period, value, lag_days=GDELT_LAG_DAYS,
                observed_at=observed_at,
                metadata={"source": "gdelt", "query": query, "mode": mode,
                          "attribution": "GDELT Project (gdeltproject.org)"}))
        time.sleep(GDELT_SLEEP_SECONDS)
    return obs, failures


def collect(*, days: int, themes_path: Path | None = None) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "repository"))
    from reference_repository import SupabaseReferenceRepository

    from source_registry import SourceRegistry, UseScope, load_project_env

    env = load_project_env()
    reg = SourceRegistry(env=env)
    for sid in ("gpr", "gdelt"):
        reg.check_use(sid, UseScope.FULLTEXT_STORE)

    now = datetime.now(timezone.utc)
    today = now.date()
    start = today - timedelta(days=days)
    themes = parse_themes((themes_path or DEFAULT_THEMES).read_text(encoding="utf-8"))
    print(f"{COLLECTOR_VERSION}: {start} ~ / 테마 {len(themes)}개", flush=True)

    specs = gpr_series_specs() + gdelt_series_specs(themes)
    obs: list[Observation] = []
    failures: list[str] = []

    try:
        gpr = fetch_gpr(start=start, observed_at=now)
        obs.extend(gpr)
        print(f"  GPR   관측 {len(gpr)}건", flush=True)
    except GeopoliticalError as e:
        failures.append(str(e))
        print(f"  ⚠ {e}", flush=True)

    started = time.monotonic()
    skipped: list[str] = []
    for code, query in themes:
        if time.monotonic() - started > GDELT_BUDGET_SECONDS:
            skipped.append(code)
            continue
        got, fails = fetch_gdelt_theme(code, query, days=days, observed_at=now,
                                       today=today)
        obs.extend(got)
        failures.extend(fails)
        mark = "" if not fails else f"  ⚠ 모드 {len(fails)}개 실패"
        print(f"  GDELT {code:20s} {len(got)}건{mark}", flush=True)
        for f in fails:
            print(f"    {f}", flush=True)
    if skipped:
        msg = (f"시간 예산({GDELT_BUDGET_SECONDS//60}분) 초과로 건너뜀: "
               f"{', '.join(skipped)} - 다음 실행이 메운다")
        failures.append(msg)
        print(f"  ⚠ {msg}", flush=True)

    if not obs:
        print("  관측 0건 - 적재하지 않는다", flush=True)
        return 1

    repo = SupabaseReferenceRepository()
    try:
        _, _, src = repo.sync_data_sources()
        ns, us, sid_by_code = repo.upsert_macro_series(specs, source_ids=src)
        print(f"  macro_series: {ns} 신규 / {us} 갱신", flush=True)
        no, uo = repo.upsert_macro_observations(obs, series_ids=sid_by_code)
        print(f"  macro_observations: {no} 신규 / {uo} 갱신", flush=True)
    finally:
        repo.close()

    for f in failures:
        print(f"  ⚠ 실패: {f}", flush=True)
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _check_gpr_sheet_parsing():
    """시트 파싱 - 결측·잡값을 0 으로 만들지 않고, 열 이름 변경은 즉시 실패."""
    header = ["DAY", "N10D", "GPRD", "GPRD_ACT", "GPRD_THREAT"]
    rows = [
        [20260725.0, 5, 183.64, 225.32, 199.69],
        [20260726.0, 5, 193.37, "", 265.66],       # ACT 결측 - 그 열만 빠진다
        ["쓰레기", 5, 1, 2, 3],                     # 날짜 잡값 - 행 자체를 버린다
        [20260727.0, 5, "NA", 248.57, 207.33],     # 비수치 - 그 열만 빠진다
    ]
    parsed, file_max = parse_gpr_sheet(header, rows)
    got = {d.isoformat(): set(v) for d, v in parsed}
    assert got["2026-07-25"] == {"GPRD", "GPRD_ACT", "GPRD_THREAT"}
    assert got["2026-07-26"] == {"GPRD", "GPRD_THREAT"}, got["2026-07-26"]
    assert got["2026-07-27"] == {"GPRD_ACT", "GPRD_THREAT"}, got["2026-07-27"]
    assert len(parsed) == 3, "잡값 행이 살아남았다"
    assert file_max == 20260727, file_max
    # 발행처가 열 이름을 바꾸면 조용히 빈 결과가 아니라 예외여야 한다
    try:
        parse_gpr_sheet(["DAY", "GPRD"], [[20260725.0, 1.0]])
        raise AssertionError("열이 없는데 통과했다")
    except GeopoliticalError:
        pass
    print("  GPR 시트 파싱            OK")


def _check_theme_parsing():
    text = ("# 주석\nGDELT_NK|North Korea missile   # 꼬리 주석\n"
            "\nGDELT_NK|중복 코드\n형식오류줄\nGDELT_IRAN|Iran strike\n")
    assert parse_themes(text) == [("GDELT_NK", "North Korea missile"),
                                  ("GDELT_IRAN", "Iran strike")]
    assert parse_themes("# 전부 주석\n") == []
    print("  테마 파싱                OK")


def _check_partial_day_excluded():
    today = date(2026, 8, 1)
    payload = {"timeline": [{"data": [
        {"date": "20260730T000000Z", "value": 0.5},
        {"date": "20260731T000000Z", "value": 0.7},
        {"date": "20260801T000000Z", "value": 0.1},   # 진행 중 - 버려야 한다
        {"date": "쓰레기", "value": 1.0},
        {"date": "20260729T000000Z", "value": "NaN수치아님"},
    ]}]}
    got = gdelt_points(payload, today=today)
    assert got == [(date(2026, 7, 30), Decimal("0.5")),
                   (date(2026, 7, 31), Decimal("0.7"))], got
    assert gdelt_points({}, today=today) == []
    print("  부분일 제외·잡값 방어    OK")


def _check_publication_lag():
    # 미래 참조 방지 - 사건 당일에 GPR 을 알았다고 하면 안 된다
    p = date(2026, 7, 20)
    o = make_observation("GPRD", p, Decimal("306.2"), lag_days=GPR_LAG_DAYS,
                         observed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                         metadata={"source": "gpr"})
    assert o.published_at.date() == date(2026, 7, 27), o.published_at
    assert o.vintage_date == date(2026, 7, 27)
    assert o.published_at.date() > p, "게시일이 관측일보다 앞서면 누수다"
    # 같은 기간을 다시 수집해도 vintage 가 같아야 멱등이다
    o2 = make_observation("GPRD", p, Decimal("306.2"), lag_days=GPR_LAG_DAYS,
                          observed_at=datetime(2026, 9, 9, tzinfo=timezone.utc),
                          metadata={"source": "gpr"})
    assert o2.vintage_date == o.vintage_date, "수집일이 vintage 를 흔들면 중복 폭발"
    g = make_observation("GDELT_NK", p, Decimal("0.08"), lag_days=GDELT_LAG_DAYS,
                         observed_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
                         metadata={"source": "gdelt"})
    assert g.vintage_date == date(2026, 7, 21)
    print("  게시 지연·멱등 vintage   OK")


def _check_series_specs():
    themes = [("GDELT_NK", "North Korea missile")]
    specs = gpr_series_specs() + gdelt_series_specs(themes)
    codes = [s.external_series_code for s in specs]
    assert codes == ["GPRD", "GPRD_ACT", "GPRD_THREAT", "GDELT_NK", "GDELT_NK_TONE"], codes
    assert len(set(codes)) == len(codes), "시계열 코드 중복"
    assert all(s.frequency == FREQ_DAILY for s in specs)
    assert {s.source_code for s in specs} == {"gpr", "gdelt"}
    print("  시계열 정의              OK")


def _check_license_gate():
    from source_registry import SourceRegistry, SourceUseNotAllowed, UseScope

    r = SourceRegistry(env={})
    for sid in ("gpr", "gdelt"):
        r.check_use(sid, UseScope.FULLTEXT_STORE)
        try:
            r.check_use(sid, UseScope.REDISTRIBUTE)
            raise AssertionError(f"{sid} 재배포가 허용됐다")
        except SourceUseNotAllowed:
            pass
    print("  라이선스 게이트          OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--collect" in sys.argv:
        a = sys.argv
        d = int(a[a.index("--days") + 1]) if "--days" in a else 90
        raise SystemExit(collect(days=d))

    print(f"{COLLECTOR_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_gpr_sheet_parsing()
    _check_theme_parsing()
    _check_partial_day_excluded()
    _check_publication_lag()
    _check_series_specs()
    _check_license_gate()
    print("지정학 수집 6개 영역 통과. 수집은 --collect [--days N]")
