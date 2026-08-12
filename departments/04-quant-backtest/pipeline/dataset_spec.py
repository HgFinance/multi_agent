#!/usr/bin/env python3
"""데이터셋 원천 명세 - 하나의 빌더가 여러 원천을 굳히게 한다.

소유: 재일 (퀀트·백테스트본부 QNT)
근거: 재일님 지시 2026-08-12 "작업 한번 진행해줘"

▶ 왜 필요했나
  `pit_dataset.py` 는 **일봉 전용**이었다. `row_line()`·`COLUMNS`·`trade_date`
  가 봉 스키마에 박혀 있어, 마이크로구조 피처를 같은 방식으로 굳힐 수가 없었다.

  그래서 2026-08-12 에 `krx-microstructure-daily/v1` 매니페스트만 등재하고
  파티션 파일을 못 만들었고, `resolve` 는 통과하는데 `load_dataset` 이
  **"Dataset 전체 해시/행수 불일치"** 로 죽었다. 레지스트리엔 등재됐는데 값이
  안 나오는 상태 - 이 저장소가 가장 나쁘다고 부르는 그것이다.

▶ 명세가 정하는 것
  ① 어느 DB 의 무엇을 읽는가 (fetch SQL)
  ② 한 행을 어떤 문자열로 정규화하는가 (해시의 근거)
  ③ 무엇으로 파티션을 가르는가 (보통 월)
  ④ 파일에서 읽어들일 때 타입을 어떻게 복원하는가

  ②와 ④가 **짝**이어야 한다. 짝이 아니면 빌드 직후엔 통과하고 다시 읽을 때
  해시가 어긋난다 - 재현성 검사가 자기 자신을 못 통과하는 상태가 된다.

▶ 값 정규화는 한 곳에서만
  `canon_number` 는 `pit_dataset` 것을 그대로 쓴다. 두 벌이 되면 같은 값이
  경로마다 다른 해시를 갖는다.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

SPEC_VERSION = "quant-dataset-spec-v1"
KST = timezone(timedelta(hours=9))


def canon_number(raw) -> str:
    """`pit_dataset.canon_number` 와 **같은 규칙**. 지수 표기를 피한 고정 문자열.

    여기서 다시 구현하지 않고 임포트하고 싶지만, pit_dataset 이 이 모듈을
    임포트하게 되면 순환이 된다. 그래서 규칙을 복사하되 자체 점검이 두 구현이
    같은 값을 내는지 대조한다(_check_canon_matches_pit_dataset).
    """
    if raw is None:
        return ""
    d = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    return format(d.normalize(), "f")


def canon_time(v) -> str:
    """시각은 **UTC ISO** 로 굳힌다. 지역 시각으로 쓰면 서버 tz 에 따라 해시가 흔들린다."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc).isoformat()
    return str(v)


