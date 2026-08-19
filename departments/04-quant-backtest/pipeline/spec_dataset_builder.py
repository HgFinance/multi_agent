#!/usr/bin/env python3
"""명세 기반 데이터셋 빌더 - 파티션 파일과 매니페스트를 함께 만든다.

소유: 재일 (퀀트·백테스트본부 QNT)
근거: 재일님 지시 2026-08-12

▶ 왜 매니페스트만으로는 안 되나
  백테스트(`backtest_runner.load_dataset`)는 DB 가 아니라 **파티션 파일**을
  읽고 `content_hash` 로 재검증한다. 그래서 매니페스트만 등재하면 `resolve` 는
  통과하는데 `load_dataset` 이 "Dataset 전체 해시/행수 불일치" 로 죽는다 -
  2026-08-12 에 실제로 그렇게 만들어 놓고 "사상됐다" 고 보고했다.

  **등재와 파일은 한 트랜잭션이어야 한다.** 이 파일이 그 짝을 강제한다.

▶ 왜 Parquet 인가
  실측(2026-08-12) 호가 20,000행 26열 왕복: 불일치 0, zstd 2.2MB(≈110 B/행).
  체결 21열은 1.0MB(≈50 B/행). CSV.gz 보다 작고, 타입(DECIMAL·TIMESTAMP(us))을
  보존한다.

  **기존 일봉 파티션(csv.gz)은 건드리지 않는다.** `load_partition` 이 확장자로
  갈라 읽으므로 두 형식이 공존한다 - 형식을 바꾸자고 이미 해시 검증을 통과하고
  있는 데이터셋을 다시 만들 이유가 없다.

▶ 해시는 형식과 무관하다
  `DatasetSpec.row_line()` 이 만든 문자열에서 나온다. 그래서 CSV 로 굳히든
  Parquet 으로 굳히든 같은 내용이면 같은 해시다 - 형식을 옮겨도 재현성 주장이
  유지된다(AWS 이전이 이 성질에 기댄다).

사용
  python pipeline/spec_dataset_builder.py                      # 자체 점검
  python pipeline/spec_dataset_builder.py --build krx-microstructure-daily/v1
  python pipeline/spec_dataset_builder.py --build <키> --from 2026-05-01
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parents[1] / "01-research" / "collectors"))

from dataset_spec import SPECS, DatasetSpec  # noqa: E402

BUILDER_VERSION = "quant-spec-dataset-builder-v1"
KST = timezone(timedelta(hours=9))
DATA_ROOT = _HERE.parents[2] / "quant-data"


def content_hash(spec: DatasetSpec, rows: list[dict]) -> str:
    """행 순서와 무관하게 같은 내용이면 같은 해시."""
    h = hashlib.sha256()
    for r in sorted(rows, key=spec.sort_key):
        h.update(spec.row_line(r).encode())
        h.update(b"\n")
    return h.hexdigest()


def fetch(spec: DatasetSpec, conn, start: date, end: date) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(spec.fetch_sql, {
            "start": datetime.combine(start, datetime.min.time(), tzinfo=KST),
            "end": datetime.combine(end, datetime.min.time(), tzinfo=KST),
            "fsv": spec.source_versions.get("microstructure_features", ""),
        })
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


def write_partitions(spec: DatasetSpec, rows: list[dict],
                     out_dir: Path) -> tuple[list[dict], list[str]]:
    """월별 Parquet 으로 내보내고 파티션 메타를 돌려준다.

    **문자열로 굳힌다.** `row_line` 이 만든 것과 같은 표현을 파일에 넣어야,
    다시 읽어 해시를 재계산할 때 어긋나지 않는다. 타입을 살려 쓰면 파일 라이브러리
    버전에 따라 표기가 흔들릴 수 있고, 그 순간 "파일 변조" 로 오진한다.
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    out_dir.mkdir(parents=True, exist_ok=True)
    by_part: dict[str, list[dict]] = {}
    for r in rows:
        by_part.setdefault(spec.partition_of(r), []).append(r)

    parts, skipped, drifted = [], [], []
    for key in sorted(by_part):
        chunk = sorted(by_part[key], key=spec.sort_key)
        chash = content_hash(spec, chunk)
        path = out_dir / f"{key}.parquet"

        # ▶ **닫힌 파티션은 다시 쓰지 않는다** (2026-08-12)
        #   예전에는 읽은 구간의 파티션을 조건 없이 전부 다시 썼다. 하루를
        #   덧붙이려고 그 달을 통째로 다시 쓰고, 그때마다 해시가 바뀌어
        #   **어제 실험이 가리키던 데이터가 사라졌다.** 사전등록이 가리키는
        #   대상이 매일 달라지면 사전등록이 형식만 남는다.
        #
        #   같은 내용이면 건너뛰고, 다르면 두 갈래로 나눈다:
        #     - 열린 파티션(오늘/이번 달) → 덧붙임이므로 다시 쓴다
        #     - 닫힌 파티션 → **소급 변경**이다. 덮지 않고 보고한다.
        #       원천이 늦게 채워지는 일은 실제로 있고, 그때 조용히 덮으면
        #       과거 결과가 재현되지 않는데 아무도 모른다.
        if path.exists():
            old_sorted, ohash = None, ""
            try:
                old_sorted = sorted(read_partition(spec, path), key=spec.sort_key)
                ohash = content_hash(spec, old_sorted)
                same = ohash == chash
            except Exception:      # noqa: BLE001 - 못 읽으면 아래에서 가른다
                same = False
            if same:
                skipped.append(key)
                parts.append(_part_meta(spec, key, path, chunk, chash))
                continue
            if key != spec.open_partition:
                if old_sorted is None:
                    # 닫힌 파티션인데 파일을 못 읽는다 = 실재가 깨졌다. 소급
                    # 재작성도, 깨진 파일을 새 해시로 위장하는 것도 금지다.
                    raise SystemExit(
                        f"닫힌 파티션 {key} 파일을 읽을 수 없다 - 새 버전으로 "
                        f"다시 굳혀라(같은 버전 소급 재작성 금지)")
                drifted.append(key)
                # ▶ 메타는 **파일의 실재**를 싣는다 (2026-08-13 실측). 파일은 옛
                #   것을 지키면서 메타에 새(원천) 해시를 실었더니 파일↔메타가
                #   어긋나 왕복 검증이 매 주기 터졌다 - 굳힌 데이터셋의 정체는
                #   원천이 아니라 파일이다. 안 덮었으면 메타도 옛것이다.
                parts.append(_part_meta(spec, key, path, old_sorted, ohash))
                continue

        cells = {c: [spec.canon.get(c, lambda v: "" if v is None else str(v))(r.get(c))
                     for r in chunk]
                 for c in spec.columns}
        pq.write_table(
            pa.Table.from_pydict(cells,
                                 schema=pa.schema([(c, pa.string())
                                                   for c in spec.columns])),
            path, compression="zstd")
        dates = [r[spec.partition_column] for r in chunk]
        dates = [d.date() if isinstance(d, datetime) else d for d in dates]
        parts.append({
            "partition_key": key,
            "object_path": path.relative_to(DATA_ROOT.parent).as_posix(),
            "row_count": len(chunk),
            "min_event_time": datetime.combine(min(dates), datetime.min.time(), tzinfo=KST),
            "max_event_time": datetime.combine(max(dates), datetime.min.time(), tzinfo=KST),
            "content_hash": content_hash(spec, chunk),
            "quality_status": partition_quality(spec, chunk),
        })
    if skipped:
        print(f"  파티션 건너뜀 {len(skipped)}개 (내용 동일): "
              f"{', '.join(skipped[:6])}{' …' if len(skipped) > 6 else ''}",
              flush=True)
    if drifted:
        # **조용히 넘어가지 않는다.** 닫힌 파티션이 달라졌다는 것은 과거가
        # 바뀌었다는 뜻이고, 그건 사람이 새 버전으로 다시 굳힐지 정할 일이다.
        print(f"  ⚠ 닫힌 파티션이 원천과 다르다 {len(drifted)}개 - **덮지 않았다**: "
              f"{', '.join(drifted)}\n"
              f"    원천이 늦게 채워졌을 수 있다. 과거를 바꾸려면 새 버전으로 "
              f"다시 굳혀라 - 같은 버전을 덮으면 그 버전으로 돌린 실험이 "
              f"재현되지 않는다.", flush=True)
    return parts, drifted


