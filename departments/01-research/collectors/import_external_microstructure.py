"""외부 프로젝트 호가·체결을 우리 스키마로 이관한다 - PIT 계보를 남기면서.

담당: 재일 (리서치본부 RES 수집)
원천: Trading_bot 프로젝트 (trading-timescaledb / ticks DB / public.{ticks,quotes})
계보: market.pit_provenance

▶ 왜 그냥 복사하면 안 되나
  저쪽은 시각이 `ts` 하나뿐이고 밀리초가 전부 0 이다(2026-08-10 실측) - 초 단위로
  잘린 **거래소 시각**이지 "우리가 언제 알았는가" 가 아니다. 우리 스키마는
  event_time / received_at / observed_at 셋을 구분하는데, 저쪽엔 뒤의 둘이 없다.

  비워 두면 PIT 질의가 그 구간을 통째로 못 쓰고, 아무 값이나 채우면 **측정한
  것처럼 보인다.** 후자가 훨씬 위험하다 - 유도값을 측정값으로 오인한 백테스트는
  자기가 선견을 했는지도 모른다.

▶ 그래서 PIT 는 주장하지 않는다 (재일님 결정, 2026-08-10)
  지연을 추정해 observed_at 을 유도할 수도 있지만, 유도값은 결국 우리가 만든
  숫자다. 그걸 원장에 넣으면 나중에 누군가 측정값으로 읽는다. **PIT 는 앞으로
  수집하는 데이터에만 적용하고, 이 구간은 PIT 없음으로 못박는다.**

    received_at  = NULL            (재지 않았다)
    observed_at  = event_time      (자리 채움이지 관측 시각이 아니다)
    provider     = 'LS-IMPORT'     (행에서 바로 구분된다)
    provenance   = 'NONE'          (이 구간에 PIT 질의를 하면 안 된다)

  observed_at 이 NOT NULL 이라 event_time 을 넣지만, **그것이 PIT 라는 뜻은
  아니다** - 그 사실은 market.pit_provenance 가 기록한다. 이 구간을 PIT 가 필요한
  실험에 쓰면 안 된다는 판단은 data_resolution 이 그 표를 읽어 내린다.

▶ 데이터는 전부 교체한다
  우리가 모은 10거래일은 KRX 뿐이었고 저쪽은 72거래일에 NXT 까지 있다. 겹치는
  구간을 남겨 두면 같은 날 두 출처가 섞여 무엇을 근거로 쓴 실험인지 흐려진다.

▶ 저쪽은 읽기만 한다
  DDL·삭제·임시 테이블 생성 없음. 매핑과 변환은 전부 우리 쪽에서 한다.

사용
  python import_external_microstructure.py --plan            # 무엇을 가져올지만 보고
  python import_external_microstructure.py --run --date 2026-07-29
  python import_external_microstructure.py --run             # 남은 전 구간
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

MODULE_VERSION = "research-import-external-microstructure-v1"

SRC = ("trading-timescaledb", "trader", "ticks")      # (컨테이너, 사용자, DB)
DST = ("hedgefund-timescaledb", "postgres", "market")

# 이 구간은 PIT 가 없다. observed_at 은 event_time 을 그대로 넣는 자리 채움이고,
# 그 사실을 pit_provenance 가 'NONE' 으로 기록한다.
PIT_KIND = "NONE"


def _psql(target, sql: str, *, quiet=False) -> str:
    container, user, db = target
    cmd = ["docker", "exec", container, "psql", "-U", user, "-d", db,
           "-v", "ON_ERROR_STOP=1", "-A", "-F", "|", "-t", "-c", sql]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0:
        raise RuntimeError(f"psql 실패: {r.stderr.strip()[:400]}")
    if not quiet and r.stderr.strip():
        print("   ", r.stderr.strip()[:200])
    return r.stdout.strip()


# ── 종목 매핑 ──────────────────────────────────────────────────────────────
def ensure_symbol_map() -> int:
    """Supabase 의 reference.instrument_symbols 를 로컬로 복제한다.

    틱은 로컬 DB, 매핑은 클라우드 DB 에 있어 조인이 안 된다. 매핑을 로컬에 두면
    변환이 전부 SQL 한 문장으로 끝나고, 행마다 파이썬을 거치지 않아도 된다.
    """
    import psycopg2

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from source_registry import load_project_env

    _psql(DST, """
        create table if not exists market.symbol_map (
            symbol text primary key,
            instrument_id uuid not null
        )""")
    have = int(_psql(DST, "select count(*) from market.symbol_map") or 0)
    if have:
        return have

    conn = psycopg2.connect(load_project_env()["DATABASE_URL"], connect_timeout=30)
    cur = conn.cursor()
    # 같은 코드가 여러 행이면 is_primary 를 먼저, 그다음 최신 등록을 쓴다.
    cur.execute("""
        select distinct on (symbol) symbol, instrument_id
          from reference.instrument_symbols
         where symbol ~ '^[0-9]{6}$'
         order by symbol, is_primary desc nulls last, valid_from desc nulls last
    """)
    rows = cur.fetchall()
    conn.close()

    with tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False,
                                     encoding="utf-8", newline="") as f:
        for sym, iid in rows:
            f.write(f"{sym},{iid}\n")
        path = f.name
    subprocess.run(["docker", "cp", path, f"{DST[0]}:/tmp/symmap.csv"], check=True)
    os.unlink(path)
    _psql(DST, "\\copy market.symbol_map from '/tmp/symmap.csv' (format csv)"
          .replace("\\copy", "copy"))
    return int(_psql(DST, "select count(*) from market.symbol_map") or 0)


# ── 계획 ───────────────────────────────────────────────────────────────────
def source_days() -> list[str]:
    out = _psql(SRC, "select distinct ts::date from public.ticks order by 1")
    return [d.strip() for d in out.splitlines() if d.strip()]


def done_days(table: str) -> set[str]:
    out = _psql(DST, f"""
        select distinct observed_at::date from market.{table}
         where provider = 'LS-IMPORT'""")
    return {d.strip() for d in out.splitlines() if d.strip()}


# ── 이관 ───────────────────────────────────────────────────────────────────
_STAGE_TICKS = """
create unlogged table if not exists market._imp_ticks (
    ts timestamptz, symbol text, price bigint, volume bigint,
    side smallint, market text)