@dataclass(frozen=True)
class DatasetSpec:
    """한 데이터셋을 굳히는 데 필요한 전부."""

    name: str
    version: str
    # 어느 연결로 읽는가: 'market'(TimescaleDB) | 'meta'(Supabase)
    db: str
    # (start, end) 를 받아 SQL 과 파라미터를 돌려준다
    fetch_sql: str
    # 열 이름. 파일 헤더이자 fetch 결과의 순서다.
    columns: tuple[str, ...]
    # 정렬·해시의 키가 되는 열(순서 무관 해시를 위해)
    key_columns: tuple[str, ...]
    # 파티션을 가르는 열(날짜여야 한다)
    partition_column: str
    # 열 이름 -> 정규화 함수. 없으면 str().
    canon: dict[str, Callable[[Any], str]]
    # 파일에서 읽을 때 타입 복원. 없으면 문자열 그대로.
    restore: dict[str, Callable[[str], Any]]
    # 이 데이터셋이 무슨 원천을 재료로 쓰는가 (매니페스트 source_versions)
    source_versions: dict[str, str]
    # PIT 정책. 이관·유도 구간은 여기서 사실대로 선언한다.
    point_in_time: dict[str, Any]
    # 파티션 입도: "month" | "day". **기본은 month** - 기존 데이터셋의 파티션
    # 키와 해시를 그대로 두기 위해서다(바꾸면 사실상 새 데이터셋이 된다).
    #
    # ▶ 왜 day 가 필요한가 (2026-08-12)
    #   빌더는 읽은 구간의 파티션을 **조건 없이 다시 쓴다.** 월 단위면 하루를
    #   덧붙이려고 그 달 전체를 다시 쓰고, 그때마다 파티션 해시가 바뀐다.
    #   매일 쌓이는 데이터셋에서는 세 가지가 같이 나빠진다:
    #     ① 비용이 날마다 커진다(하루 늘리자고 한 달을 다시 쓴다)
    #     ② **재현성이 매일 깨진다** - 어제 실험이 가리키던 해시가 사라진다
    #     ③ 과거가 조용히 바뀐다(원천이 늦게 채워지면 지난 날이 다시 접힌다)
    #   ②가 특히 나쁘다. 사전등록이 가리키는 대상이 매일 달라지면 사전등록의
    #   의미가 없어진다.
    #
    #   Parquet 은 푸터(스키마·통계)가 끝에 있어 **덧붙이기가 안 된다.** 그래서
    #   "닫힌 단위는 다시 쓰지 않는다"가 유일하게 싼 해법이고, 단위를 하루로
    #   잡으면 덧붙임이 곧 새 파일이라 재작성이 0 이 된다. 원시 아카이브
    #   (`market-archive/<표>/<날짜>.parquet`)가 이미 같은 규칙을 쓴다.
    partition_grain: str = "month"

    def row_line(self, row: dict) -> str:
        """해시의 근거가 되는 한 줄. **열 순서가 계약이다.**"""
        return "|".join(
            self.canon.get(c, lambda v: "" if v is None else str(v))(row.get(c))
            for c in self.columns)

    def sort_key(self, row: dict):
        return tuple(str(row.get(c)) for c in self.key_columns)

    def partition_of(self, row: dict) -> str:
        d = row[self.partition_column]
        d = d.date() if isinstance(d, datetime) else d
        return f"{d:%Y-%m-%d}" if self.partition_grain == "day" else f"{d:%Y-%m}"

    @property
    def open_partition(self) -> str:
        """지금 시각 기준으로 **아직 안 닫힌** 파티션 키. 이것만 다시 쓴다.

        하루 단위면 오늘, 달 단위면 이번 달이다. 이보다 과거인 파티션이 달라졌다면
        그건 덧붙임이 아니라 **소급 변경**이므로 덮지 않고 보고한다.
        """
        now = datetime.now(KST)
        return (f"{now:%Y-%m-%d}" if self.partition_grain == "day"
                else f"{now:%Y-%m}")


def _d(v):
    return None if v in (None, "") else Decimal(v)


def _f(v):
    return None if v in (None, "") else float(v)


def _dt(v):
    return None if v in (None, "") else datetime.fromisoformat(v)


def _date(v):
    return None if v in (None, "") else date.fromisoformat(v)