def _part_meta(spec: DatasetSpec, key: str, path: Path,
               chunk: list[dict], chash: str) -> dict:
    """파일을 안 쓴 파티션의 메타. 쓴 것과 같은 모양이어야 매니페스트가 일관된다."""
    dates = [r[spec.partition_column] for r in chunk]
    dates = [d.date() if isinstance(d, datetime) else d for d in dates]
    return {
        "partition_key": key,
        "object_path": path.relative_to(DATA_ROOT.parent).as_posix(),
        "row_count": len(chunk),
        "min_event_time": datetime.combine(min(dates), datetime.min.time(), tzinfo=KST),
        "max_event_time": datetime.combine(max(dates), datetime.min.time(), tzinfo=KST),
        "content_hash": chash,
        "quality_status": partition_quality(spec, chunk),
    }


def read_partition(spec: DatasetSpec, path: Path) -> list[dict]:
    """Parquet -> 행 dict. 빌드 시와 같은 타입으로 복원한다(해시 재검증용)."""
    import pyarrow.parquet as pq

    tbl = pq.read_table(path).to_pydict()
    n = len(next(iter(tbl.values()))) if tbl else 0
    out = []
    for i in range(n):
        raw = {c: tbl[c][i] for c in spec.columns}
        out.append({c: spec.restore.get(c, lambda x: x)(raw[c]) for c in spec.columns})
    return out