"""

_STAGE_QUOTES = """
create unlogged table if not exists market._imp_quotes (
    ts timestamptz, symbol text,
    a1 bigint,a2 bigint,a3 bigint,a4 bigint,a5 bigint,
    a6 bigint,a7 bigint,a8 bigint,a9 bigint,a10 bigint,
    b1 bigint,b2 bigint,b3 bigint,b4 bigint,b5 bigint,
    b6 bigint,b7 bigint,b8 bigint,b9 bigint,b10 bigint,
    av1 bigint,av2 bigint,av3 bigint,av4 bigint,av5 bigint,
    av6 bigint,av7 bigint,av8 bigint,av9 bigint,av10 bigint,
    bv1 bigint,bv2 bigint,bv3 bigint,bv4 bigint,bv5 bigint,
    bv6 bigint,bv7 bigint,bv8 bigint,bv9 bigint,bv10 bigint)
"""

_SEL_TICKS = ("select ts, trim(symbol), price, volume, side, trim(market) "
              "from public.ticks where ts >= '{d}' and ts < '{d}'::date + 1{venue}")

_SEL_QUOTES = ("select ts, trim(symbol), ask1,ask2,ask3,ask4,ask5,ask6,ask7,ask8,"
               "ask9,ask10, bid1,bid2,bid3,bid4,bid5,bid6,bid7,bid8,bid9,bid10, "
               "ask_vol1,ask_vol2,ask_vol3,ask_vol4,ask_vol5,ask_vol6,ask_vol7,"
               "ask_vol8,ask_vol9,ask_vol10, bid_vol1,bid_vol2,bid_vol3,bid_vol4,"
               "bid_vol5,bid_vol6,bid_vol7,bid_vol8,bid_vol9,bid_vol10 "
               "from public.quotes where ts >= '{d}' and ts < '{d}'::date + 1{venue}")

# ▶ received_at 은 NULL 이다. 0 이나 ts 를 넣으면 "재 봤더니 지연 0" 으로 읽힌다.
# ▶ observed_at 은 유도값이고 provider='LS-IMPORT' 가 그것을 표시한다 -
#   provenance 표를 안 봐도 행 자체에서 구분된다.
# ▶ PK 가 (event_time, source_event_id) 이므로 같은 초 안에서 유일해야 한다.
#   원본에 이벤트 id 가 없으니 내용에서 결정론적으로 만든다. 같은 초에 완전히
#   동일한 체결이 둘 있을 수 있어 row_number 로 가른다 - 순서가 뒤바뀌어도
#   **만들어지는 id 집합은 같으므로** 재실행이 멱등이다.
_INS_TICKS = f"""
insert into market.market_ticks
  (event_time, received_at, observed_at, instrument_id, provider, tr_code,
   market, price, quantity, side, source_event_id, schema_version)
