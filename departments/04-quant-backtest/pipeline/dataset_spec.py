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

import re
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
    # ▶ **거래대금 단위를 명세가 선언한다** (2026-08-14)
    #   실행면은 `1 단위 = 1,000,000원`(KRW_MILLION)을 가정하고, 로더 경계에서
    #   매니페스트 선언과 대조한다(`assert_declared_units`). 그런데 spec 경로로
    #   만든 매니페스트에는 그 선언을 심는 자리가 없어 매번 "선언이 없다" 로
    #   지나갔다 - 대조가 있는데 대조할 것이 없는 상태다.
    #   거래대금 열이 없는 데이터셋은 None 그대로 둔다(없는 열은 안 묻는다).
    notional_unit: str | None = None

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

# ── 마이크로구조 일별 피처 v2 - 거래대금·체결수량 추가 ──────────────────────
#
# ▶ 왜 (2026-08-14, 재일님 지적 "거래대금 있었던거 같은데")
#   `market.market_ticks` 는 price·quantity·cumulative_value 를 다 갖고 있다
#   (실측: 하루 체결 1,335만건 · 2,489종목 · 거래대금 41.96조). 그런데 집계는
#   `sum(quantity)` 를 **계산해 놓고 출력에 안 실었고** 거래대금은 아예 만들지
#   않았다. 그래서 미시구조 표본에서는 유동성 필터가 쓸 값이 없었고, "거래대금
#   급증" 같은 신호를 표현할 칸도 없었다.
#
# ▶ **v1 을 고치지 않고 v2 를 만든다.** v1 로 돈 실험(`332fdec9`)의 재현이
#   살아야 한다 - 같은 이름·같은 버전인데 내용이 달라지면 그 실험의 input_hash
#   가 가리키던 데이터가 사라진다. 원천 쪽도 `ms-daily-v2` 로 따로 적재한다.
#
# ▶ 단위는 **백만원**(일봉 `notional` 과 같은 눈금). 매니페스트가
#   `notional_unit='KRW_MILLION'` 을 선언하고 로더가 대조한다 - 원 단위로 담으면
#   유동성 필터가 1e6 배 어긋난 채 돌아 유니버스가 0 이 된다(2026-08-14 실측).
MICROSTRUCTURE_DAILY_V2 = DatasetSpec(
    name="krx-microstructure-daily",
    version="v2",
    db="market",
    fetch_sql="""
        select instrument_id::text,
               (event_time at time zone 'Asia/Seoul')::date as trade_date,
               spread_bps, depth_imbalance, order_flow_imbalance,
               trade_intensity, realized_volatility,
               traded_value, traded_volume,
               quality_status, observed_at
          from market.microstructure_features
         where feature_set_version = %(fsv)s
           and event_time >= %(start)s and event_time < %(end)s
    """,
    columns=("instrument_id", "trade_date", "spread_bps", "depth_imbalance",
             "order_flow_imbalance", "trade_intensity", "realized_volatility",
             "traded_value", "traded_volume", "quality_status", "observed_at"),
    key_columns=("instrument_id", "trade_date"),
    partition_column="trade_date",
    canon={
        "trade_date": lambda v: v.isoformat() if isinstance(v, date) else str(v),
        "spread_bps": canon_number,
        "depth_imbalance": canon_number,
        "order_flow_imbalance": canon_number,
        "trade_intensity": canon_number,
        "realized_volatility": canon_number,
        "traded_value": canon_number,
        "traded_volume": canon_number,
        "observed_at": canon_time,
    },
    restore={
        "trade_date": _date,
        "spread_bps": _f, "depth_imbalance": _f, "order_flow_imbalance": _f,
        "trade_intensity": _f, "realized_volatility": _f,
        "traded_value": _f, "traded_volume": _f,
        "observed_at": _dt,
    },
    source_versions={"microstructure_features": "ms-daily-v2"},
    point_in_time={
        "kind": "MIXED",
        "why": ("이관 구간(2026-05-18~08-11)은 원본에 관측 시각이 없어 "
                "observed_at 이 자리 채움이다(market.pit_provenance = NONE). "
                "실시간 수집분만 PIT 가 성립한다."),
    },
    partition_grain="day",
    # 거래대금은 **백만원**으로 접는다 - 일봉 `notional` 과 같은 눈금이라야
    # 같은 유동성 문턱을 쓸 수 있다. 빌더가 이 값을 매니페스트에 심고
    # 로더가 실행면 가정과 대조한다.
    notional_unit="KRW_MILLION",
)