# ── 마이크로구조 일별 피처 ────────────────────────────────────────────────
#   원시(호가·체결)는 압축 후에도 2.5GB/일이라 파티션으로 굳힐 수 없다. 굳히는
#   것은 **종목·일자 단위로 접은 값**이고, 이게 백테스트가 실제로 읽는 계층이다.
MICROSTRUCTURE_DAILY = DatasetSpec(
    name="krx-microstructure-daily",
    version="v1",
    db="market",
    fetch_sql="""
        select instrument_id::text,
               (event_time at time zone 'Asia/Seoul')::date as trade_date,
               spread_bps, depth_imbalance, order_flow_imbalance,
               trade_intensity, realized_volatility,
               quality_status, observed_at
          from market.microstructure_features
         where feature_set_version = %(fsv)s
           and event_time >= %(start)s and event_time < %(end)s
    """,
    columns=("instrument_id", "trade_date", "spread_bps", "depth_imbalance",
             "order_flow_imbalance", "trade_intensity", "realized_volatility",
             "quality_status", "observed_at"),
    key_columns=("instrument_id", "trade_date"),
    partition_column="trade_date",
    canon={
        "trade_date": lambda v: v.isoformat() if isinstance(v, date) else str(v),
        # 피처는 double precision 이다. **None 은 빈 문자열이지 0 이 아니다** -
        # 0 으로 굳히면 "호가가 없던 날" 이 "스프레드 0" 이 되어 유동성 순위가
        # 뒤집힌다. 그 규칙을 파일 형식에서도 지킨다.
        "spread_bps": canon_number,
        "depth_imbalance": canon_number,
        "order_flow_imbalance": canon_number,
        "trade_intensity": canon_number,
        "realized_volatility": canon_number,
        "observed_at": canon_time,
    },
    restore={
        "trade_date": _date,
        "spread_bps": _f, "depth_imbalance": _f, "order_flow_imbalance": _f,
        "trade_intensity": _f, "realized_volatility": _f,
        "observed_at": _dt,
    },
    source_versions={"microstructure_features": "ms-daily-v1"},
    point_in_time={
        "kind": "MIXED",
        "why": ("이관 구간(2026-05-18~08-11)은 원본에 관측 시각이 없어 "
                "observed_at 이 자리 채움이다(market.pit_provenance = NONE). "
                "실시간 수집분만 PIT 가 성립한다."),
    },
    # ▶ **하루 단위** (2026-08-12). 이 데이터셋은 매일 자란다.
    #   월 단위였을 때는 하루를 덧붙이려고 그 달을 통째로 다시 썼고, 그때마다
    #   파티션 해시가 바뀌어 어제 실험이 가리키던 데이터가 사라졌다.
    #   하루 단위면 덧붙임이 곧 새 파일이라 **재작성이 0** 이다.
    #   하루 2,500행이라 파일이 작고, 원시 아카이브
    #   (`market-archive/<표>/<날짜>.parquet`)가 이미 같은 규칙을 쓴다.
    partition_grain="day",
)

SPECS: dict[str, DatasetSpec] = {
    f"{MICROSTRUCTURE_DAILY.name}/{MICROSTRUCTURE_DAILY.version}": MICROSTRUCTURE_DAILY,
}


# ── 자체 점검 (DB 없음) ───────────────────────────────────────────────────
def _check_canon_matches_pit_dataset():
    """값 정규화가 **두 곳에서 같아야** 한다. 다르면 같은 값이 다른 해시를 갖는다."""
    from pit_dataset import canon_number as pit_canon  # noqa: PLC0415

    for v in (Decimal("70000.0000000000"), Decimal("70000"), 0, 1, -1,
              Decimal("0.00001"), Decimal("1E+3"), 12345678901234, 0.5):
        assert canon_number(v) == pit_canon(v), (v, canon_number(v), pit_canon(v))
    # None 처리만 다르다 - pit_dataset 은 호출부에서 걸러서 넘긴다
    assert canon_number(None) == ""
    print("  값 정규화 일치           OK")


def _check_none_is_not_zero():
    """**못 잰 것을 0 으로 굳히지 않는다.**

    호가가 없던 날의 스프레드를 0 으로 쓰면 "스프레드가 가장 좁은 종목" 을
    고를 때 거래가 없던 종목이 1등이 된다. 파일 형식에서도 그 구분이 살아야 한다.
    """
    s = MICROSTRUCTURE_DAILY
    row = {"instrument_id": "i-1", "trade_date": date(2026, 8, 11),
           "spread_bps": None, "depth_imbalance": 0.0,
           "order_flow_imbalance": None, "trade_intensity": 1.5,
           "realized_volatility": None, "quality_status": "WARN",
           "observed_at": datetime(2026, 8, 11, tzinfo=KST)}
    line = s.row_line(row)
    parts = line.split("|")
    assert parts[2] == "", f"결측 스프레드가 {parts[2]!r} 로 굳었다"
    assert parts[3] == "0", f"실제 0 이 사라졌다: {parts[3]!r}"
    assert parts[4] == "" and parts[6] == ""
    print("  결측 != 0 (파일에서도)   OK")


