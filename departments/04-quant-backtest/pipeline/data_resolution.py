"""데이터 요구 사상 - 리서치의 원천 테이블을 실행면 데이터셋으로 옮긴다.

담당: 재일 (퀀트·백테스트본부 QNT)
계약: departments/01-research/contracts/factory_contracts.py (DataRequirement)
근거: docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md 7.1절 0단계

▶ 이 모듈이 없어서 공장 경로가 통째로 막혀 있었다
  리서치는 "무슨 데이터가 필요한가" 를 원천 이름으로 말하고(`market_bars`),
  퀀트 실행면은 "어느 스냅샷으로 돌리나" 를 매니페스트 이름으로 묻는다
  (`krx-basket-daily/v2`). 층위가 다른 두 이름을 그대로 대조하니 공장을 거친
  가설은 전부 `NOT_RUNNABLE` 로 떨어져 PROPOSED 에 영구 정체했다(2026-08-10 실측:
  구 경로 가설은 TESTING/RUNNING 까지 갔고 공장 경로 가설만 멈춰 있었다).

▶ **사상표를 코드에 박지 않는다**
  매니페스트가 이미 `source_versions = {"market_bars": "ls_chart/1D"}` 로 자기가
  어느 원천에서 만들어졌는지 기록한다. 사상은 그 기록에서 유도한다 - 코드에 박으면
  매니페스트가 늘 때마다 코드를 고쳐야 하고, 고치는 걸 잊으면 조용히 틀린다.

▶ **이름이 있다고 데이터가 있는 것은 아니다**
  매니페스트 행이 존재하면 통과시키는 것은 fail-open 이다. 매니페스트는 빌드 시점의
  *주장*이고, 그 뒤 원천이 비었는지 짧아졌는지는 말해 주지 않는다. 그래서 여기서
  **로컬 시장 DB 를 실제로 조회해 커버리지를 잰다.** 재지 못하면 통과가 아니라
  `NOT_VERIFIED` 다 - 미측정은 0 이 아니고, 0 은 PASS 가 아니다.

▶ 두 DB 를 걸친다
  메타(매니페스트·가설)는 `DATABASE_URL`, 시장 데이터는 `TIMESCALE_DATABASE_URL`.
  `pit_dataset.py` 가 이미 쓰는 관례를 그대로 따른다.

자체 점검: python departments/04-quant-backtest/pipeline/data_resolution.py
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

MODULE_VERSION = "quant-data-resolution-v1"

# ── 원천 테이블 화이트리스트 ────────────────────────────────────────────────
# 표는 **우리 것**이다. 기획안이 준 문자열을 테이블명으로 쓰면 주입이 되고, 모르는
# 이름을 짐작하면 엉뚱한 테이블의 커버리지를 이 가설의 근거로 삼게 된다. 여기 없는
# 원천은 짐작하지 않고 `UNMAPPED_SOURCE` 로 리서치에 돌려보낸다.
#   (스키마, 테이블, 시각 컬럼, 구간 컬럼 or None)
SOURCE_TABLES: dict[str, tuple[str, str, str, str | None]] = {
    "market_bars": ("market", "market_bars", "bucket_time", "interval_code"),
    "market_ticks": ("market", "market_ticks", "observed_at", None),
    "market_quotes": ("market", "market_quotes", "observed_at", None),
}

# 판정. ok 는 RESOLVED 하나뿐이다 - 나머지는 전부 "돌리면 안 된다".
RESOLVED = "RESOLVED"
UNMAPPED_SOURCE = "UNMAPPED_SOURCE"          # 사상할 매니페스트가 없다
SOURCE_EMPTY = "SOURCE_EMPTY"                # 매니페스트는 있는데 원천이 비었다
INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"  # 요구 기간에 못 미친다
NOT_VERIFIED = "NOT_VERIFIED"                # 시장 DB 를 못 봤다 - 통과 아님


@dataclass(frozen=True)
class SourceCoverage:
    """로컬 시장 DB 에서 **실제로 잰** 값. 매니페스트의 주장이 아니다."""

    table: str
    interval: str | None
    rows: int
    first_day: str | None
    last_day: str | None
    history_days: int      # 관측된 서로 다른 날짜 수. 달력 폭이 아니다.
    symbols: int

    @property
    def empty(self) -> bool:
        return self.rows <= 0


@dataclass(frozen=True)
class Resolution:
    verdict: str
    datasets: tuple[str, ...] = ()
    unmapped: tuple[str, ...] = ()
    coverage: dict[str, SourceCoverage] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.verdict == RESOLVED and bool(self.datasets)


# ── 요구 정규화 ────────────────────────────────────────────────────────────
def normalize_requirement(value) -> tuple[tuple[str, ...], int, tuple[str, ...]]:
    """세 모양을 다 받는다.

    반환: (원천 테이블, 최소 이력 일수, 이미 매니페스트 이름인 것)

    구 경로가 넣던 `["krx-basket-daily/v1"]` 은 이미 실행면 이름이라 사상할 게
    없다. 다만 **검증까지 건너뛰지는 않는다** - 그게 fail-open 이었다.
    """
    if isinstance(value, str):
        value = json.loads(value or "null")
    if value is None:
        return (), 0, ()
    if isinstance(value, dict):
        tables = tuple(str(t) for t in (value.get("tables") or ()))
        min_days = int(value.get("min_history_days") or 0)
        # dict 안에 실행면 이름을 직접 준 경우도 받아 준다.
        direct = tuple(str(d) for d in (value.get("data_products") or ()))
        return tables, min_days, direct
    # 리스트: "이름/버전" 은 매니페스트, 그 외는 원천 테이블로 읽는다.
    tables, direct = [], []
    for item in value:
        s = str(item)
        (direct if "/" in s else tables).append(s)
    return tuple(tables), 0, tuple(direct)


# ── 매니페스트 색인 ────────────────────────────────────────────────────────
_SQL_MANIFESTS = """
select name, version, coalesce(source_versions, '{}'::jsonb)
  from quant.dataset_manifests