# ── 마이크로구조 일별 피처 v3 - 일중 구간 피처 추가 ────────────────────────
#
# ▶ 왜 (2026-08-14, 재일님 "호가 체결만 있으면 가공하면 다 만들 수 있지 않음?")
#   맞다. 원천은 이미 다 있었다(체결 11.29억건, 호가 10단계) - 문제는 **접는
#   방식**이었다. v1/v2 의 OFI 는 `sum(side*qty)/sum(qty)` 로 하루 전체를 하나로
#   평균한다. 오전에 팔고 오후에 산 종목은 중립으로 찍혀 정보가 상쇄된다.
#
#   2026-08-13 하루로 재 봤다(체결 30건 이상 2,420종목):
#     마감 30분 OFI 표준편차 0.4424  vs  하루 전체 0.2538   (1.74배)
#     둘의 상관 0.3713  <- 서로 다른 것을 잰다. 새 정보가 있다.
#   일별 OFI 첫 실험이 IC +0.012(t 0.79)로 죽은 것이 이 압축 때문이다.
#
# ▶ **실행면은 한 줄도 안 바뀐다.** 여전히 (종목, 일자) 한 행이라 `Market.micro`
#   도 walk-forward 도 체결 규약(t-1 시그널 / t 시가)도 그대로다. 일중에
#   *거래*하는 것은 엔진을 갈아야 하는 별개 작업이고 여기 포함되지 않는다.
MICROSTRUCTURE_DAILY_V3 = DatasetSpec(
    name="krx-microstructure-daily",
    version="v3",
    db="market",
    fetch_sql="""
        select instrument_id::text,
               (event_time at time zone 'Asia/Seoul')::date as trade_date,
               spread_bps, depth_imbalance, order_flow_imbalance,
               trade_intensity, realized_volatility,
               traded_value, traded_volume,
               ofi_close, ofi_open, ofi_intraday_std,
               close_vs_vwap, spread_close_ratio,
               quality_status, observed_at
          from market.microstructure_features
         where feature_set_version = %(fsv)s
           and event_time >= %(start)s and event_time < %(end)s
    """,
    columns=("instrument_id", "trade_date", "spread_bps", "depth_imbalance",
             "order_flow_imbalance", "trade_intensity", "realized_volatility",
             "traded_value", "traded_volume",
             "ofi_close", "ofi_open", "ofi_intraday_std",
             "close_vs_vwap", "spread_close_ratio",
             "quality_status", "observed_at"),
    key_columns=("instrument_id", "trade_date"),
    partition_column="trade_date",
    canon={
        "trade_date": lambda v: v.isoformat() if isinstance(v, date) else str(v),
        "spread_bps": canon_number,
        "depth_imbalance": canon_number,
        "order_flow_imbalance": canon_number,
        "trade_intensity": canon_number,
        "realized_volatility": canon_number,
        "traded_value": canon_number,
        "traded_volume": canon_number,
        "ofi_close": canon_number,
        "ofi_open": canon_number,
        "ofi_intraday_std": canon_number,
        "close_vs_vwap": canon_number,
        "spread_close_ratio": canon_number,
        "observed_at": canon_time,
    },
    restore={
        "trade_date": _date,
        "spread_bps": _f, "depth_imbalance": _f, "order_flow_imbalance": _f,
        "trade_intensity": _f, "realized_volatility": _f,
        "traded_value": _f, "traded_volume": _f,
        "ofi_close": _f, "ofi_open": _f, "ofi_intraday_std": _f,
        "close_vs_vwap": _f, "spread_close_ratio": _f,
        "observed_at": _dt,
    },
    source_versions={"microstructure_features": "ms-daily-v3"},
    point_in_time={
        "kind": "MIXED",
        "why": ("이관 구간(2026-05-18~08-11)은 원본에 관측 시각이 없어 "
                "observed_at 이 자리 채움이다(market.pit_provenance = NONE). "
                "실시간 수집분만 PIT 가 성립한다. 일중 구간 피처는 그날 정규장이 "
                "끝난 뒤에야 확정되므로, 체결이 t+1 시가인 우리 규약과 시점이 "
                "맞는다 - 같은 날 종가로 거래하는 전략에는 쓸 수 없다."),
    },
    partition_grain="day",
    notional_unit="KRW_MILLION",
)