select s.ts, null, s.ts, m.instrument_id,
       'LS-IMPORT', 'IMPORTED',
       case s.market when 'K' then 'KRX' when 'N' then 'NXT' else s.market end,
       s.price, s.volume,
       -- ▶ 체결구분 사상. 저쪽은 LS 원값(1=매수, 5=매도)을 그대로 담았고
       --   우리는 부호(1/-1)로 쓴다. 실측: 저쪽 값은 1 과 5 뿐이다.
       --   모르는 값은 짐작하지 않고 0(미상)으로 둔다 - 방향을 잘못 넣으면
       --   주문흐름 불균형이 통째로 뒤집힌다.
       case s.side when 1 then 1 when 5 then -1 else 0 end,
       md5(s.symbol||':'||s.market||':'||s.price||':'||s.volume||':'||s.side||':'||
           row_number() over (partition by s.ts
                              order by s.symbol, s.market, s.price, s.volume, s.side)),
       1
  from market._imp_ticks s
  join market.symbol_map m on m.symbol = s.symbol
on conflict do nothing
"""

_INS_QUOTES = f"""
insert into market.market_quotes
  (event_time, received_at, observed_at, instrument_id, provider, tr_code, market,
   bid_prices, bid_sizes, ask_prices, ask_sizes, total_bid_size, total_ask_size,
   best_bid, best_ask, mid_price, spread, depth_imbalance,
   source_event_id, schema_version)
select s.ts, null, s.ts, m.instrument_id,
       -- ▶ 저쪽 quotes 에는 거래소 컬럼이 아예 없다(ts, symbol, 10호가, 잔량,
       --   spread, bi 뿐). 그래서 venue 를 KRX 로 단정할 수 없고, 우리가 이미
       --   KRX 를 측정한 구간(07-30~)에는 이 표를 넣지 않는다 - 넣으면 어느
       --   거래소 호가인지 모르는 행이 측정 구간에 섞인다.
       'LS-IMPORT', 'IMPORTED', 'UNKNOWN',
       array[s.b1,s.b2,s.b3,s.b4,s.b5,s.b6,s.b7,s.b8,s.b9,s.b10]::numeric[],
       array[s.bv1,s.bv2,s.bv3,s.bv4,s.bv5,s.bv6,s.bv7,s.bv8,s.bv9,s.bv10]::numeric[],
       array[s.a1,s.a2,s.a3,s.a4,s.a5,s.a6,s.a7,s.a8,s.a9,s.a10]::numeric[],
       array[s.av1,s.av2,s.av3,s.av4,s.av5,s.av6,s.av7,s.av8,s.av9,s.av10]::numeric[],
       (s.bv1+s.bv2+s.bv3+s.bv4+s.bv5+s.bv6+s.bv7+s.bv8+s.bv9+s.bv10),
       (s.av1+s.av2+s.av3+s.av4+s.av5+s.av6+s.av7+s.av8+s.av9+s.av10),
       -- ▶ 호가가 없으면(0) NULL 이다. 0 을 가격으로 쓰면 스프레드가 음수로
       --   튀고 mid 가 반토막 난다 - 미측정과 0 을 구분하는 것과 같은 원칙이다.
       nullif(s.b1,0), nullif(s.a1,0),
       case when s.b1>0 and s.a1>0 then (s.b1+s.a1)/2.0 end,
       case when s.b1>0 and s.a1>0 then (s.a1-s.b1) end,
       case when (s.bv1+s.av1) > 0
            then (s.bv1-s.av1)::float8 / (s.bv1+s.av1) end,
       md5(s.symbol||':'||s.b1||':'||s.a1||':'||s.bv1||':'||s.av1||':'||
           row_number() over (partition by s.ts
                              order by s.symbol, s.b1, s.a1, s.bv1, s.av1)),
       1
  from market._imp_quotes s
  join market.symbol_map m on m.symbol = s.symbol