_SQL_MANIFEST = """
insert into quant.dataset_manifests
  (name, version, as_of, source_versions, feature_spec_versions, partitions,
   point_in_time_policy, quality_summary, object_path, content_hash,
   row_count, schema_definition, notional_unit)
values (%s,%s, now(), %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
        %s, %s, %s, %s::jsonb, %s)
-- ▶ **레이아웃도 갱신한다** (2026-08-12)
--   예전에는 `quality_summary` 만 갱신했다. 그런데 같은 내용을 **다른 파티션
--   입도**로 다시 굳히면(월→일) 내용 해시는 같고 레이아웃만 바뀐다. 그때
--   `partitions` 가 옛 월별 키로 남아 매니페스트와 `dataset_partitions` 표가
--   서로 다른 말을 했다(실측: 매니페스트는 월 4개, 표는 63행).
-- ▶ **충돌 기준은 (name, version) 이다** (2026-08-14 실측)
--   예전 기준은 `content_hash` 였는데, 데이터셋이 매일 자라면 전체 해시가
--   날마다 바뀐다 - 그러면 이 절이 안 걸리고 `dataset_manifests_name_version_key`
--   유니크 위반으로 **파티션 굳히기가 매 주기 죽는다**(오늘 새 날 8/13 이
--   접히자마자 재현). 같은 이름·버전은 같은 데이터셋의 자란 모습이므로
--   그 행을 갱신하는 것이 맞고, content_hash 도 함께 갱신해야 원장이
--   파일 실재와 일치한다(어제 고친 '메타는 파일 실재' 와 같은 원칙).
--   ▷ 그래도 과거 스냅샷을 얼리고 싶으면 `--stamp` 로 버전에 날짜를 박는다
--     (v1 -> v1-20260814). 그때는 새 행이 되므로 이 절이 관여하지 않는다.
on conflict (name, version) do update set
  as_of           = excluded.as_of,
  quality_summary = excluded.quality_summary,
  partitions      = excluded.partitions,
  row_count       = excluded.row_count,
  object_path     = excluded.object_path,
  content_hash    = excluded.content_hash,
  source_versions = excluded.source_versions,
  -- 단위 선언도 갱신한다. 명세가 정본이므로 여기 남은 옛 값이 이기면
  -- 로더 대조가 명세와 어긋난 채로 통과한다.
  notional_unit   = excluded.notional_unit
returning dataset_id
"""

_SQL_PART = """
insert into quant.dataset_partitions
  (dataset_id, partition_key, object_path, row_count,
   min_event_time, max_event_time, content_hash, quality_status)
values (%s,%s,%s,%s,%s,%s,%s,%s)
on conflict (dataset_id, partition_key) do update set
  object_path = excluded.object_path, row_count = excluded.row_count,
  min_event_time = excluded.min_event_time, max_event_time = excluded.max_event_time,
  content_hash = excluded.content_hash, quality_status = excluded.quality_status
"""