# ── 마이크로구조 일별 피처 v4 - 호가 공간축·체결크기축 추가 ────────────────
#
# v3 까지 `depth_imbalance` 는 내부 원천에서 1~10호가 합계, 외부 이관 원천에서
# 최우선호가(`bi`)였다. v4 는 legacy 열을 L1 로 통일하고 L1/L10/차이를 각각
# 보존한다. `size_weighted_ofi` 는 큰 체결에 한 번 더 무게를 주며, 모든 새 값은
# 기존 호가·체결 일별 스캔 안에서 만들어진다.
MICROSTRUCTURE_DAILY_V4 = DatasetSpec(
    name="krx-microstructure-daily",
    version="v4",
    db="market",
    fetch_sql="""
        select instrument_id::text,
               (event_time at time zone 'Asia/Seoul')::date as trade_date,
               spread_bps, depth_imbalance, order_flow_imbalance,
               trade_intensity, realized_volatility,
               traded_value, traded_volume,
               ofi_close, ofi_open, ofi_intraday_std,
               close_vs_vwap, spread_close_ratio,
               depth_imbalance_l1, depth_imbalance_l10,
               depth_imbalance_slope, size_weighted_ofi,
               quality_status, observed_at
          from market.microstructure_features
         where feature_set_version = %(fsv)s
           and event_time >= %(start)s and event_time < %(end)s
    """,
    columns=("instrument_id", "trade_date", "spread_bps", "depth_imbalance",
             "order_flow_imbalance", "trade_intensity", "realized_volatility",
             "traded_value", "traded_volume",
             "ofi_close", "ofi_open", "ofi_intraday_std",
             "close_vs_vwap", "spread_close_ratio",
             "depth_imbalance_l1", "depth_imbalance_l10",
             "depth_imbalance_slope", "size_weighted_ofi",
             "quality_status", "observed_at"),
    key_columns=("instrument_id", "trade_date"),
    partition_column="trade_date",
    canon={
        "trade_date": lambda v: v.isoformat() if isinstance(v, date) else str(v),
        **{name: canon_number for name in (
            "spread_bps", "depth_imbalance", "order_flow_imbalance",
            "trade_intensity", "realized_volatility", "traded_value",
            "traded_volume", "ofi_close", "ofi_open", "ofi_intraday_std",
            "close_vs_vwap", "spread_close_ratio", "depth_imbalance_l1",
            "depth_imbalance_l10", "depth_imbalance_slope", "size_weighted_ofi",
        )},
        "observed_at": canon_time,
    },
    restore={
        "trade_date": _date,
        **{name: _f for name in (
            "spread_bps", "depth_imbalance", "order_flow_imbalance",
            "trade_intensity", "realized_volatility", "traded_value",
            "traded_volume", "ofi_close", "ofi_open", "ofi_intraday_std",
            "close_vs_vwap", "spread_close_ratio", "depth_imbalance_l1",
            "depth_imbalance_l10", "depth_imbalance_slope", "size_weighted_ofi",
        )},
        "observed_at": _dt,
    },
    source_versions={"microstructure_features": "ms-daily-v4"},
    point_in_time=dict(MICROSTRUCTURE_DAILY_V3.point_in_time),
    partition_grain="day",
    notional_unit="KRW_MILLION",
)

SPECS: dict[str, DatasetSpec] = {
    f"{MICROSTRUCTURE_DAILY.name}/{MICROSTRUCTURE_DAILY.version}": MICROSTRUCTURE_DAILY,
    f"{MICROSTRUCTURE_DAILY_V2.name}/{MICROSTRUCTURE_DAILY_V2.version}":
        MICROSTRUCTURE_DAILY_V2,
    f"{MICROSTRUCTURE_DAILY_V3.name}/{MICROSTRUCTURE_DAILY_V3.version}":
        MICROSTRUCTURE_DAILY_V3,
    f"{MICROSTRUCTURE_DAILY_V4.name}/{MICROSTRUCTURE_DAILY_V4.version}":
        MICROSTRUCTURE_DAILY_V4,
}

# 버전 스탬프. 빌더의 `--stamp` 가 `v1` 을 `v1-20260812` 로 찍는다.
_STAMPED = re.compile(r"^(?P<base>.+?)-\d{8}$")


def spec_for(name: str, version: str) -> "DatasetSpec | None":
    """`name/version` -> 명세. **스탬프를 벗겨서도 찾는다.**

    ▶ 왜 (2026-08-12 실측)
      `--stamp` 로 스냅샷 버전을 찍자 `SPECS.get("…/v1-20260812")` 가 빗나갔다.
      명세를 못 찾은 로더는 **일봉 gzip 경로로 조용히 떨어져** Parquet 파일을
      gzip 으로 열었고 `BadGzipFile: Not a gzipped file (b'PA')` 로 죽었다
      (`PA` 는 Parquet 매직 `PAR1` 의 앞 두 글자다).

      스탬프는 같은 명세의 다른 스냅샷이지 다른 명세가 아니다. 내용 해시가
      버전 문자열을 안 쓰므로(`content_hash` 는 sort_key·row_line 만 본다)
      기준 버전 명세로 읽어도 해시는 그대로 맞는다.

      **조회 규칙은 등록부가 소유한다** - 부르는 쪽마다 각자 벗기면 한 곳을
      고쳐도 다른 곳이 계속 gzip 으로 떨어진다.
    """
    key = f"{name}/{version}"
    s = SPECS.get(key)
    if s is not None:
        return s
    m = _STAMPED.match(str(version or ""))
    return SPECS.get(f"{name}/{m.group('base')}") if m else None


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