on conflict do nothing
"""


def _stream(day: str, *, table: str, venue: str) -> int:
    """저쪽 -> 파일 -> 우리 스테이징 -> 변환. 저쪽에는 쓰지 않는다."""
    stage = "_imp_ticks" if table == "market_ticks" else "_imp_quotes"
    sel = (_SEL_TICKS if table == "market_ticks" else _SEL_QUOTES).format(
        d=day, venue=venue)
    ins = _INS_TICKS if table == "market_ticks" else _INS_QUOTES

    _psql(DST, _STAGE_TICKS if table == "market_ticks" else _STAGE_QUOTES)
    _psql(DST, f"truncate market.{stage}")

    host_csv = Path(tempfile.gettempdir()) / f"imp_{table}_{day}.csv"
    with open(host_csv, "wb") as fh:
        p = subprocess.run(
            ["docker", "exec", SRC[0], "psql", "-U", SRC[1], "-d", SRC[2],
             "-v", "ON_ERROR_STOP=1", "-c", f"copy ({sel}) to stdout (format csv)"],
            stdout=fh, stderr=subprocess.PIPE)
    if p.returncode != 0:
        raise RuntimeError(f"원천 COPY 실패: {p.stderr.decode('utf-8','replace')[:300]}")

    subprocess.run(["docker", "cp", str(host_csv), f"{DST[0]}:/tmp/imp.csv"],
                   check=True)
    host_csv.unlink(missing_ok=True)
    _psql(DST, f"copy market.{stage} from '/tmp/imp.csv' (format csv)")
    staged = int(_psql(DST, f"select count(*) from market.{stage}") or 0)
    _psql(DST, ins)
    _psql(DST, f"truncate market.{stage}")
    return staged


def import_day(day: str) -> dict:
    """하루치 이관. 전 구간을 그대로 가져온다(교체이므로 지킬 측정본이 없다)."""
    out = {"day": day}
    out["market_ticks"] = _stream(day, table="market_ticks", venue="")
    out["market_quotes"] = _stream(day, table="market_quotes", venue="")
    return out


def wipe_existing() -> dict:
    """우리가 모은 호가·체결을 비운다. **교체이므로 남기지 않는다.**

    남겨 두면 같은 날 두 출처가 섞여 어느 것을 근거로 쓴 실험인지 흐려진다.
    삭제 전 규모를 찍어 두는 이유는, 지운 것이 무엇이었는지 로그에 남기기 위해서다.
    """
    before = {}
    for t in ("market_ticks", "market_quotes"):
        before[t] = int(_psql(DST, f"select count(*) from market.{t}") or 0)
        _psql(DST, f"truncate market.{t}")
    _psql(DST, "delete from market.pit_provenance")
    return before


def record_provenance(days: list[str]) -> None:
    if not days:
        return
    _psql(DST, f"""
        delete from market.pit_provenance where observed_at_kind = '{PIT_KIND}';
        insert into market.pit_provenance
          (source_table, range_start, range_end, observed_at_kind, derivation,
           origin, note)
        select t, '{min(days)}'::date, '{max(days)}'::date, '{PIT_KIND}',
               'observed_at = event_time (자리 채움). 관측 시각이 원본에 없다',
               'Trading_bot / trading-timescaledb public.{{ticks,quotes}}',
               'PIT 없음 - 이 구간은 시점 재현 실험에 쓸 수 없다. '
               'received_at 은 NULL, provider=LS-IMPORT 로 행에서도 구분된다'
          from unnest(array['market_ticks','market_quotes']) t
        on conflict (source_table, range_start, range_end) do nothing""")


def main(argv: list[str]) -> int:
    days = source_days()
    only = argv[argv.index("--date") + 1] if "--date" in argv else None
    if only:
        days = [d for d in days if d == only]

    # ▶ 삭제를 먼저 한다. 목록을 먼저 세면 방금 지운 날이 "이미 이관" 으로 남아
    #   할 일이 비어 버린다(2026-08-10 실측: 지우기만 하고 아무것도 안 가져왔다).
    if "--wipe" in argv and "--run" in argv:
        before = wipe_existing()
        print(f"  기존 삭제: ticks {before['market_ticks']:,} / "
              f"quotes {before['market_quotes']:,}")

    done = done_days("market_ticks")
    todo = [d for d in days if d not in done]
    print(f"{MODULE_VERSION}")
    print(f"  원천 거래일 {len(days)}건 / 이미 이관 {len(done)}건 / 남은 {len(todo)}건")
    if todo:
        print(f"  범위 {todo[0]} ~ {todo[-1]}   PIT: 없음(앞으로 수집분에만 적용)")
    if "--run" not in argv:
        print("  (--plan 모드 - 아무것도 하지 않았다)")
        return 0

    n = ensure_symbol_map()
    print(f"  종목 매핑 {n}건")

    imported: list[str] = []
    for i, d in enumerate(todo, 1):
        try:
            r = import_day(d)
            imported.append(d)
            print(f"  [{i}/{len(todo)}] {d}: "
                  f"ticks {r['market_ticks']:,} / quotes {r['market_quotes']:,}",
                  flush=True)
            record_provenance(imported)
        except Exception as e:                      # 하루가 실패해도 나머지는 간다
            print(f"  [{i}/{len(todo)}] {d} 실패: {str(e)[:180]}", flush=True)
    print(f"  이관 완료 {len(imported)}/{len(todo)}일")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
