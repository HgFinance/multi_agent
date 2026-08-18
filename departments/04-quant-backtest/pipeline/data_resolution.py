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
import sys
from pathlib import Path
from dataclasses import dataclass, field

MODULE_VERSION = "quant-data-resolution-v2"
INTRADAY_EVENT = "INTRADAY_EVENT"
LIVE_INTRADAY_DATASET = "krx-intraday-events/v1"

# ── 원천 테이블 화이트리스트 ────────────────────────────────────────────────
# 표는 **우리 것**이다. 기획안이 준 문자열을 테이블명으로 쓰면 주입이 되고, 모르는
# 이름을 짐작하면 엉뚱한 테이블의 커버리지를 이 가설의 근거로 삼게 된다. 여기 없는
# 원천은 짐작하지 않고 `UNMAPPED_SOURCE` 로 리서치에 돌려보낸다.
#   (스키마, 테이블, 시각 컬럼, 구간 컬럼 or None)
# 원천 한 줄 = (DB, 스키마, 테이블, 시각 컬럼, 구간 컬럼, 종목 컬럼)
#
# ▶ **DB 를 명시한다** (2026-08-12)
#   예전엔 전부 시장 DB(TimescaleDB) 라고 가정하고 `market_conn` 하나로 쟀다.
#   그래서 뉴스·공시·재무처럼 메타 DB(Supabase `research` 스키마)에 있는 원천은
#   **아예 사상 대상이 될 수 없었다** - 13만 건짜리 공시 원장이 있는데 백테스트
#   에서는 존재하지 않는 것과 같았다. 어느 DB 인지가 원천의 속성이므로 여기 적는다.
#
# ▶ 종목 컬럼도 명시한다
#   시장 테이블은 `instrument_id` 지만 문서·재무는 연결 테이블을 통해 붙는다.
#   None 이면 종목 수를 세지 않는다 - **0 으로 채우지 않는다.** 못 센 것과
#   0 종목은 다르고, 섞이면 커버리지 판정이 오염된다.
DB_MARKET, DB_META = "market", "meta"

SOURCE_TABLES: dict[str, tuple[str, str, str, str, str | None, str | None]] = {
    # ── 시장 평면 (TimescaleDB) ──
    "market_bars":   (DB_MARKET, "market", "market_bars", "bucket_time",
                      "interval_code", "instrument_id"),
    "market_ticks":  (DB_MARKET, "market", "market_ticks", "observed_at",
                      None, "instrument_id"),
    "market_quotes": (DB_MARKET, "market", "market_quotes", "observed_at",
                      None, "instrument_id"),
    # 틱 파생 1분봉(연속집계). ls_chart 1M 은 350종목에서 멈춰 있으므로 분봉이
    # 필요하면 이쪽이다 - 우리 틱에서 나오므로 종목 수가 호가·체결과 같다.
    "bars_1m":       (DB_MARKET, "market", "bars_1m", "bucket_time",
                      None, "instrument_id"),
    # 호가·체결을 일별로 접은 피처. **백테스트가 읽는 것은 원시가 아니라 이것이다**
    # (원시는 압축 후에도 220GB 라 파티션으로 굳힐 수 없다). 스프레드·호가불균형·
    # 주문흐름불균형·체결강도·실현변동성이 종목·일자 단위로 들어 있다.
    "microstructure_features": (DB_MARKET, "market", "microstructure_features",
                                "event_time", None, "instrument_id"),
    "derivative_snapshots": (DB_MARKET, "market", "derivative_snapshots",
                             "observed_at", None, None),
    "market_breadth": (DB_MARKET, "market", "market_breadth", "observed_at",
                       None, None),
    # ── 리서치 평면 (Supabase) ──
    #
    # ▶ **여기 적는 것은 계속 채워질 원천만이다** (2026-08-12)
    #   원천을 사상 가능하게 만들면 브리핑이 그것을 광고하고, 기획자는 그걸 쓰는
    #   기획안을 낸다. 갱신이 멈춘 테이블을 남겨 두면 접수·Gate 0 을 통과한 뒤
    #   **실험 단계에서 죽는다** - `low_volatility` 로 이미 한 번 겪은 경로다.
    #
    #   내린 것과 그 이유(QUALITATIVE_FACTOR_SPEC.md, 다른 세션 결정):
    #     documents / document_instruments — 원문을 적재하지 않기로 했다.
    #       DART 를 MCP 로 그때그때 조회해 **점수만** 남긴다.
    #     macro_observations — macro Job 이 내려갔다. 거시는 정성 점수 팩터의
    #       재료가 되고 원계열은 더 안 쌓인다.
    #
    #   남긴 것:
    #     financial_facts — 펀더멘털 **정량** 점수의 입력이고 소급 재현이 된다
    #       (같은 문서 §2.1). 정성 전환 대상이 아니다.
    "financial_facts": (DB_META, "research", "financial_facts", "as_known_at",
                        None, "instrument_id"),
}