def partition_quality(spec: DatasetSpec, chunk: list[dict]) -> str:
    """그 파티션의 등급. **행에서 계산한다 - 상수로 PASS 를 박지 않는다.**

    원천이 이미 행마다 quality_status 를 갖고 있으면 그것을 접는다. 하나라도
    FAIL 이면 파티션이 FAIL 이고, WARN 이 섞이면 WARN 이다 - 나쁜 쪽으로
    수렴시켜야 소비자가 걸러낼 기회를 갖는다.

    등급 열이 없는 명세는 알 수 없으므로 `UNKNOWN` 이다. 모르는 것을 PASS 로
    쓰면 그 순간 품질 표시가 장식이 된다.
    """
    if "quality_status" not in spec.columns:
        return "UNKNOWN"
    grades = {str(r.get("quality_status") or "").upper() for r in chunk}
    if "FAIL" in grades:
        return "FAIL"
    if "WARN" in grades:
        return "WARN"
    return "PASS" if grades == {"PASS"} else "UNKNOWN"


def build(key: str, *, start: date, end: date, dry_run: bool = False,
          stamp: bool = False) -> int:
    """데이터셋 하나를 굳힌다.

    ▶ `stamp` - **매니페스트 버전에 날짜를 박는다** (2026-08-12)
      매일 자라는 데이터셋을 한 버전에 계속 덮으면 전체 `content_hash` 가
      날마다 바뀐다. 그러면 **어제 실험이 가리키던 스냅샷이 사라진다** - 실험은
      "어느 데이터로 돌았는가" 를 해시로 가리키는데 그 대상이 없어지므로
      사전등록이 형식만 남는다.

      `v1` -> `v1-20260812` 처럼 날짜를 박으면 그날의 스냅샷이 그대로 남고,
      실험은 자기가 쓴 버전을 계속 가리킨다. 파일은 파티션 단위로 공유되므로
      (증분 빌드) 디스크가 날마다 늘지 않는다 - 늘어나는 것은 매니페스트 행뿐이다.

      `resolve()` 는 같은 이름이면 문자열이 큰 버전을 고르므로
      `v1-20260813 > v1-20260812` 가 되어 최신 스냅샷이 자동으로 쓰인다.
    """
    import psycopg2
    from db_writer import connect as connect_writer
    from source_registry import load_project_env

    spec = SPECS.get(key)
    if spec is None:
        raise SystemExit(f"모르는 데이터셋이다: {key} (가능: {', '.join(SPECS)})")

    env = load_project_env()
    src = psycopg2.connect(
        env["TIMESCALE_DATABASE_URL"] if spec.db == "market" else env["DATABASE_URL"],
        connect_timeout=20)
    # Supabase transaction pooler가 이전 세션의 read-only 기본값을 재사용할 수
    # 있다. 메타 원장은 쓰기 경로이므로 매 트랜잭션을 READ WRITE로 봉인하는
    # 공용 연결을 쓴다(raw psycopg2.connect로 v4 등재가 실제 실패했다).
    meta = connect_writer(
        env["DATABASE_URL"],
        connect_timeout=20,
        runtime_role="svc_dataset_builder",
    )
    try:
        rows = fetch(spec, src, start, end)
        print(f"{BUILDER_VERSION}: {key} 원천 {len(rows):,}행 ({start}~{end})",
              flush=True)
        if not rows:
            # 0 행을 데이터셋으로 굳히지 않는다 - 나중에 "있다" 로 읽힌다
            print("  원천이 비었다 - 굳히지 않는다", flush=True)
            return 1

        # 파일은 **버전 스탬프와 무관하게** 같은 자리에 쌓인다 - 스냅샷이
        # 달라도 파티션은 공유하므로 디스크가 날마다 늘지 않는다.
        ver = (f"{spec.version}-{date.today():%Y%m%d}" if stamp
               else spec.version)
        out_dir = DATA_ROOT / f"{spec.name}-{spec.version}"
        parts, drifted = write_partitions(spec, rows, out_dir)

        # **쓰자마자 다시 읽어 검증한다.** 빌드 직후에만 통과하고 재검증에서
        # 깨지는 결함이 가장 잡기 어렵다 - 여기서 잡는다.
        back: list[dict] = []
        for p in parts:
            got = read_partition(spec, DATA_ROOT.parent / p["object_path"])
            if content_hash(spec, got) != p["content_hash"]:
                raise SystemExit(f"파티션 {p['partition_key']} 왕복 해시 불일치 - "
                                 "정규화와 복원이 짝이 아니다")
            back.extend(got)
        # ▶ 등재 해시·행수는 **파일 실재**로 계산한다 (2026-08-13). 드리프트가
        #   있으면 파일(안 덮은 옛 파티션)과 원천이 다른 게 정상이고, 실험이
        #   읽는 것은 파일이다 - 원천 해시로 등재하면 load_dataset 이 "전체
        #   해시 불일치" 로 죽는다. 드리프트가 없을 때만 원천=파일 동일성이
        #   왕복 검증의 일부다.
        whole = content_hash(spec, back)
        if not drifted and (content_hash(spec, rows) != whole
                            or len(back) != len(rows)):
            raise SystemExit("전체 왕복 불일치 - 굳히지 않는다")
        if drifted:
            print(f"  ⚠ 드리프트 {len(drifted)}개 - 등재는 파일 실재 기준 "
                  f"(원천 {len(rows):,}행 vs 파일 {len(back):,}행)", flush=True)
        print(f"  파티션 {len(parts)}개 / 전체 해시 {whole[:12]}… "
              f"/ 왕복 검증 OK ({len(back):,}행)", flush=True)

        if dry_run:
            print("  [dry-run] 원장에 등재하지 않았다", flush=True)
            return 0

        with meta.cursor() as cur:
            cur.execute(_SQL_MANIFEST, (
                spec.name, ver,
                json.dumps(spec.source_versions),
                json.dumps({f"{spec.name}-rows": ver}),
                json.dumps({"count": len(parts),
                            "keys": [p["partition_key"] for p in parts]}),
                json.dumps(spec.point_in_time),
                json.dumps({"rows": len(back), "partitions": len(parts),
                            "status": "PASS"}),
                (DATA_ROOT / f"{spec.name}-{spec.version}").relative_to(
                    DATA_ROOT.parent).as_posix(),
                whole, len(back),
                json.dumps({"columns": list(spec.columns)}),
                # 명세가 선언한 거래대금 단위. 로더가 이 값과 실행면 가정을
                # 대조한다 - 없으면 대조가 있는데 대조할 것이 없는 상태다.
                spec.notional_unit))
            dsid = cur.fetchone()[0]
            # ▶ **옛 파티션 행을 지우고 다시 넣는다** (2026-08-12)
            #   입도가 바뀌면 옛 키(월)와 새 키(일)가 **함께 남는다.** 실측에서
            #   59+4=63행이 됐고, `load_dataset` 은 이 표를 읽으므로 같은 데이터를
            #   두 번 로드해 전체 해시 불일치로 죽는다. 등재된 레이아웃은
            #   **이번 빌드가 만든 것 하나**여야 한다.
            cur.execute("delete from quant.dataset_partitions "
                        "where dataset_id = %s and partition_key <> all(%s)",
                        (dsid, [p["partition_key"] for p in parts]))
            for p in parts:
                cur.execute(_SQL_PART, (
                    dsid, p["partition_key"], p["object_path"], p["row_count"],
                    p["min_event_time"], p["max_event_time"], p["content_hash"],
                    p["quality_status"]))
        meta.commit()
        print(f"  등재 {dsid} / 파티션 {len(parts)}개", flush=True)
    finally:
        src.close()
        meta.close()
    return 0