def _check_stamped_version_still_finds_the_spec():
    """**스탬프 찍힌 버전도 같은 명세를 찾아야 한다.** (2026-08-12 사고 원문)

    `--stamp` 로 `v1` -> `v1-20260812` 를 찍자 조회가 빗나갔고, 로더는 명세를
    못 찾자 일봉 gzip 경로로 조용히 떨어져 Parquet 를 gzip 으로 열었다:
    `BadGzipFile: Not a gzipped file (b'PA')`. 실험이 그 자리에서 죽었다.
    """
    n = MICROSTRUCTURE_DAILY.name
    assert spec_for(n, "v1") is MICROSTRUCTURE_DAILY
    assert spec_for(n, "v1-20260812") is MICROSTRUCTURE_DAILY, "스탬프 조회 실패"
    # **아무 접미사나 벗기지 않는다** - 8자리 날짜만이다. 버전이 다르면 다른
    # 명세이고, 섞이면 v1 로 돈 실험이 v2 열을 읽거나 그 반대가 된다.
    assert spec_for(n, "v2") is MICROSTRUCTURE_DAILY_V2
    assert spec_for(n, "v2") is not MICROSTRUCTURE_DAILY
    assert spec_for(n, "v2-20260814") is MICROSTRUCTURE_DAILY_V2, "v2 스탬프 실패"
    assert spec_for(n, "v3") is MICROSTRUCTURE_DAILY_V3
    assert spec_for(n, "v4") is MICROSTRUCTURE_DAILY_V4
    assert spec_for(n, "v9") is None, "없는 버전을 아무 명세로나 읽었다"
    # **판본은 쌓인다. 옛 판본의 열은 안 늘어난다** - 늘면 그 재현이 깨진다.
    assert "traded_value" in MICROSTRUCTURE_DAILY_V2.columns
    assert "traded_value" not in MICROSTRUCTURE_DAILY.columns
    for f in ("ofi_close", "ofi_open", "ofi_intraday_std", "close_vs_vwap",
              "spread_close_ratio"):
        assert f in MICROSTRUCTURE_DAILY_V3.columns, f
        assert f not in MICROSTRUCTURE_DAILY_V2.columns, (f, "v2 가 오염됐다")
        assert f not in MICROSTRUCTURE_DAILY.columns, (f, "v1 이 오염됐다")
    # 각 판본은 자기 원천 판본만 읽는다 - 섞이면 열이 빈 채로 굳는다
    assert MICROSTRUCTURE_DAILY_V3.source_versions == {
        "microstructure_features": "ms-daily-v3"}, MICROSTRUCTURE_DAILY_V3.source_versions
    for f in ("depth_imbalance_l1", "depth_imbalance_l10",
              "depth_imbalance_slope", "size_weighted_ofi"):
        assert f in MICROSTRUCTURE_DAILY_V4.columns, f
        assert f not in MICROSTRUCTURE_DAILY_V3.columns, (f, "v3 가 오염됐다")
    assert MICROSTRUCTURE_DAILY_V4.source_versions == {
        "microstructure_features": "ms-daily-v4"}, MICROSTRUCTURE_DAILY_V4.source_versions
    assert spec_for(n, "v1-2026081") is None, "8자리가 아닌 것을 스탬프로 봤다"
    assert spec_for(n, "v1-abc") is None
    # 이름이 다르면 못 찾는다 - 스탬프를 벗겼다고 남의 명세로 읽으면 안 된다
    assert spec_for("krx-basket-daily", "v1-20260812") is None
    # 내용 해시가 버전 문자열을 안 써야 이 대체가 안전하다
    import inspect  # noqa: PLC0415

    from spec_dataset_builder import content_hash  # noqa: PLC0415
    src = inspect.getsource(content_hash)
    assert ".version" not in src, ("content_hash 가 버전을 쓴다 - 스탬프 조회가 "
                                   "해시 불일치를 만든다")
    print("  스탬프 버전도 명세 찾음  OK")


def _selfcheck() -> int:
    print(f"{SPEC_VERSION} 자체 점검 (DB 없음)")
    _check_canon_matches_pit_dataset()
    _check_none_is_not_zero()
    _check_roundtrip_is_hash_stable()
    _check_partition_and_sort_are_deterministic()
    _check_spec_columns_match_sql()
    _check_stamped_version_still_finds_the_spec()
    print("데이터셋 명세 6개 영역 통과.")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(_selfcheck())
