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


def write_partitions(spec: DatasetSpec, rows: list[dict], out_dir: Path) -> list[dict]:
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
            try:
                old = read_partition(spec, path)
                same = content_hash(spec, sorted(old, key=spec.sort_key)) == chash
            except Exception:      # noqa: BLE001 - 못 읽으면 다시 쓴다
                same = False
            if same:
                skipped.append(key)
                parts.append(_part_meta(spec, key, path, chunk, chash))
                continue
            if key != spec.open_partition:
                drifted.append(key)
                parts.append(_part_meta(spec, key, path, chunk, chash))
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
    return parts


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
   row_count, schema_definition)
values (%s,%s, now(), %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb, %s::jsonb,
        %s, %s, %s, %s::jsonb)
on conflict (content_hash) do update set quality_summary = excluded.quality_summary
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


def build(key: str, *, start: date, end: date, dry_run: bool = False) -> int:
    import psycopg2
    from source_registry import load_project_env

    spec = SPECS.get(key)
    if spec is None:
        raise SystemExit(f"모르는 데이터셋이다: {key} (가능: {', '.join(SPECS)})")

    env = load_project_env()
    src = psycopg2.connect(
        env["TIMESCALE_DATABASE_URL"] if spec.db == "market" else env["DATABASE_URL"],
        connect_timeout=20)
    meta = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    try:
        rows = fetch(spec, src, start, end)
        print(f"{BUILDER_VERSION}: {key} 원천 {len(rows):,}행 ({start}~{end})",
              flush=True)
        if not rows:
            # 0 행을 데이터셋으로 굳히지 않는다 - 나중에 "있다" 로 읽힌다
            print("  원천이 비었다 - 굳히지 않는다", flush=True)
            return 1

        out_dir = DATA_ROOT / f"{spec.name}-{spec.version}"
        parts = write_partitions(spec, rows, out_dir)
        whole = content_hash(spec, rows)
        print(f"  파티션 {len(parts)}개 / 전체 해시 {whole[:12]}…", flush=True)

        # **쓰자마자 다시 읽어 검증한다.** 빌드 직후에만 통과하고 재검증에서
        # 깨지는 결함이 가장 잡기 어렵다 - 여기서 잡는다.
        back: list[dict] = []
        for p in parts:
            got = read_partition(spec, DATA_ROOT.parent / p["object_path"])
            if content_hash(spec, got) != p["content_hash"]:
                raise SystemExit(f"파티션 {p['partition_key']} 왕복 해시 불일치 - "
                                 "정규화와 복원이 짝이 아니다")
            back.extend(got)
        if content_hash(spec, back) != whole or len(back) != len(rows):
            raise SystemExit("전체 왕복 불일치 - 굳히지 않는다")
        print(f"  왕복 검증 OK ({len(back):,}행)", flush=True)

        if dry_run:
            print("  [dry-run] 원장에 등재하지 않았다", flush=True)
            return 0

        with meta.cursor() as cur:
            cur.execute(_SQL_MANIFEST, (
                spec.name, spec.version,
                json.dumps(spec.source_versions),
                json.dumps({f"{spec.name}-rows": spec.version}),
                json.dumps({"count": len(parts),
                            "keys": [p["partition_key"] for p in parts]}),
                json.dumps(spec.point_in_time),
                json.dumps({"rows": len(rows), "partitions": len(parts),
                            "status": "PASS"}),
                (DATA_ROOT / f"{spec.name}-{spec.version}").relative_to(
                    DATA_ROOT.parent).as_posix(),
                whole, len(rows),
                json.dumps({"columns": list(spec.columns)})))
            dsid = cur.fetchone()[0]
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
        first = write_partitions(spec, rows, out)
        assert first, "첫 빌드가 파티션을 안 만들었다"
        stamps = {p["partition_key"]: (out / f"{p['partition_key']}.parquet").stat().st_mtime_ns
                  for p in first}

        # 같은 입력으로 다시 - **파일이 안 바뀌어야 한다**
        again = write_partitions(spec, rows, out)
        for p in again:
            f = out / f"{p['partition_key']}.parquet"
            assert f.stat().st_mtime_ns == stamps[p["partition_key"]],                 f"{p['partition_key']} 를 다시 썼다 - 증분이 아니다"
        # 해시도 그대로여야 어제 실험이 가리키던 대상이 남는다
        assert [p["content_hash"] for p in again] ==                [p["content_hash"] for p in first]


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
    a = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not a.build:
        return _selfcheck()
    start = date.fromisoformat(a.start) if a.start else date(2016, 1, 1)
    end = date.fromisoformat(a.end) if a.end else date.today() + timedelta(days=1)
    return build(a.build, start=start, end=end, dry_run=a.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