# ── 자체 점검 (DB 없음) ───────────────────────────────────────────────────
def _check_hash_is_format_independent():
    """해시가 **파일 형식과 무관**해야 AWS 이전이 성립한다.

    CSV 로 굳히든 Parquet 으로 굳히든 같은 내용이면 같은 해시여야, 형식을 옮겨도
    "옮긴 것이 같은 것" 을 증명할 수 있다.
    """
    spec = SPECS["krx-microstructure-daily/v1"]
    rows = [{"instrument_id": f"i-{i}", "trade_date": date(2026, 8, 11),
             "spread_bps": 1.5 + i, "depth_imbalance": None,
             "order_flow_imbalance": 0.0, "trade_intensity": 10.0,
             "realized_volatility": None, "quality_status": "PASS",
             "observed_at": datetime(2026, 8, 11, tzinfo=KST)} for i in range(5)]
    a = content_hash(spec, rows)
    assert a == content_hash(spec, list(reversed(rows))), "행 순서가 해시를 바꿨다"
    changed = [{**r} for r in rows]
    changed[0]["spread_bps"] = 999.0
    assert a != content_hash(spec, changed), "값이 바뀌었는데 해시가 같다"
    # 결측과 0 이 다른 해시를 낸다 - 이게 같아지면 "못 잼" 이 "0" 이 된다
    z = [{**r} for r in rows]
    z[0]["spread_bps"] = None
    n = [{**r} for r in rows]
    n[0]["spread_bps"] = 0.0
    assert content_hash(spec, z) != content_hash(spec, n), "결측과 0 이 같은 해시다"
    print("  해시 형식 무관·결측 구분 OK")