"""


def manifest_index(meta_conn) -> list[tuple[str, str, dict]]:
    """(이름, 버전, source_versions) 목록. 사상의 근거는 전부 여기서 나온다."""
    cur = meta_conn.cursor()
    cur.execute(_SQL_MANIFESTS)
    out = []
    for name, version, sources in cur.fetchall():
        if isinstance(sources, str):
            sources = json.loads(sources or "{}")
        out.append((str(name), str(version), dict(sources or {})))
    return out


def _interval_of(source_version: str) -> str | None:
    """`"ls_chart/1D"` -> `"1D"`. 구간을 안 적었으면 None(구간 무관)."""
    s = str(source_version or "")
    return s.rsplit("/", 1)[1] if "/" in s else None


# ── 실측 ───────────────────────────────────────────────────────────────────
def measure_source(market_conn, table: str, interval: str | None) -> SourceCoverage | None:
    """로컬 시장 DB 에서 커버리지를 잰다. 모르는 테이블이면 None(짐작하지 않는다)."""
    spec = SOURCE_TABLES.get(table)
    if spec is None:
        return None
    schema, tbl, tcol, icol = spec

    # 식별자는 우리 화이트리스트에서만 오고, 값(interval)만 파라미터로 넘긴다.
    where, params = "", []
    if interval and icol:
        where, params = f" where {icol} = %s", [interval]
    sql = (
        f"select count(*), min({tcol})::date, max({tcol})::date,"
        f" count(distinct {tcol}::date), count(distinct instrument_id)"
        f" from {schema}.{tbl}{where}"
    )
    cur = market_conn.cursor()
    cur.execute(sql, params)
    rows, first, last, days, syms = cur.fetchone()
    return SourceCoverage(
        table=table, interval=interval, rows=int(rows or 0),
        first_day=str(first) if first else None,
        last_day=str(last) if last else None,
        history_days=int(days or 0), symbols=int(syms or 0),
    )


# ── 사상 ───────────────────────────────────────────────────────────────────
def resolve(requirement, *, meta_conn, market_conn) -> Resolution:
    """원천 요구를 실행 가능한 데이터셋 이름으로 옮긴다.

    통과 조건은 셋을 **모두** 만족해야 한다:
      ① 요구한 원천 전부를 재료로 쓰는 매니페스트가 있다
      ② 그 원천이 로컬에 실제로 있고 비어 있지 않다
      ③ 관측된 이력이 요구 기간 이상이다
    """
    tables, min_days, direct = normalize_requirement(requirement)
    notes: list[str] = []

    if market_conn is None:
        # 재지 못한 것을 통과로 세면 이 관문 전체가 장식이 된다.
        return Resolution(NOT_VERIFIED, notes=("시장 DB 연결이 없어 커버리지를 재지 못했다",))

    index = manifest_index(meta_conn)
    known = {f"{n}/{v}": (n, v, s) for n, v, s in index}

    # 이미 실행면 이름으로 온 것: 사상은 건너뛰되 검증은 한다.
    chosen: list[tuple[str, dict]] = []
    unmapped: list[str] = []
    for d in direct:
        if d in known:
            chosen.append((d, known[d][2]))
        else:
            unmapped.append(d)

    # 원천 이름으로 온 것: source_versions 가 그 원천을 덮는 매니페스트를 찾는다.
    if tables:
        want = set(tables)
        cands = [(f"{n}/{v}", s) for n, v, s in index if want <= set(s.keys())]
        if not cands:
            covered = {t for _, _, s in index for t in s}
            unmapped.extend(sorted(want - covered))
            # 원천은 아는데 그걸 재료로 쓰는 매니페스트가 없는 경우도 사상 실패다.
            unmapped.extend(sorted(want & covered - want))
            if not unmapped:
                unmapped.extend(sorted(want))
        else:
            # 같은 이름이면 최신 버전 하나만 쓴다 - 여러 버전을 동시에 돌리면
            # 어느 스냅샷의 결과인지가 사라진다.
            best: dict[str, tuple[str, dict]] = {}
            for key, s in cands:
                name, ver = key.rsplit("/", 1)
                if name not in best or ver > best[name][0].rsplit("/", 1)[1]:
                    best[name] = (key, s)
            chosen.extend(best.values())

    if unmapped:
        return Resolution(UNMAPPED_SOURCE, unmapped=tuple(sorted(set(unmapped))),
                          notes=("사상할 데이터셋이 없다 - 리서치에 반려한다",))
    if not chosen:
        return Resolution(UNMAPPED_SOURCE, notes=("요구가 비어 있다",))

    # ② ③ 실측. 매니페스트가 선언한 구간(1D 등)으로 잰다.
    coverage: dict[str, SourceCoverage] = {}
    empty: list[str] = []
    short: list[str] = []
    for _key, sources in chosen:
        for tbl, sv in sources.items():
            if tables and tbl not in tables:
                continue          # 이 요구가 안 쓴 재료까지 볼 필요는 없다
            cov = measure_source(market_conn, tbl, _interval_of(sv))
            if cov is None:
                unmapped.append(tbl)
                continue
            coverage[tbl] = cov
            if cov.empty:
                empty.append(tbl)
            elif min_days and cov.history_days < min_days:
                short.append(f"{tbl}({cov.history_days}일<{min_days}일)")

    if unmapped:
        return Resolution(UNMAPPED_SOURCE, unmapped=tuple(sorted(set(unmapped))),
                          coverage=coverage, notes=("원천 화이트리스트에 없다",))
    if empty:
        return Resolution(SOURCE_EMPTY, coverage=coverage,
                          notes=tuple(f"{t} 행 0건" for t in empty))
    if short:
        return Resolution(INSUFFICIENT_HISTORY, coverage=coverage,
                          notes=tuple(short))

    for tbl, cov in sorted(coverage.items()):
        notes.append(f"{tbl}: {cov.rows:,}행 {cov.first_day}~{cov.last_day} "
                     f"{cov.history_days}일 {cov.symbols}종목")
    return Resolution(RESOLVED, datasets=tuple(sorted(k for k, _ in chosen)),
                      coverage=coverage, notes=tuple(notes))


# ── 자체 점검 ──────────────────────────────────────────────────────────────
class _Cur:
    def __init__(self, rows): self._rows = rows
    def execute(self, sql, params=None): self._sql = sql
    def fetchall(self): return self._rows
    def fetchone(self): return self._rows[0]


class _Conn:
    def __init__(self, rows): self._rows = rows
    def cursor(self): return _Cur(self._rows)


def _selfcheck() -> int:
    META = [("krx-basket-daily", "v1", {"market_bars": "ls_chart/1D"}),
            ("krx-basket-daily", "v2", {"market_bars": "ls_chart/1D"})]
    FULL = _Conn([(218985, "2024-01-01", "2026-08-09", 640, 350)])
    EMPTY = _Conn([(0, None, None, 0, 0)])
    meta = _Conn(META)
    fails = []

    def check(label, cond):
        if not cond:
            fails.append(label)

    # 요구 정규화
    t, d, x = normalize_requirement({"tables": ["market_bars"], "min_history_days": 400})
    check("dict 정규화", t == ("market_bars",) and d == 400 and x == ())
    t, d, x = normalize_requirement(["krx-basket-daily/v1"])
    check("구 경로 정규화", x == ("krx-basket-daily/v1",) and t == ())
    t, _, _ = normalize_requirement('{"tables":["market_ticks"]}')
    check("JSON 문자열", t == ("market_ticks",))

    # ① 공장 경로가 실제로 풀린다 - 이게 막혀 있던 그 케이스다
    r = resolve({"tables": ["market_bars"], "min_history_days": 400},
                meta_conn=meta, market_conn=FULL)
    check("공장 경로 사상", r.ok and r.datasets == ("krx-basket-daily/v2",))
    check("최신 버전 선택", "v1" not in "".join(r.datasets))

    # ② 미측정은 통과가 아니다
    check("시장 DB 없음", resolve({"tables": ["market_bars"]},
                                  meta_conn=meta, market_conn=None).verdict == NOT_VERIFIED)

    # ③ 0 건은 PASS 가 아니다
    check("빈 원천", resolve({"tables": ["market_bars"]},
                             meta_conn=meta, market_conn=EMPTY).verdict == SOURCE_EMPTY)

    # ④ 이력 부족
    check("이력 부족", resolve({"tables": ["market_bars"], "min_history_days": 900},
                               meta_conn=meta, market_conn=FULL).verdict == INSUFFICIENT_HISTORY)

    # ⑤ 사상할 매니페스트가 없는 원천은 반려한다 - 비슷한 걸로 대신 돌리지 않는다
    r = resolve({"tables": ["market_quotes"]}, meta_conn=meta, market_conn=FULL)
    check("미사상 반려", r.verdict == UNMAPPED_SOURCE and "market_quotes" in r.unmapped)

    # ⑥ 화이트리스트에 없는 이름은 짐작하지 않는다
    check("모르는 원천", measure_source(FULL, "drop_table", None) is None)

    # ⑦ 구 경로도 검증을 받는다(존재하지만 비면 막힌다)
    check("구 경로 검증", resolve(["krx-basket-daily/v1"],
                                  meta_conn=meta, market_conn=EMPTY).verdict == SOURCE_EMPTY)
    check("구 경로 통과", resolve(["krx-basket-daily/v1"],
                                  meta_conn=meta, market_conn=FULL).ok)

    # ⑧ 없는 매니페스트 이름
    check("미등록 매니페스트", resolve(["nope/v9"], meta_conn=meta,
                                       market_conn=FULL).verdict == UNMAPPED_SOURCE)

    for f in fails:
        print(f"  FAIL {f}")
    print(f"data_resolution 자체 점검: {8 + 3 - len(fails)}/{8 + 3} 통과")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(_selfcheck())