def _check_roundtrip_is_hash_stable():
    """**정규화와 복원이 짝인가.** 아니면 다시 읽을 때 해시가 어긋난다.

    빌드 직후엔 통과하고 재검증에서만 깨지는 종류라, 여기서 고정하지 않으면
    나중에 "파일 변조/유실" 로 오진하게 된다.
    """
    s = MICROSTRUCTURE_DAILY
    row = {"instrument_id": "i-1", "trade_date": date(2026, 8, 11),
           "spread_bps": 12.3456, "depth_imbalance": -0.25,
           "order_flow_imbalance": 0.0, "trade_intensity": 1234.5,
           "realized_volatility": None, "quality_status": "PASS",
           "observed_at": datetime(2026, 8, 11, 15, 30, tzinfo=KST)}
    line = s.row_line(row)
    # 파일에 쓴 문자열을 그대로 복원해 다시 정규화하면 같아야 한다
    cells = dict(zip(s.columns, line.split("|")))
    restored = {c: s.restore.get(c, lambda x: x)(cells[c]) for c in s.columns}
    assert s.row_line(restored) == line, (line, s.row_line(restored))
    print("  정규화/복원 짝 맞음      OK")


def _check_partition_and_sort_are_deterministic():
    """같은 내용이면 같은 파티션·같은 순서. 순서가 흔들리면 해시가 흔들린다."""
    import dataclasses

    s = MICROSTRUCTURE_DAILY
    r = {"instrument_id": "i-9", "trade_date": date(2026, 7, 3)}
    # ▶ 이 명세는 **하루 단위**다(매일 자라므로). datetime 이 와도 같은 키다.
    assert s.partition_of(r) == "2026-07-03"
    assert s.partition_of({**r, "trade_date": datetime(2026, 7, 3, 9, tzinfo=KST)})         == "2026-07-03"
    assert s.sort_key(r) == ("i-9", "2026-07-03")

    # ▶ 월 단위도 그대로 동작해야 한다 - 기존 데이터셋(krx-basket-daily)이
    #   그 입도이고, 바꾸면 파티션 키가 달라져 사실상 새 데이터셋이 된다.
    m = dataclasses.replace(s, partition_grain="month")
    assert m.partition_of(r) == "2026-07"

    # ▶ 열린 파티션은 입도를 따른다. 이 값보다 과거가 달라지면 **소급 변경**이라
    #   빌더가 덮지 않고 보고한다.
    assert len(s.open_partition) == 10 and s.open_partition.count("-") == 2
    assert len(m.open_partition) == 7
    print("  파티션/정렬 결정론       OK")


def _check_spec_columns_match_sql():
    """SQL 이 뽑는 열과 `columns` 가 어긋나면 조용히 엉뚱한 값이 굳는다."""
    s = MICROSTRUCTURE_DAILY
    body = s.fetch_sql.split("from")[0]
    for col in ("spread_bps", "depth_imbalance", "order_flow_imbalance",
                "trade_intensity", "realized_volatility", "quality_status",
                "observed_at"):
        assert col in body, f"SQL 이 {col} 을 안 뽑는다"
    assert "trade_date" in body and "instrument_id" in body
    assert len(s.columns) == len(set(s.columns)), "열 이름이 겹친다"
    assert set(s.key_columns) <= set(s.columns)
    assert s.partition_column in s.columns
    print("  SQL/열 명세 일치         OK")


def _selfcheck() -> int:
    print(f"{SPEC_VERSION} 자체 점검 (DB 없음)")
    _check_canon_matches_pit_dataset()
    _check_none_is_not_zero()
    _check_roundtrip_is_hash_stable()
    _check_partition_and_sort_are_deterministic()
    _check_spec_columns_match_sql()
    print("데이터셋 명세 5개 영역 통과.")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(_selfcheck())