# ── 유도 관계 ──────────────────────────────────────────────────────────────
# ▶ **원시를 요구했는데 접은 것만 있을 때** (2026-08-12, 재일 지시)
#   리서치는 논문 용어대로 `market_quotes`·`market_ticks` 를 요구한다. 그런데
#   원시는 압축 후에도 하루 2.5GB 라 파티션으로 굳힐 수 없고, 백테스트가 실제로
#   읽는 것은 **종목·일자로 접은** `microstructure_features` 다. 그 안에
#   `spread_bps`·`depth_imbalance`·`order_flow_imbalance` 가 들어 있으니
#   호가 불균형·주문흐름 가설은 이것으로 검정된다.
#
#   그래서 원시 요구를 유도본으로 **대체할 수 있다고 본다.** 다만 조용히
#   바꾸지 않는다 - 대체했다는 사실이 `Resolution.notes` 로 나가고 실험 기록에
#   남는다. 원시 틱으로 검정했다고 착각하면 결론의 강도를 잘못 읽는다.
#
#   ⚠ 여기 적는 것은 **정말로 그 원천에서 유도된 것만**이다. 비슷해 보인다고
#     넣으면 "없는 것을 있다고" 하는 것이고, 그건 이 저장소가 반복해 막아 온
#     실패다.
DERIVED_FROM: dict[str, tuple[str, ...]] = {
    "microstructure_features": ("market_quotes", "market_ticks"),
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
    # EXACT이면 관측된 서로 다른 날짜 수, CHUNK_RANGE이면 달력 범위다.
    # 후자는 최소 이력 판정에 사용하지 않고 bounded runner가 정확히 센다.
    history_days: int
    symbols: int
    measurement: str = "EXACT"
    chunks: int = 0

    @property
    def empty(self) -> bool:
        if self.measurement == "CHUNK_RANGE":
            return self.chunks <= 0
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
  from quant.dataset_manifests manifest
 where not (manifest.name = 'krx-intraday-events'
            and manifest.version = 'v1')
   and exists (
       select 1
         from quant.universe_members member
        where member.universe_version_id = manifest.universe_version_id
       )
   and not exists (
       select 1
         from quant.universe_members member
         left join reference.instruments instrument
           on instrument.instrument_id = member.instrument_id
        where member.universe_version_id = manifest.universe_version_id
          and (
            instrument.instrument_id is null
            or coalesce(upper(instrument.instrument_type), '') <> 'STOCK'
            or coalesce(upper(instrument.asset_class), '') <> 'EQUITY'
            or coalesce(upper(instrument.market), '') <> 'KRX'
            or coalesce(upper(instrument.status), '') <> 'ACTIVE'
          )
       )
"""

# The raw event manifest is deliberately not an immutable materialized panel:
# its exact cutoff, sessions and observed stock subset are frozen by the
# intraday runner for every experiment.  It may therefore participate only in
# INTRADAY_EVENT source-availability resolution, never in the generic/daily
# manifest index or reusable performance evidence.  Exact identity, clocks and
# audit markers are all required so a different NULL-universe manifest cannot
# enter through this exception.
_SQL_LIVE_INTRADAY_MANIFEST = """
select manifest.name, manifest.version,
       coalesce(manifest.source_versions, '{}'::jsonb)
  from quant.dataset_manifests manifest
 where manifest.name = 'krx-intraday-events'
   and manifest.version = 'v1'
   and manifest.universe_version_id is null
   and manifest.row_count is null
   and manifest.partitions = '[]'::jsonb
   and manifest.object_path =
       'timescaledb://market/{market_quotes,market_ticks}'
   and manifest.source_versions ?& array['market_quotes', 'market_ticks']
   and nullif(manifest.source_versions->>'market_quotes', '') is not null
   and nullif(manifest.source_versions->>'market_ticks', '') is not null
   and manifest.quality_summary->>'status' =
       'LIVE_SLICE_REQUIRES_PER_EXPERIMENT_AUDIT'
   and manifest.point_in_time_policy->>'knowledge_clock' =
       'available_at=max(received_at,observed_at)'
   and manifest.point_in_time_policy->>'feature_cutoff' =
       'event_time<=decision_time and available_at<=decision_time'
   and manifest.point_in_time_policy->>'label_cutoff' =
       'entry_time+horizon'
   and manifest.point_in_time_policy->>'instrument_isolation' = 'true'
   and manifest.schema_definition->'market_quotes'->'required' @>
       '["event_time","received_at","observed_at","instrument_id"]'::jsonb
   and manifest.schema_definition->'market_ticks'->'required' @>
       '["event_time","received_at","observed_at","instrument_id"]'::jsonb
"""

_SQL_INTRADAY_MANIFESTS = (
    _SQL_MANIFESTS.rstrip() + "\nunion\n" +
    _SQL_LIVE_INTRADAY_MANIFEST.strip() + "\n")


def manifest_index(meta_conn, *, research_lane: str = "") \
        -> list[tuple[str, str, dict]]:
    """Return manifests eligible for this execution lane.

    Generic and daily callers receive only immutable governed stock panels.
    ``INTRADAY_EVENT`` additionally receives the single typed live Timescale
    source manifest.  That extra row grants source availability only; the
    runner must still freeze and attest the exact stock slice before a trial is
    registered.
    """
    cur = meta_conn.cursor()
    sql = (_SQL_INTRADAY_MANIFESTS
           if str(research_lane or "").upper() == INTRADAY_EVENT
           else _SQL_MANIFESTS)
    cur.execute(sql)
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
def measure_source(market_conn, table: str, interval: str | None,
                   *, meta_conn=None) -> SourceCoverage | None:
    """그 원천이 사는 DB 에서 커버리지를 잰다. 모르는 테이블이면 None.

    `meta_conn` 은 리서치 평면(공시·재무) 원천에만 필요하다. 안 주면 그 원천은
    **못 잰 것으로 남는다**(None) - 시장 연결로 대신 재면 없는 테이블을 조회해
    예외가 나거나, 더 나쁘게는 이름이 같은 다른 테이블을 잰다.
    """
    spec = SOURCE_TABLES.get(table)
    if spec is None:
        return None
    db, schema, tbl, tcol, icol, symcol = spec

    conn = market_conn if db == DB_MARKET else meta_conn
    if conn is None:
        return None      # 못 잰 것이다. 0 으로 채우지 않는다.

    # Raw quote/trade hypertables are tens to hundreds of GB. A global
    # count/distinct coverage probe fans out over every compressed chunk before
    # the actual bounded experiment even starts. At this eligibility boundary
    # we only need to know whether a time range exists. Exact sessions,
    # instruments, rows and lineage are measured by intraday_experiment_runner
    # inside its preregistered slice.
    if db == DB_MARKET and tbl in {"market_ticks", "market_quotes"}:
        cur = conn.cursor()
        cur.execute("""
            select min(range_start)::date, max(range_end)::date, count(*)
              from timescaledb_information.chunks
             where hypertable_schema = %s and hypertable_name = %s
        """, (schema, tbl))
        first, last, chunks = cur.fetchone()
        calendar_days = ((last - first).days + 1) if first and last else 0
        return SourceCoverage(
            table=table, interval=interval, rows=0,
            first_day=str(first) if first else None,
            last_day=str(last) if last else None,
            history_days=calendar_days, symbols=0,
            measurement="CHUNK_RANGE", chunks=int(chunks or 0),
        )

    # 식별자는 우리 화이트리스트에서만 오고, 값(interval)만 파라미터로 넘긴다.
    where, params = "", []
    if interval and icol:
        where, params = f" where {icol} = %s", [interval]
    sym_expr = f"count(distinct {symcol})" if symcol else "0"
    sql = (
        f"select count(*), min({tcol})::date, max({tcol})::date,"
        f" count(distinct {tcol}::date), {sym_expr}"
        f" from {schema}.{tbl}{where}"
    )
    cur = conn.cursor()
    cur.execute(sql, params)
    rows, first, last, days, syms = cur.fetchone()
    return SourceCoverage(
        table=table, interval=interval, rows=int(rows or 0),
        first_day=str(first) if first else None,
        last_day=str(last) if last else None,
        history_days=int(days or 0), symbols=int(syms or 0),
    )


# ── 사상 ───────────────────────────────────────────────────────────────────
def resolve(requirement, *, meta_conn, market_conn,
            research_lane: str = "") -> Resolution:
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

    lane = str(research_lane or "").upper()
    index = manifest_index(meta_conn, research_lane=lane)
    known = {f"{n}/{v}": (n, v, s) for n, v, s in index}

    # 이미 실행면 이름으로 온 것: 사상은 건너뛰되 검증은 한다.
    chosen: list[tuple[str, dict]] = []
    unmapped: list[str] = []
    substituted: list[str] = []
    for d in direct:
        if d in known:
            chosen.append((d, known[d][2]))
        else:
            unmapped.append(d)

    # 원천 이름으로 온 것: 그 원천을 덮는 매니페스트를 찾는다.
    #
    # ▶ **한 매니페스트가 전부를 덮을 필요는 없다** (2026-08-12 실측)
    #   예전엔 `want <= set(s.keys())` 로 **하나가 모든 원천을 덮을 때만** 통과
    #   시켰다. 그래서 일봉과 마이크로구조가 각각 등재돼 있는데도
    #   `{market_bars, microstructure_features}` 요구가 `UNMAPPED_SOURCE` 로
    #   떨어졌다 - 둘 다 있는데 "사상할 데이터셋이 없다" 고 말한 것이다.
    #   여러 원천을 조합하는 기획안(가격 + 유동성)이 전부 그렇게 막혔다.
    #
    #   원천마다 덮는 매니페스트를 고르고 합집합을 쓴다. 사상 실패는 **어떤
    #   매니페스트도 그 원천을 안 쓰는 경우**뿐이다.
    #
    #   ▷ 예전 코드에는 죽은 줄도 있었다: `want & covered - want` 는 집합
    #     대수상 항상 공집합이다((A∩B)−A = ∅). 그래서 그 분기가 무엇을
    #     잡으려 했든 아무것도 안 잡았고, 바로 아래 `if not unmapped` 가
    #     **요구 전체**를 미사상으로 몰아 진짜 원인을 덮었다.
    if tables:
        want = set(tables)
        # 같은 이름이면 최신 버전 하나만 쓴다 - 여러 버전을 동시에 돌리면
        # 어느 스냅샷의 결과인지가 사라진다.
        best: dict[str, tuple[str, dict]] = {}
        for tbl in sorted(want):
            for n, v, s in index:
                if tbl not in s:
                    continue
                if n not in best or v > best[n][0].rsplit("/", 1)[1]:
                    best[n] = (f"{n}/{v}", s)
        covered = {t for _, s in best.values() for t in s}
        # 유도본으로 대체 가능한 원시 요구를 건져낸다. **대체 사실을 남긴다.**
        still = want - covered
        for raw in sorted(still):
            for derived, sources in DERIVED_FROM.items():
                if raw not in sources:
                    continue
                for n, v, srcs in index:
                    if derived not in srcs:
                        continue
                    if n not in best or v > best[n][0].rsplit("/", 1)[1]:
                        best[n] = (f"{n}/{v}", srcs)
                    # **원시 이름**을 덮인 것으로 표시한다. 유도본 이름을
                    # 넣으면 `want` 에 없는 값이라 미사상이 그대로 남는다.
                    covered = covered | {raw}
                    substituted.append(
                        f"{raw} -> {derived} (유도본으로 대체. 원시가 아니라 "
                        f"종목·일자로 접은 값이다 - 결론을 원시 틱 검정으로 읽지 마라)")
                    break
        unmapped.extend(sorted(want - covered))
        if not unmapped:
            # 고른 것 중 **이 요구가 실제로 쓰는 원천을 가진 것**만 남긴다.
            # 안 그러면 무관한 매니페스트의 커버리지까지 재게 된다.
            # 유도본은 원시 이름과 안 겹치므로 `& want` 로 걸러지면 빠진다.
            keep = set(want) | {d for d in DERIVED_FROM}
            chosen.extend(v for v in best.values() if set(v[1]) & keep)

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
            cov = measure_source(market_conn, tbl, _interval_of(sv),
                                 meta_conn=meta_conn)
            if cov is None:
                unmapped.append(tbl)
                continue
            coverage[tbl] = cov
            if cov.empty:
                empty.append(tbl)
            # 청크 카탈로그의 달력 폭을 거래 세션 수처럼 사용하면 fail-open이다.
            # 원시 호가·체결의 정확한 세션 수는 intraday runner가 사전등록된
            # bounded slice에서 세며, 그 결과가 UNDERPOWERED 판정을 담당한다.
            elif (min_days and cov.measurement != "CHUNK_RANGE"
                  and cov.history_days < min_days):
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

    # **대체를 맨 앞에 싣는다.** 읽는 쪽이 커버리지 숫자만 보고 원시로 검정한
    # 줄 알면 결론의 강도를 잘못 읽는다.
    notes.extend(substituted)
    if any(key == LIVE_INTRADAY_DATASET for key, _sources in chosen):
        notes.append(
            "krx-intraday-events/v1: LIVE_RUNTIME_SOURCE_ONLY; exact cutoff, "
            "sessions and KRX ACTIVE EQUITY/STOCK identities are frozen and "
            "fail-closed by the bounded intraday runner; this manifest alone "
            "is not reusable performance evidence")
    for tbl, cov in sorted(coverage.items()):
        if cov.measurement == "CHUNK_RANGE":
            notes.append(
                f"{tbl}: LIVE CHUNK_RANGE {cov.first_day}~{cov.last_day} "
                f"({cov.chunks} chunks); 정확한 세션·종목·행 수는 실험별 "
                "bounded slice에서 계측")
        else:
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


class _SqlAwareCur:
    def __init__(self, conn):
        self._conn = conn
        self._rows = []

    def execute(self, sql, params=None):
        self._conn.executed.append((sql, params))
        self._rows = self._conn.responses["chunks" if
            "timescaledb_information.chunks" in sql else "default"]

    def fetchall(self): return self._rows
    def fetchone(self): return self._rows[0]


class _SqlAwareConn:
    """자체 점검용: 청크 probe가 전역 count로 퇴행하는 것을 잡는다."""

    def __init__(self, *, default, chunks):
        self.responses = {"default": default, "chunks": chunks}
        self.executed = []

    def cursor(self): return _SqlAwareCur(self)


def _selfcheck() -> int:
    META = [("krx-basket-daily", "v1", {"market_bars": "ls_chart/1D"}),
            ("krx-basket-daily", "v2", {"market_bars": "ls_chart/1D"})]
    FULL = _Conn([(218985, "2024-01-01", "2026-08-09", 640, 350)])
    EMPTY = _Conn([(0, None, None, 0, 0)])
    meta = _Conn(META)
    fails = []

    ran = [0]

    def check(label, cond):
        ran[0] += 1
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

    # ⑨ **여러 원천이 서로 다른 매니페스트에 있어도 조합된다** (2026-08-12)
    #    예전엔 하나가 전부를 덮을 때만 통과시켜, 일봉과 마이크로구조가 각각
    #    등재돼 있는데도 둘을 같이 요구하면 "사상할 데이터셋이 없다" 였다.
    #    가격 + 유동성처럼 축을 섞는 기획안이 전부 그렇게 막힌다.
    META2 = [*META,
             ("krx-microstructure-daily", "v1",
              {"microstructure_features": "ms-daily-v1"})]
    meta2 = _Conn(META2)
    r = resolve({"tables": ["market_bars", "microstructure_features"],
                 "min_history_days": 30}, meta_conn=meta2, market_conn=FULL)
    check("여러 매니페스트 조합", r.ok and set(r.datasets) == {
        "krx-basket-daily/v2", "krx-microstructure-daily/v1"})
    # 원시 요구를 검증된 유도본이 덮으면 대체 사실을 명시하고 실행 가능하다.
    r = resolve({"tables": ["market_bars", "market_quotes"]},
                meta_conn=_Conn(META2), market_conn=FULL)
    check("원시 요구 유도본 대체", r.ok
          and set(r.datasets) == {
              "krx-basket-daily/v2", "krx-microstructure-daily/v1"}
          and any("market_quotes -> microstructure_features" in n
                  for n in r.notes))

    # 원시 하이퍼테이블의 eligibility probe는 청크 카탈로그만 읽는다. 정확한
    # count/distinct는 수백 GB를 훑으므로 반드시 bounded runner 뒤로 미룬다.
    from datetime import date
    raw = _SqlAwareConn(
        default=[(999, date(2026, 1, 1), date(2026, 8, 1), 150, 350)],
        chunks=[(date(2026, 6, 25), date(2026, 8, 16), 43)],
    )
    cov = measure_source(raw, "market_ticks", None)
    raw_sql = "\n".join(sql for sql, _ in raw.executed).lower()
    check("원시 청크 bounded probe", cov is not None
          and cov.measurement == "CHUNK_RANGE" and cov.chunks == 43
          and cov.history_days == 53 and not cov.empty
          and "timescaledb_information.chunks" in raw_sql
          and "count(distinct" not in raw_sql)

    for f in fails:
        print(f"  FAIL {f}")
    # ▶ 총계를 **실제로 센다**. 예전엔 `8 + 3` 하드코딩이라 검사를 늘려도 숫자가
    #   그대로였다 - 2026-08-12 에 두 개를 더했는데 여전히 "11/11 통과" 가
    #   찍혔다. 늘어난 줄 알고 넘어가면 새 검사가 도는지 아무도 모른다.
    print(f"data_resolution 자체 점검: {ran[0] - len(fails)}/{ran[0]} 통과")
    return 1 if fails else 0


_SQL_CATALOG = """
select m.name, m.version,
       coalesce(m.source_versions, '{}'::jsonb),
       m.row_count,
       -- ▶ **유니버스 미연결과 0종목을 가른다** (2026-08-12)
       --   서브쿼리는 행이 없어도 0 을 준다. `universe_version_id` 가 아예
       --   NULL 인 데이터셋(마이크로구조가 그렇다)까지 `0종목` 으로 찍혀
       --   "종목이 하나도 없는 데이터셋" 처럼 보였다 - 148,931행짜리인데도.
       --   미측정과 0 은 다르다. 안 걸린 것은 NULL 로 내보낸다.
       case when m.universe_version_id is null then null
            else (select count(*) from quant.universe_members u
                   where u.universe_version_id = m.universe_version_id)
       end,
       coalesce(m.partitions, '[]'::jsonb),
       coalesce(m.quality_summary, '{}'::jsonb),
       m.as_of
  from quant.dataset_manifests m
 order by m.name, m.version
"""


def catalog(meta_conn) -> list[dict]:
    """등재된 데이터셋 전부와 **고를 때 필요한 사실**. 원장에서만 읽는다.

    ▶ 왜 필요한가 (2026-08-12)
      데이터셋을 고르는 길이 없었다. 에이전트는 `dataset_manifests` 를 직접 SQL
      로 뒤져야 했고, 그마저 "무엇을 덮는지"가 `source_versions` JSON 안에 있어
      한눈에 안 보였다. 그래서 기획자가 매니페스트가 안 덮는 원천
      (`market_quotes`)을 요구하는 가설을 계속 냈고 전부 실험 단계에서 죽었다.

      **고르려면 보여야 한다.** 이름·덮는 원천·기간·행수·종목수를 한 줄로 준다.
    """
    cur = meta_conn.cursor()
    cur.execute(_SQL_CATALOG)
    out = []
    for name, ver, srcs, rows, syms, parts, qual, as_of in cur.fetchall():
        if isinstance(srcs, str):
            srcs = json.loads(srcs or "{}")
        if isinstance(parts, str):
            parts = json.loads(parts or "[]")
        # ▶ `partitions` 는 매니페스트마다 **모양이 다르다** - 목록이기도 하고
        #   {파티션키: 메타} dict 이기도 하다. 한 모양만 가정하면 `KeyError: 0`
        #   으로 조회면 전체가 죽는다(2026-08-12 실측). 오늘만 세 번째다.
        #   모양이 셋이다: ①목록 ②{파티션키: 메타} ③{count, keys:[...]} 요약.
        #   ③을 dict 로 뭉뚱그리면 기간이 `count ~ keys` 로 찍힌다(실측).
        if isinstance(parts, dict):
            parts = parts.get("keys") if isinstance(parts.get("keys"), list) \
                else sorted(parts)
        parts = [str(p) for p in (parts or [])]
        if isinstance(qual, str):
            qual = json.loads(qual or "{}")
        checks = (qual or {}).get("checks") or {}
        warns = sorted(k for k, v in checks.items()
                       if isinstance(v, dict) and v.get("status") in ("WARN", "FAIL"))
        out.append({
            "dataset": f"{name}/{ver}",
            "sources": sorted(srcs or {}),
            "rows": int(rows or 0),
            # None = 유니버스가 안 걸렸다(못 셌다). 0 = 걸렸는데 비었다.
            "symbols": None if syms is None else int(syms),
            "span": (f"{parts[0]} ~ {parts[-1]}" if parts else "(파티션 없음)"),
            "partitions": len(parts or []),
            "warnings": warns,
            "as_of": str(as_of)[:19],
        })
    return out


def _print_catalog(rows: list[dict]) -> None:
    if not rows:
        print("등재된 데이터셋이 없다. `pit_dataset.py --build` 로 만든다.")
        return
    print(f"{'데이터셋':28} {'덮는 원천':26} {'기간':20} {'종목':>6} {'행수':>12}  경고")
    for r in rows:
        # 못 센 것을 0 으로 찍지 않는다 - `유니버스없음` 이 사실이다
        syms = f"{r['symbols']:,}" if r["symbols"] is not None else "유니버스없음"
        print(f"{r['dataset']:28} {','.join(r['sources'])[:26]:26} "
              f"{r['span']:20} {syms:>12} {r['rows']:>12,}  "
              f"{','.join(r['warnings']) or '-'}")
    print("\n▶ 실험은 **여기 있는 원천만** 쓸 수 있다. 없는 원천을 요구하면 "
          "`사상할 데이터셋이 없다` 로 막힌다.")
    print("▶ 필요한 것이 없으면 반려하지 말고 만든다: "
          "`pit_dataset.py --build --name <이름> --version <새 버전>`")


if __name__ == "__main__":
    if "--list" in sys.argv:
        import psycopg2                                # noqa: PLC0415
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                               / "01-research" / "collectors"))
        from source_registry import load_project_env   # noqa: PLC0415

        _c = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=20)
        try:
            _print_catalog(catalog(_c))
        finally:
            _c.close()
        raise SystemExit(0)
    raise SystemExit(_selfcheck())