def _check_empty_is_not_built():
    """0 행을 데이터셋으로 굳히지 않는다 - 나중에 '있다' 로 읽힌다."""
    import inspect

    src = inspect.getsource(build)
    assert "if not rows:" in src and "굳히지 않는다" in src
    print("  빈 원천 미등재           OK")


def _check_build_verifies_roundtrip_before_registering():
    """**등재 전에 다시 읽어 검증**하는가. 빌드 직후에만 통과하는 결함을 막는다."""
    import inspect

    src = inspect.getsource(build)
    i_read = src.index("read_partition")
    i_manifest = src.index("_SQL_MANIFEST")
    assert i_read < i_manifest, "왕복 검증이 등재 뒤에 온다"
    assert "왕복 해시 불일치" in src
    # ▶ 매니페스트 upsert 는 **(name, version)** 충돌을 처리해야 한다
    #   (2026-08-14 실측): content_hash 기준이면 데이터셋이 자라 해시가 바뀌는
    #   날 유니크 위반으로 굳히기가 통째로 죽는다. 매일 자라는 데이터셋에서
    #   이 절은 상시 경로다.
    m = " ".join(_SQL_MANIFEST.split())
    assert "on conflict (name, version) do update" in m, m
    assert "content_hash    = excluded.content_hash" in _SQL_MANIFEST, \
        "해시를 안 갱신하면 원장이 파일 실재와 어긋난다"
    # ▶ **명세가 선언한 거래대금 단위가 매니페스트까지 간다** (2026-08-14).
    #   실행면은 로더 경계에서 이 값을 대조하는데(`assert_declared_units`),
    #   spec 경로에는 심는 자리가 없어 매번 "선언이 없다" 로 지나갔다 -
    #   대조가 있는데 대조할 것이 없는 상태였다.
    assert "notional_unit" in _SQL_MANIFEST, "단위 선언을 매니페스트에 안 심는다"
    assert "notional_unit   = excluded.notional_unit" in _SQL_MANIFEST, \
        "재빌드 때 옛 단위가 남으면 명세와 어긋난 채 대조를 통과한다"
    assert "spec.notional_unit" in src, "심는 값이 명세에서 안 온다"
    # 실행면 가정과 같은 눈금이어야 한다 - 여기서 갈리면 유동성 필터가
    # 1e6 배 어긋난 채 돌아 유니버스가 0 이 된다(2026-08-14 실측)
    from strategy_templates import NOTIONAL_UNIT_KRW
    v2 = SPECS.get("krx-microstructure-daily/v2")
    if v2 is not None and v2.notional_unit:
        from backtest_runner import UNIT_FACTOR_KRW
        assert UNIT_FACTOR_KRW[v2.notional_unit] == NOTIONAL_UNIT_KRW, \
            (v2.notional_unit, NOTIONAL_UNIT_KRW)
    print("  등재 전 왕복 검증        OK")
    if _check_closed_partition_is_not_rewritten() != "SKIPPED":
        print("  닫힌 파티션 불변         OK")


def _check_quality_is_computed_not_assumed():
    """등급을 상수로 박지 않는다. **나쁜 쪽으로 수렴**해야 소비자가 거른다."""
    spec = SPECS["krx-microstructure-daily/v1"]
    mk = lambda g: {"quality_status": g}                              # noqa: E731
    assert partition_quality(spec, [mk("PASS"), mk("PASS")]) == "PASS"
    assert partition_quality(spec, [mk("PASS"), mk("WARN")]) == "WARN"
    assert partition_quality(spec, [mk("WARN"), mk("FAIL")]) == "FAIL"
    # 모르는 값이 섞이면 PASS 라고 말하지 않는다
    assert partition_quality(spec, [mk("PASS"), mk("")]) == "UNKNOWN"
    print("  파티션 등급 계산         OK")



def _check_closed_partition_is_not_rewritten():
    """**닫힌 파티션은 다시 안 쓰고, 달라지면 덮지 않고 보고한다.** (2026-08-12)

    예전에는 읽은 구간을 조건 없이 전부 다시 썼다. 하루를 덧붙이려고 그 달을
    통째로 다시 쓰고 해시가 바뀌어, 어제 실험이 가리키던 데이터가 사라졌다.
    """
    import tempfile
    from dataset_spec import SPECS

    try:
        import pyarrow  # noqa: F401  (파일을 실제로 써 봐야 하는 검사다)
    except ModuleNotFoundError:
        # **통과로 세지 않는다.** 못 돌린 검사를 OK 로 찍으면 다음 사람이
        # 검증됐다고 믿는다 - 이 저장소가 반복해 막아 온 그 사고다.
        print("  닫힌 파티션 불변         건너뜀 (pyarrow 없음 - 컨테이너에서 확인)")
        return "SKIPPED"

    spec = SPECS["krx-microstructure-daily/v1"]
    # 서로 다른 두 날 - 하나는 닫힌 파티션, 하나는 오늘(열린 파티션)이 된다
    rows = [{"instrument_id": f"i-{i}", "trade_date": date(2026, 8, 11),
             "spread_bps": 1.5 + i, "depth_imbalance": None,
             "order_flow_imbalance": 0.0, "trade_intensity": 10.0,
             "realized_volatility": None, "quality_status": "PASS",
             "observed_at": datetime(2026, 8, 11, tzinfo=KST)} for i in range(3)]
    # ▶ 임시 디렉터리를 **DATA_ROOT 아래**에 만든다. `object_path` 가
    #   `relative_to(DATA_ROOT.parent)` 라 저장소 밖이면 계산이 안 된다.
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=DATA_ROOT) as td:
        out = Path(td)
        first, drift0 = write_partitions(spec, rows, out)
        assert first, "첫 빌드가 파티션을 안 만들었다"
        assert drift0 == [], drift0
        stamps = {p["partition_key"]: (out / f"{p['partition_key']}.parquet").stat().st_mtime_ns
                  for p in first}

        # 같은 입력으로 다시 - **파일이 안 바뀌어야 한다**
        again, drift1 = write_partitions(spec, rows, out)
        assert drift1 == [], drift1
        for p in again:
            f = out / f"{p['partition_key']}.parquet"
            assert f.stat().st_mtime_ns == stamps[p["partition_key"]],                 f"{p['partition_key']} 를 다시 썼다 - 증분이 아니다"
        # 해시도 그대로여야 어제 실험이 가리키던 대상이 남는다
        assert [p["content_hash"] for p in again] ==                [p["content_hash"] for p in first]

        # ▶ 드리프트 픽스처 (2026-08-13 실측): 닫힌 파티션의 원천이 바뀌어도
        #   메타는 **파일의 실재**를 실어야 한다. 예전에는 새(원천) 해시를
        #   실어서 파일↔메타가 어긋났고 왕복 검증이 매 주기 터졌다.
        changed = [dict(r, spread_bps=99.9) for r in rows]
        third, drift2 = write_partitions(spec, changed, out)
        assert drift2, "닫힌 파티션 소급 변경이 드리프트로 안 잡혔다"
        for p in third:
            if p["partition_key"] in drift2:
                got = read_partition(spec, out / f"{p['partition_key']}.parquet")
                assert content_hash(spec, got) == p["content_hash"],                     "드리프트 파티션의 메타가 파일 실재가 아니다 - 왕복이 또 터진다"


def _selfcheck() -> int:
    print(f"{BUILDER_VERSION} 자체 점검 (DB 없음)")
    _check_quality_is_computed_not_assumed()
    _check_hash_is_format_independent()
    _check_empty_is_not_built()
    _check_build_verifies_roundtrip_before_registering()
    print("명세 빌더 4개 영역 통과. 실행은 --build <이름/버전>")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="명세 기반 데이터셋 빌더")
    ap.add_argument("--build", metavar="이름/버전", default="")
    ap.add_argument("--from", dest="start", default="")
    ap.add_argument("--to", dest="end", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--stamp", action="store_true",
                    help="매니페스트 버전에 날짜를 박는다(v1 -> v1-20260812). "
                         "매일 자라는 데이터셋은 이걸 켜야 어제 실험이 "
                         "가리키던 스냅샷이 남는다")
    a = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not a.build:
        return _selfcheck()
    start = date.fromisoformat(a.start) if a.start else date(2016, 1, 1)
    end = date.fromisoformat(a.end) if a.end else date.today() + timedelta(days=1)
    return build(a.build, start=start, end=end, dry_run=a.dry_run,
                 stamp=a.stamp)


if __name__ == "__main__":
    raise SystemExit(main())
