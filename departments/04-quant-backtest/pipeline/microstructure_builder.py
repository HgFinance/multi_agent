#!/usr/bin/env python3
"""호가·체결 -> 일별 마이크로구조 피처. **백테스트가 읽는 것은 이 계층이다.**

소유: 재일 (퀀트·백테스트본부 QNT)
근거: 재일님 지시 2026-08-12 "호가체결 부터 하고 나머지 실행"
계약: market.microstructure_features (timescaledb 마이그레이션에 이미 있다)

▶ 왜 원시를 그대로 안 굳히나
  market_quotes 181GB / market_ticks 41GB 다(압축 후). 백테스트는 데이터셋을
  파티션 파일로 읽는데, 원시를 그대로 내보내면 한 번 실행에 수십 GB 를 읽어야
  하고 디스크도 다시 찬다. 그리고 **전략이 실제로 쓰는 것은 원시가 아니다** -
  스프레드·호가불균형·주문흐름불균형 같은 종목·일자 단위 값이다.

  그래서 원시는 원천으로 두고 여기서 일별로 접는다. 60거래일 × 2,600종목 =
  15만 행 수준이라 데이터셋으로 굳히기에 알맞다.

▶ 계산은 전부 SQL 로 한다 (결정론)
  같은 구간을 두 번 돌리면 같은 값이 나와야 재현이 성립한다. 파이썬으로 끌어와
  집계하면 부동소수 누적 순서가 실행마다 달라질 수 있고, 무엇보다 수억 행을
  네트워크로 옮기게 된다.

▶ 못 잰 것을 0 으로 채우지 않는다
  그날 호가가 없던 종목의 스프레드는 **0 이 아니라 없음**이다. 0 으로 채우면
  "스프레드가 가장 좁은 종목" 을 고를 때 거래가 없던 종목이 1등이 된다.
  SQL 이 NULL 로 두고, 적재도 NULL 로 넣는다.

▶ PIT
  observed_at 은 원천의 observed_at 최댓값을 쓴다. 이관 구간은 그것이
  event_time 자리 채움이므로(pit_provenance 가 'NONE' 으로 못박음) 이 피처도
  그 구간에서는 PIT 를 주장하지 않는다 - input_watermark 에 그대로 남긴다.

사용
  python pipeline/microstructure_builder.py                    # 자체 점검
  python pipeline/microstructure_builder.py --build            # 미적재 전 구간
  python pipeline/microstructure_builder.py --build --from 2026-08-01
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                       / "01-research" / "collectors"))

BUILDER_VERSION = "quant-microstructure-builder-v7"
SOURCE_CONTENT_HASH_CONTRACT = "pg-composite-row-xor0-sum1-sha256-v1"
# ▶ 판본 (2026-08-14). **옛 판본 행은 안 건드린다** - 그 판본으로 돈 실험의
#   재현이 살아야 한다.
#     v1 = 스프레드·잔량불균형·OFI·체결강도·실현변동성 (하루 하나로 평균)
#     v2 = + 거래대금·체결수량
#     v3 = + 일중 구간 피처(마감/개장 OFI · 구간 분산 · 종가/VWAP · 스프레드 수축)
#     v4 = + L1/L10 호가 공간축 · 깊이 기울기 · 체결크기 가중 OFI
#          + 외부 side 코드(1=매수, 5=매도)를 ofi_contrib(±volume)로 정상화
#     v5 = + L1/L10 절대 호가 수용력(가격×잔량, 백만원)
#   v3 의 근거: 하루 평균이 정보를 상쇄해 지운다. 2026-08-13 실측으로 마감 30분
#   OFI 표준편차가 하루 전체의 1.74배(0.4424 vs 0.2538)이고 둘의 상관은 0.3713 이다.
FEATURE_SET_VERSION = "ms-daily-v5"
KST = timezone(timedelta(hours=9))

# 정규장만 접는다. 시간외·프리마켓은 체결 규칙이 달라 같은 통계에 섞으면
# 스프레드·체결강도가 왜곡된다(가이드 8.2 와 같은 이유).
SESSION_START, SESSION_END = "09:00", "15:30"
EXTERNAL_CONTENT_WINDOW_CONTRACT = \
    "KRX_REGULAR_SESSION_HALF_OPEN_0900_1530_KST_V1"

# 그날 이 미만이면 통계가 아니라 잡음이다. 버리지 않고 quality_status 로 표시해
# 소비자가 거르게 한다 - 조용히 빼면 유니버스가 왜 줄었는지 알 수 없다.
MIN_TICKS_FOR_PASS = 30
MIN_QUOTES_FOR_PASS = 30

# Detect a collector that silently loses a large shard of the symbol universe.
# Per-row WARN grades alone make this look like ordinary illiquidity.
PARTIAL_GAP_MIN_ROWS = 50
PARTIAL_GAP_MIN_FRACTION = 0.10


# ── 일별 피처 SQL ──────────────────────────────────────────────────────────
#   호가와 체결을 각각 접고 종목 기준으로 붙인다. full outer join 인 이유:
#   호가만 있고 체결이 없는 종목(거래정지 직전 등)도 사실이므로 버리지 않는다.
_SQL_BUILD = """
with q as (
    select instrument_id,
           avg(case when mid_price > 0 then spread / mid_price * 10000 end) spread_bps,
           -- v3 까지는 이 한 컬럼이 내부에서는 L10, 외부에서는 L1 이었다.
           -- v4 는 의미를 분리하고 legacy `depth_imbalance` 를 L1 로 통일한다.
           avg(case when cardinality(bid_sizes) >= 1
                          and cardinality(ask_sizes) >= 1
                          and bid_sizes[1] + ask_sizes[1] > 0
                    then (bid_sizes[1] - ask_sizes[1])::float8
                         / (bid_sizes[1] + ask_sizes[1]) end) depth_imbalance_l1,
           avg(depth_imbalance) depth_imbalance_l10,
           avg(case when cardinality(bid_prices) >= 1
                          and cardinality(ask_prices) >= 1
                          and cardinality(bid_sizes) >= 1
                          and cardinality(ask_sizes) >= 1
                    then (bid_prices[1] * bid_sizes[1]
                          + ask_prices[1] * ask_sizes[1])::float8 / 1e6 end)
             book_depth_notional_l1,
           avg(case when cardinality(bid_prices) >= 1
                          and cardinality(ask_prices) >= 1
                          and cardinality(bid_sizes) >= 1
                          and cardinality(ask_sizes) >= 1
                    then (coalesce(bid_prices[1]*bid_sizes[1],0)
               + coalesce(bid_prices[2]*bid_sizes[2],0)
               + coalesce(bid_prices[3]*bid_sizes[3],0)
               + coalesce(bid_prices[4]*bid_sizes[4],0)
               + coalesce(bid_prices[5]*bid_sizes[5],0)
               + coalesce(bid_prices[6]*bid_sizes[6],0)
               + coalesce(bid_prices[7]*bid_sizes[7],0)
               + coalesce(bid_prices[8]*bid_sizes[8],0)
               + coalesce(bid_prices[9]*bid_sizes[9],0)
               + coalesce(bid_prices[10]*bid_sizes[10],0)
               + coalesce(ask_prices[1]*ask_sizes[1],0)
               + coalesce(ask_prices[2]*ask_sizes[2],0)
               + coalesce(ask_prices[3]*ask_sizes[3],0)
               + coalesce(ask_prices[4]*ask_sizes[4],0)
               + coalesce(ask_prices[5]*ask_sizes[5],0)
               + coalesce(ask_prices[6]*ask_sizes[6],0)
               + coalesce(ask_prices[7]*ask_sizes[7],0)
               + coalesce(ask_prices[8]*ask_sizes[8],0)
               + coalesce(ask_prices[9]*ask_sizes[9],0)
               + coalesce(ask_prices[10]*ask_sizes[10],0))::float8 / 1e6 end)
             book_depth_notional_l10,
           count(*) n_quotes,
           -- ▶ **유동성의 일중 변화** (2026-08-14). 하루 평균 스프레드 하나로는
           --   "개장에 벌어졌다 마감에 좁혀진 종목" 과 "종일 넓은 종목" 이
           --   같은 값으로 찍힌다.
           avg(case when mid_price > 0 and event_time >= %(t_close)s
                    then spread / mid_price * 10000 end) spread_close,
           avg(case when mid_price > 0 and event_time < %(t_open_end)s
                    then spread / mid_price * 10000 end) spread_open,
           max(observed_at) obs_q,
           max(event_time) evt_q
      from market.market_quotes
     where event_time >= %(lo)s and event_time < %(hi)s
     group by instrument_id
), t as (
    select instrument_id,
           -- 주문흐름불균형: 부호 있는 체결량 / 총 체결량. 분모 0 이면 NULL.
           case when sum(quantity) > 0
                then sum(side * quantity)::float8 / sum(quantity) end ofi,
           -- 체결강도: 분당 체결 건수. 정규장 390분 고정이 아니라 관측 폭으로
           -- 나눈다 - 반나절 장에서 두 배로 부풀지 않는다.
           case when max(event_time) > min(event_time)
                then count(*)::float8
                     / (extract(epoch from max(event_time) - min(event_time)) / 60.0)
                end trade_intensity,
           -- 실현변동성: 체결가 로그수익률 표준편차 × sqrt(건수). 건수가 적으면
           -- 표본표준편차가 정의되지 않아 NULL 이 된다(그게 맞다).
           stddev_samp(ln(nullif(price, 0)::float8)) * sqrt(count(*)) realized_vol,
           count(*) n_ticks,
           sum(quantity) vol,
           -- ▶ **거래대금. 단위는 백만원** (2026-08-14, 재일님 지적)
           --   원 단위로 담으면 유동성 필터가 1e6 배 어긋난 채 돈다 - 일봉
           --   `notional` 과 같은 눈금이어야 같은 문턱을 쓸 수 있다.
           --   `sum(quantity)` 는 예전부터 계산해 놓고 **출력에 안 실었다.**
           sum(price * quantity)::float8 / 1e6 traded_value,
           -- 체결량 OFI 와 달리 큰 체결에 한 번 더 무게를 준다. 별도 창/정렬 없이
           -- 같은 스캔에서 계산되어 10억 행 FDW 경로의 비용을 폭증시키지 않는다.
           case when sum(quantity * quantity) > 0
                then sum(side * quantity * quantity)::float8
                     / sum(quantity * quantity) end size_weighted_ofi,
           -- ▶ **일중 구간별 OFI** (2026-08-14 실측이 근거)
           --   하루 전체 평균은 오전에 팔고 오후에 산 종목을 중립으로 찍는다.
           --   2026-08-13 하루 대조: 마감 30분 OFI 표준편차 0.4424 vs 하루 전체
           --   0.2538(1.74배), 둘의 상관 0.3713 - 서로 다른 것을 잰다.
           sum(side*quantity) filter (where event_time >= %(t_close)s)::float8
             / nullif(sum(quantity) filter (where event_time >= %(t_close)s),0)
             ofi_close,
           sum(side*quantity) filter (where event_time < %(t_open_end)s)::float8
             / nullif(sum(quantity) filter (where event_time < %(t_open_end)s),0)
             ofi_open,
           -- 종가/VWAP - 1. 하루 내내 매수 압력이 있었으면 종가가 VWAP 위다.
           (array_agg(price order by event_time desc, sequence_no desc))[1]::float8
             / nullif(sum(price*quantity)::float8 / nullif(sum(quantity),0), 0)
             - 1 close_vs_vwap,
           -- ▶ **4구간 OFI 의 표본표준편차. 같은 스캔에서 낸다** (2026-08-14)
           --   처음엔 별도 CTE 로 `market_ticks` 를 한 번 더 group by 했는데,
           --   FDW 경로에서는 그것이 원천을 **두 번 끌어오는 것**이라 하루
           --   1,900만 행이 두 배가 됐고 빌더가 죽었다. `filter` 로 네 값을
           --   같은 스캔에서 내고 `unnest` 로 표준편차를 잰다 - `stddev_samp`
           --   은 NULL 을 무시하므로 결과가 이전과 **정확히 같다**.
           (select stddev_samp(v) from unnest(array[
              sum(side*quantity) filter (where event_time < %(t_open_end)s)::float8
                / nullif(sum(quantity) filter (where event_time < %(t_open_end)s),0),
              sum(side*quantity) filter (where event_time >= %(t_open_end)s
                                           and event_time < %(t_noon)s)::float8
                / nullif(sum(quantity) filter (where event_time >= %(t_open_end)s
                                                 and event_time < %(t_noon)s),0),
              sum(side*quantity) filter (where event_time >= %(t_noon)s
                                           and event_time < %(t_close)s)::float8
                / nullif(sum(quantity) filter (where event_time >= %(t_noon)s
                                                 and event_time < %(t_close)s),0),
              sum(side*quantity) filter (where event_time >= %(t_close)s)::float8
                / nullif(sum(quantity) filter (where event_time >= %(t_close)s),0)
            ]) v) ofi_intraday_std,
           max(observed_at) obs_t,
           max(event_time) evt_t
      from market.market_ticks
     where event_time >= %(lo)s and event_time < %(hi)s
       and price > 0
     group by instrument_id
)
select coalesce(q.instrument_id, t.instrument_id) instrument_id,
       q.spread_bps, q.depth_imbalance_l1, t.ofi, t.trade_intensity, t.realized_vol,
       coalesce(t.n_ticks, 0) n_ticks, coalesce(q.n_quotes, 0) n_quotes,
       t.traded_value, t.vol traded_volume,
       t.ofi_close, t.ofi_open, t.ofi_intraday_std, t.close_vs_vwap,
       -- 개장 스프레드가 0 이거나 없으면 비율을 만들지 않는다(0 으로 안 채운다)
       case when q.spread_open > 0 then q.spread_close / q.spread_open end
         spread_close_ratio,
       q.depth_imbalance_l1, q.depth_imbalance_l10,
       q.depth_imbalance_l1 - q.depth_imbalance_l10 depth_imbalance_slope,
       t.size_weighted_ofi,
       q.book_depth_notional_l1, q.book_depth_notional_l10,
       -- ▶ 관측시각의 논리적 하한 (2026-08-13 실측): 실시간 수집분에서 원천의
       --   event_time 이 observed_at 보다 ~2초 앞서는 시계 스큐가 있었고,
       --   그대로 접으면 watermark > observed_at 이 되어 적재 제약
       --   (input_watermark <= observed_at)이 매 주기 터졌다. 관측은 입력의
       --   최신 사건보다 앞설 수 없다 - 네 시각의 최댓값이 관측시각이다.
       --   (워터마크를 깎는 반대 방향은 입력 신선도를 지어내는 것이라 금지.
       --   원천 시계 스큐 자체는 수집기 몫의 결함으로 따로 남긴다)
       greatest(coalesce(q.obs_q, %(lo)s), coalesce(t.obs_t, %(lo)s),
                coalesce(q.evt_q, %(lo)s), coalesce(t.evt_t, %(lo)s)) observed_at,
       greatest(coalesce(q.evt_q, %(lo)s), coalesce(t.evt_t, %(lo)s)) watermark
  from q full outer join t on t.instrument_id = q.instrument_id
"""

# ── 외부 원천(Trading_bot)에서 바로 접기 ──────────────────────────────────
#
# ▶ 왜 원시를 안 옮기나 (재일님 지적 2026-08-12 "Parquet 이거 쓰면 안되나?")
#   우리 원장의 호가는 29일 중 8일만 온전했다. 나머지 21일(약 3.9억 행)을
#   이관하려니 대상 청크가 이미 압축돼 있어 33~44시간이 걸렸다.
#
#   그런데 **옮길 이유가 없다.** 저쪽 DB 에는 74거래일이 온전히 있고, 우리가
#   필요한 것은 종목·일자 단위 피처(하루 2,500행)다. 집계를 저쪽에서 돌리고
#   결과만 받으면 3.9억 행이 15만 행이 된다. 원시는 저쪽에 남고, 우리는
#   백테스트가 실제로 읽는 계층만 갖는다.
#
# ▶ 저쪽 스키마는 이미 파생값을 갖고 있다
#   quotes.spread(= ask1-bid1), quotes.bi(호가불균형),
#   ticks.ofi_contrib(매수 +volume / 매도 -volume).
#   같은 정의를 두 번 구현하지 않는다 - 우리 쪽 SQL 과 대응이 어긋나면
#   같은 종목의 같은 날 값이 경로마다 달라진다.
_SQL_BUILD_EXTERNAL = """
with q as (
    select symbol,
           avg(case when (ask1 + bid1) > 0
                    then spread::float8 / ((ask1 + bid1) / 2.0) * 10000 end) spread_bps,
           avg(bi::float8) depth_imbalance_l1,
           avg(case when
                 (bid_vol1+bid_vol2+bid_vol3+bid_vol4+bid_vol5+bid_vol6+bid_vol7+
                  bid_vol8+bid_vol9+bid_vol10+ask_vol1+ask_vol2+ask_vol3+ask_vol4+
                  ask_vol5+ask_vol6+ask_vol7+ask_vol8+ask_vol9+ask_vol10) > 0
               then
                 (bid_vol1+bid_vol2+bid_vol3+bid_vol4+bid_vol5+bid_vol6+bid_vol7+
                  bid_vol8+bid_vol9+bid_vol10-ask_vol1-ask_vol2-ask_vol3-ask_vol4-
                  ask_vol5-ask_vol6-ask_vol7-ask_vol8-ask_vol9-ask_vol10)::float8 /
                 (bid_vol1+bid_vol2+bid_vol3+bid_vol4+bid_vol5+bid_vol6+bid_vol7+
                  bid_vol8+bid_vol9+bid_vol10+ask_vol1+ask_vol2+ask_vol3+ask_vol4+
                  ask_vol5+ask_vol6+ask_vol7+ask_vol8+ask_vol9+ask_vol10)
               end) depth_imbalance_l10,
           avg((bid1::numeric * bid_vol1 + ask1::numeric * ask_vol1)::float8
               / 1e6) book_depth_notional_l1,
           avg(case when bid1 is not null and ask1 is not null
                    then (coalesce(bid1::numeric*bid_vol1,0)
               + coalesce(bid2::numeric*bid_vol2,0)
               + coalesce(bid3::numeric*bid_vol3,0)
               + coalesce(bid4::numeric*bid_vol4,0)
               + coalesce(bid5::numeric*bid_vol5,0)
               + coalesce(bid6::numeric*bid_vol6,0)
               + coalesce(bid7::numeric*bid_vol7,0)
               + coalesce(bid8::numeric*bid_vol8,0)
               + coalesce(bid9::numeric*bid_vol9,0)
               + coalesce(bid10::numeric*bid_vol10,0)
               + coalesce(ask1::numeric*ask_vol1,0)
               + coalesce(ask2::numeric*ask_vol2,0)
               + coalesce(ask3::numeric*ask_vol3,0)
               + coalesce(ask4::numeric*ask_vol4,0)
               + coalesce(ask5::numeric*ask_vol5,0)
               + coalesce(ask6::numeric*ask_vol6,0)
               + coalesce(ask7::numeric*ask_vol7,0)
               + coalesce(ask8::numeric*ask_vol8,0)
               + coalesce(ask9::numeric*ask_vol9,0)
               + coalesce(ask10::numeric*ask_vol10,0))::float8 / 1e6 end)
             book_depth_notional_l10,
           count(*) n_quotes,
           -- Hash the complete source composite directly.  JSON/text
           -- serialization made one day 216.9s versus a 36.5s baseline;
           -- PostgreSQL's typed record hash preserves the full-row identity
           -- at ~5s extra/day. XOR plus an independent additive seed keeps
           -- duplicate-row multiplicity from cancelling silently.
           bit_xor(hash_record_extended(quotes, 0))::text
             quote_content_xor_0,
           sum(hash_record_extended(quotes, 1)::numeric)::text
             quote_content_sum_1,
           avg(case when (ask1 + bid1) > 0 and ts >= %(t_close)s
                    then spread::float8 / ((ask1 + bid1) / 2.0) * 10000 end) spread_close,
           avg(case when (ask1 + bid1) > 0 and ts < %(t_open_end)s
                    then spread::float8 / ((ask1 + bid1) / 2.0) * 10000 end) spread_open
      from public.quotes quotes
     where ts >= %(lo)s and ts < %(hi)s
     group by symbol
), t as (
    select symbol,
           -- 외부 side 는 1=매수, 5=매도라 곱하면 [-1,1]을 벗어난다.
           -- 수집기가 이미 정규화한 ofi_contrib(±volume)를 사용한다.
           case when sum(volume) > 0
                then sum(ofi_contrib)::float8 / sum(volume) end ofi,
           case when max(ts) > min(ts)
                then count(*)::float8
                     / (extract(epoch from max(ts) - min(ts)) / 60.0) end trade_intensity,
           stddev_samp(ln(nullif(price, 0)::float8)) * sqrt(count(*)) realized_vol,
           count(*) n_ticks,
           bit_xor(hash_record_extended(ticks, 0))::text
             trade_content_xor_0,
           sum(hash_record_extended(ticks, 1)::numeric)::text
             trade_content_sum_1,
           -- 내부 경로와 **같은 정의·같은 단위**(백만원)여야 한다. 어긋나면
           -- 같은 종목의 같은 날 값이 출처마다 달라진다.
           sum(price::float8 * volume) / 1e6 traded_value,
           case when sum(volume::numeric * volume) > 0
                then sum(ofi_contrib::numeric * volume)::float8
                     / sum(volume::numeric * volume) end size_weighted_ofi,
           sum(volume) vol,
           sum(ofi_contrib) filter (where ts >= %(t_close)s)::float8
             / nullif(sum(volume) filter (where ts >= %(t_close)s),0) ofi_close,
           sum(ofi_contrib) filter (where ts < %(t_open_end)s)::float8
             / nullif(sum(volume) filter (where ts < %(t_open_end)s),0) ofi_open,
           (array_agg(price order by ts desc))[1]::float8
             / nullif(sum(price::float8*volume) / nullif(sum(volume),0), 0)
             - 1 close_vs_vwap,
           -- 내부 경로와 같은 정의. **같은 스캔에서** 낸다 - 별도 CTE 로
           -- 원천을 한 번 더 읽으면 FDW 로는 하루 1,900만 행이 두 배가 된다.
           (select stddev_samp(v) from unnest(array[
              sum(ofi_contrib) filter (where ts < %(t_open_end)s)::float8
                / nullif(sum(volume) filter (where ts < %(t_open_end)s),0),
              sum(ofi_contrib) filter (where ts >= %(t_open_end)s
                                         and ts < %(t_noon)s)::float8
                / nullif(sum(volume) filter (where ts >= %(t_open_end)s
                                               and ts < %(t_noon)s),0),
              sum(ofi_contrib) filter (where ts >= %(t_noon)s
                                         and ts < %(t_close)s)::float8
                / nullif(sum(volume) filter (where ts >= %(t_noon)s
                                               and ts < %(t_close)s),0),
              sum(ofi_contrib) filter (where ts >= %(t_close)s)::float8
                / nullif(sum(volume) filter (where ts >= %(t_close)s),0)
            ]) v) ofi_intraday_std
      from public.ticks ticks
     where ts >= %(lo)s and ts < %(hi)s and price > 0
     group by symbol
)
select coalesce(q.symbol, t.symbol) symbol,
       q.spread_bps, q.depth_imbalance_l1, t.ofi, t.trade_intensity, t.realized_vol,
       coalesce(t.n_ticks, 0), coalesce(q.n_quotes, 0),
       t.traded_value, t.vol traded_volume,
       t.ofi_close, t.ofi_open, t.ofi_intraday_std, t.close_vs_vwap,
       case when q.spread_open > 0 then q.spread_close / q.spread_open end
          spread_close_ratio,
       q.depth_imbalance_l1, q.depth_imbalance_l10,
       q.depth_imbalance_l1 - q.depth_imbalance_l10 depth_imbalance_slope,
       t.size_weighted_ofi,
       q.book_depth_notional_l1, q.book_depth_notional_l10,
       q.quote_content_xor_0, q.quote_content_sum_1,
       t.trade_content_xor_0, t.trade_content_sum_1
  from q full outer join t on t.symbol = q.symbol
"""

# ── 같은 집계를 **FDW 로** - 별도 커넥션도 자격증명도 없이 ────────────────
#
# ▶ 왜 (2026-08-14)
#   `--external-dsn` 경로는 저쪽 DB 에 직접 붙어야 해서 자격증명을 손으로
#   날라야 했다. 그런데 우리 DB 에는 이미 `ext_src` 스키마가 `trading_src`
#   foreign server 로 걸려 있다(실측: ext_src.ticks 가 2026-05-17~08-14,
#   11.29억 건). 같은 커넥션에서 읽을 수 있는데 자격증명을 다시 다룰 이유가 없다.
#
#   SQL 은 위 `_SQL_BUILD_EXTERNAL` 과 **글자 하나까지 같은 정의**여야 한다 -
#   스키마 이름만 다르다. 자체점검이 둘을 대조한다(경로마다 값이 달라지면
#   같은 종목의 같은 날이 출처에 따라 다른 값을 갖는다).
_SQL_BUILD_FDW = _SQL_BUILD_EXTERNAL.replace("public.quotes", "ext_src.quotes") \
                                    .replace("public.ticks", "ext_src.ticks")

_SQL_INSERT = """
insert into market.microstructure_features
  (event_time, observed_at, instrument_id, market, feature_set_version,
   realized_volatility, spread_bps, depth_imbalance, order_flow_imbalance,
   trade_intensity, traded_value, traded_volume,
   ofi_close, ofi_open, ofi_intraday_std, close_vs_vwap, spread_close_ratio,
   depth_imbalance_l1, depth_imbalance_l10, depth_imbalance_slope,
   size_weighted_ofi,
   book_depth_notional_l1, book_depth_notional_l10,
   values, quality_status, input_watermark, input_hash)
values %s
on conflict do nothing
"""

# ▶ **한 날은 한 출처만 쓴다** (2026-08-12 실측 사고)
#   `on conflict do nothing` 은 **먼저 넣은 쪽이 이긴다.** 품질이 아니라 실행
#   순서가 승자를 정한다. 그래서 이런 일이 생겼다:
#
#     로컬 빌드가 먼저 돌았다 → 그때 우리 호가는 21일이 구멍이라 스프레드가
#     통째로 NULL 이었다 → 나중에 외부 빌드를 돌렸는데 이미 있는 종목은 안
#     덮였다 → **그 21일은 종목 2,490개가 스프레드 없고 35~39개만 있다.**
#
#   한 날 안에서 종목마다 품질이 다르면 횡단면 전략이 "스프레드가 있는 39개"
#   만 고른다. **표본이 조용히 바뀌는데 아무도 모른다** - 이게 최악이다.
#
#   그래서 적재 전에 그날의 기존 출처를 보고, 다르면 **거부하거나 명시적으로
#   교체**한다. 조용히 섞이는 경로를 없앤다.
_SQL_DAY_ORIGINS = """
select distinct coalesce(values->>'origin', 'local')
  from market.microstructure_features
 where feature_set_version = %s
   and event_time >= %s and event_time < %s
"""

# 판정을 사람이 읽는 문장으로. **조용히 건너뛰지 않는다** - 왜 0행인지
# 로그에 남아야 다음 사람이 원인을 찾는다.
_GATE_NOTE = {
    "skip": "같은 출처가 이미 있다 - 건너뜀",
    "blocked": "**다른 출처가 이미 있다 - 섞지 않는다.** 교체하려면 --replace",
}

_SQL_DELETE_DAY = """
delete from market.microstructure_features
 where feature_set_version = %s
   and event_time >= %s and event_time < %s
"""


def day_origin_guard(conn, day: date, origin: str, *, replace: bool = False) -> str:
    """그날 이미 있는 출처와 지금 넣으려는 출처를 대조한다.

    반환: 'insert'(넣어도 됨) | 'skip'(같은 출처가 이미 있음) | 'blocked'
    `replace=True` 면 출처가 같든 다르든 **그날을 통째로 지우고** 다시 넣는다.
    원천 정정이나 내용-지문 계약 변경도 기존 날짜를 재구축할 수 있어야 한다.
    부분 덮어쓰기는 하지 않는다. 섞인 상태를 만드는 것이 문제였으므로,
    교체는 날 단위로만 허용한다.
    """
    bucket = datetime.combine(day, datetime.min.time(), tzinfo=KST)
    nxt = bucket + timedelta(days=1)
    with conn.cursor() as cur:
        # Serialize every writer for one feature-version/day. DELETE+INSERT is
        # atomic only when overlapping workers cannot both pass this guard.
        cur.execute(
            "select pg_advisory_xact_lock(hashtextextended(%s, 0))",
            (f"{FEATURE_SET_VERSION}|{day.isoformat()}",))
        cur.execute(_SQL_DAY_ORIGINS, (FEATURE_SET_VERSION, bucket, nxt))
        have = {r[0] for r in cur.fetchall()}
    if not have:
        return "insert"
    if not replace:
        return "skip" if have == {origin} else "blocked"
    with conn.cursor() as cur:
        cur.execute(_SQL_DELETE_DAY, (FEATURE_SET_VERSION, bucket, nxt))
    # Caller inserts the rebuilt rows and commits both operations together.
    # Committing here creates a crash window in which an entire trading day is
    # absent after DELETE but before INSERT.
    return "insert"

# 이미 접은 날은 다시 접지 않는다. 같은 날을 두 번 넣으면 데이터셋 해시가
# 실행마다 달라져 재현이 깨진다.
_SQL_DONE_DAYS = """
select distinct (event_time at time zone 'Asia/Seoul')::date
  from market.microstructure_features
 where feature_set_version = %s
"""

_SQL_SOURCE_DAYS = """
select distinct (range_start at time zone 'Asia/Seoul')::date
  from timescaledb_information.chunks
 where hypertable_name = 'market_ticks'
 order by 1
"""


def quality_of(n_ticks: int, n_quotes: int) -> str:
    """행 수로 등급을 매긴다. **버리지 않고 표시한다.**

    조용히 빼면 소비자는 유니버스가 왜 줄었는지 알 수 없다. WARN 을 남기면
    "거래가 거의 없던 종목" 이라는 사실 자체가 재료가 된다.
    """
    if n_ticks >= MIN_TICKS_FOR_PASS and n_quotes >= MIN_QUOTES_FOR_PASS:
        return "PASS"
    if n_ticks == 0 and n_quotes == 0:
        return "FAIL"          # 있을 수 없는 행이다 - 그래도 만들면 표시한다
    return "WARN"


def assert_v4_bounds(**features) -> None:
    """정의상 범위를 벗어난 피처를 원장에 넣지 않는다.

    외부 side=1/5 를 ±1 로 오해한 과거 집계는 OFI=2.8 같은 값을 만들었지만
    예외도 경고도 없었다. v4 는 값의 의미를 적재 경계에서 강제한다.
    """
    for name, value in features.items():
        if value is None:
            continue
        low, high = (-2.0, 2.0) if name == "depth_imbalance_slope" else (-1.0, 1.0)
        number = float(value)
        if not (low - 1e-12 <= number <= high + 1e-12):
            raise ValueError(
                f"{FEATURE_SET_VERSION} {name}={number} outside [{low}, {high}]")


def assert_v5_capacity(**features) -> None:
    """Absolute displayed depth is missing or non-negative, never signed."""
    for name, value in features.items():
        if value is None:
            continue
        number = float(value)
        if number < 0:
            raise ValueError(f"{FEATURE_SET_VERSION} {name}={number} is negative")


def missing_sources(rows) -> list[str]:
    """그날 **원천 하나가 통째로 빈** 경우를 이름으로 돌려준다.

    ▶ 왜 (2026-08-14 실측)
      2026-08-10 에 `market.market_quotes` 가 **0행**이었다(체결은 1,348만건
      정상, 저쪽 원본에는 1,851만건 있었다). 그런데:
        · 종목마다 `quality_of` 가 WARN 을 찍고 그대로 적재됐다
        · `market.feed_gaps` 는 **0행** - 감지표가 있는데 채우는 쪽이 없었다
        · 그래서 스프레드를 읽는 신호는 그날 전 종목 미산출 = 조용히 거래 0
      WARN 은 "이 종목의 표본이 얇다" 는 뜻이지 "원천이 통째로 없다" 가 아니다.
      **한 종목의 문제와 하루 전체의 문제는 다른 사실**이라 다르게 말해야 한다.
    """
    if not rows:
        return []
    # 행 구조: (..., n_ticks, n_quotes, ...) - 인덱스 6,7 (내부/외부 경로 공통)
    tot_t = sum(int(r[6] or 0) for r in rows)
    tot_q = sum(int(r[7] or 0) for r in rows)
    out = []
    if tot_q == 0:
        out.append("quotes")
    if tot_t == 0:
        out.append("ticks")
    return out


def partial_source_gaps(rows) -> list[dict]:
    """Return material one-sided source loss hidden inside a live day."""
    total = len(rows)
    if not total:
        return []
    candidates = (
        ("quotes", sum(int(r[6] or 0) > 0 and int(r[7] or 0) == 0 for r in rows)),
        ("ticks", sum(int(r[7] or 0) > 0 and int(r[6] or 0) == 0 for r in rows)),
    )
    threshold = max(PARTIAL_GAP_MIN_ROWS,
                    int(total * PARTIAL_GAP_MIN_FRACTION + 0.999999))
    return [
        {"source": source, "affected_rows": affected,
         "total_rows": total, "affected_fraction": affected / total}
        for source, affected in candidates if affected >= threshold
    ]


def input_hash(day: date, n_ticks: int, n_quotes: int) -> str:
    """어느 입력에서 나온 값인지. 원천이 바뀌면 해시가 바뀐다."""
    blob = f"{FEATURE_SET_VERSION}|{day.isoformat()}|{n_ticks}|{n_quotes}"
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def external_content_fingerprints(
        day: date, symbol: str, n_ticks: int, n_quotes: int,
        quote_xor_0, quote_sum_1, trade_xor_0, trade_sum_1,
) -> tuple[str, str, str]:
    """Bind an external feature row to the raw quote/trade row multisets.

    Counts alone do not identify data: a vendor correction can change price,
    volume, timestamp, or L10 book values without changing either count.  SQL
    therefore folds the complete typed rows with a seeded 64-bit XOR and an
    independent seeded additive checksum.  Count and sum preserve multiplicity
    when identical rows cancel in XOR.  This function gives each side and their
    union a canonical SHA-256 identity suitable for the experiment lockbox.
    """
    counts = {"quotes": int(n_quotes), "ticks": int(n_ticks)}
    if any(value < 0 for value in counts.values()):
        raise ValueError(f"negative external source count: {counts}")

    def one(source: str, count: int, components) -> str:
        normalized = [None if value is None else str(value)
                      for value in components]
        if count > 0 and any(value is None for value in normalized):
            raise ValueError(
                f"{source} content digest missing for {symbol} {day}: "
                f"count={count}")
        body = {
            "contract": SOURCE_CONTENT_HASH_CONTRACT,
            "day": day.isoformat(),
            "symbol": str(symbol).strip(),
            "source": source,
            "row_count": count,
            "xor_seed_0": normalized[0],
            "sum_seed_1": normalized[1],
        }
        canonical = json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    quote_fp = one(
        "quotes", counts["quotes"],
        (quote_xor_0, quote_sum_1))
    trade_fp = one(
        "ticks", counts["ticks"],
        (trade_xor_0, trade_sum_1))
    combined = json.dumps({
        "contract": SOURCE_CONTENT_HASH_CONTRACT,
        "day": day.isoformat(),
        "symbol": str(symbol).strip(),
        "quote_content_fingerprint": quote_fp,
        "trade_content_fingerprint": trade_fp,
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return (
        quote_fp,
        trade_fp,
        hashlib.sha256(combined.encode("utf-8")).hexdigest(),
    )


def session_bounds(day: date) -> tuple[str, str]:
    """그날 정규장 구간(KST). 시간외를 섞지 않는다."""
    return (f"{day.isoformat()} {SESSION_START}+09",
            f"{day.isoformat()} {SESSION_END}+09")


def external_session_content_window(day: date) \
        -> tuple[datetime, datetime]:
    """Return the sole raw-content hash window used by builder and runner.

    This is a fixed half-open regular-session interval.  Prediction horizon,
    latency, purge gap, and whether a formula needs score calibration may limit
    sample construction, but they must never change which raw rows identify a
    session/instrument cell.
    """

    start = datetime.combine(day, time.fromisoformat(SESSION_START), KST)
    end = datetime.combine(day, time.fromisoformat(SESSION_END), KST)
    if not start < end:
        raise RuntimeError("external source content window must be non-empty")
    return start, end


# ▶ 일중 구간 경계(KST). 개장 09:00-09:30 · 오전 -12:00 · 오후 -14:50 · 마감 -15:30.
#   마감을 40분으로 잡은 것은 15:20 이후 동시호가에 체결이 몰리기 때문이다 -
#   30분으로 자르면 그 물량이 통째로 빠진다.
SEG_OPEN_END, SEG_NOON, SEG_CLOSE = "09:30", "12:00", "14:50"


def session_params(day: date) -> dict:
    """집계 SQL 이 받는 시각 파라미터 전부. **한 곳에서 만든다** - 두 경로가
    다른 경계를 쓰면 같은 종목의 같은 날이 출처에 따라 다른 값을 갖는다."""
    lo, hi = session_bounds(day)
    d = day.isoformat()
    return {"lo": lo, "hi": hi,
            "t_open_end": f"{d} {SEG_OPEN_END}+09",
            "t_noon":     f"{d} {SEG_NOON}+09",
            "t_close":    f"{d} {SEG_CLOSE}+09"}


def build_day(market_conn, day: date, *, dry_run: bool = False,
              replace: bool = False) -> dict:
    """하루를 접어 적재한다. 반환: 요약."""
    from psycopg2.extras import execute_values

    with market_conn.cursor() as cur:
        cur.execute(_SQL_BUILD, session_params(day))
        rows = cur.fetchall()
    if not rows:
        return {"day": day, "rows": 0, "note": "원천에 그날 행이 없다"}

    bucket = datetime.combine(day, datetime.min.time(), tzinfo=KST)
    # **원천 하나가 통째로 비었으면 행마다가 아니라 그 사실을 남긴다.**
    miss = missing_sources(rows)
    partial = partial_source_gaps(rows)
    miss_tag = f', "missing_source": "{"+".join(miss)}"' if miss else ""
    payload, grades = [], {"PASS": 0, "WARN": 0, "FAIL": 0}
    for (iid, spread, di, ofi, intensity, rvol,
         n_ticks, n_quotes, tvalue, tvolume,
         ofi_c, ofi_o, ofi_sd, cvwap, sp_ratio,
         depth_l1, depth_l10, depth_slope, size_ofi,
         book_depth_l1, book_depth_l10,
         observed_at, watermark) in rows:
        assert_v4_bounds(
            order_flow_imbalance=ofi, ofi_close=ofi_c, ofi_open=ofi_o,
            depth_imbalance_l1=depth_l1, depth_imbalance_l10=depth_l10,
            depth_imbalance_slope=depth_slope, size_weighted_ofi=size_ofi)
        assert_v5_capacity(book_depth_notional_l1=book_depth_l1,
                           book_depth_notional_l10=book_depth_l10)
        g = quality_of(int(n_ticks), int(n_quotes))
        grades[g] += 1
        payload.append((
            bucket, observed_at, iid, "KRX", FEATURE_SET_VERSION,
            rvol, spread, di, ofi, intensity, tvalue, tvolume,
            ofi_c, ofi_o, ofi_sd, cvwap, sp_ratio,
            depth_l1, depth_l10, depth_slope, size_ofi,
            book_depth_l1, book_depth_l10,
            # values 에 표본 수를 남긴다 - 어떤 행이 얇은지 나중에 판단할 수 있어야 한다
            f'{{"n_ticks": {int(n_ticks)}, "n_quotes": {int(n_quotes)}{miss_tag}}}',
            g, watermark, input_hash(day, int(n_ticks), int(n_quotes))))

    if dry_run:
        return {"day": day, "rows": len(payload), **grades,
                "missing": miss, "partial": partial, "dry_run": True}
    gate = day_origin_guard(market_conn, day, "local", replace=replace)
    if gate != "insert":
        return {"day": day, "rows": 0, **grades, "note": _GATE_NOTE[gate]}
    with market_conn.cursor() as cur:
        execute_values(cur, _SQL_INSERT, payload, page_size=2000)
    if miss:
        record_feed_gap(market_conn, day, miss, len(payload))
    if partial:
        record_partial_feed_gaps(market_conn, day, partial)
    market_conn.commit()
    return {"day": day, "rows": len(payload), **grades,
            "missing": miss, "partial": partial}


# ▶ **결손을 표에 남긴다** (2026-08-14). `market.feed_gaps` 는 이 저장소가
#   결손을 기록하라고 만들어 둔 표인데 **0행이었다** - 08-10 에 호가가 하루
#   통째로 빠졌는데 아무 기록이 없었다. 감지표가 있어도 채우는 쪽이 없으면
#   없는 것과 같다(같은 날 `VOID_NO_TRADE` 도 읽는 쪽이 없어 장식이었다).
_SQL_FEED_GAP = """
insert into market.feed_gaps
  (provider, stream_type, detected_at, gap_start, gap_end, severity, status,
   backfill_source, evidence)
values (%s, %s, now(), %s, %s, %s, 'OPEN', %s, %s::jsonb)
"""

_SQL_PARTIAL_FEED_GAP = """
insert into market.feed_gaps
  (provider, stream_type, detected_at, gap_start, gap_end, severity, status,
   backfill_source, evidence)
select %s, %s, now(), %s, %s, 'HIGH', 'OPEN', %s, %s::jsonb
where not exists (
  select 1 from market.feed_gaps
   where provider = %s and stream_type = %s
     and gap_start = %s and gap_end = %s and status = 'OPEN'
     and evidence->>'kind' = 'partial_universe_loss'
)
"""


def record_feed_gap(conn, day: date, missing: list[str], n_rows: int) -> None:
    """하루 전체가 빈 원천을 `market.feed_gaps` 에 남긴다. **실패해도 적재는 산다.**

    복구원(`backfill_source`)까지 적는다 - 실측으로 저쪽 원본에는 그날 호가가
    1,851만건 있었다. "없다" 가 아니라 "여기 있는데 우리가 못 받았다" 가 사실이다.
    """
    lo, hi = session_bounds(day)
    for src in missing:
        with conn.cursor() as cur:
            cur.execute("savepoint record_feed_gap")
            try:
                cur.execute(_SQL_FEED_GAP, (
                    "KRX-COLLECTOR", src, lo, hi, "CRITICAL",
                    f"ext_src.{src}",
                    f'{{"day": "{day.isoformat()}", "rows_built": {n_rows},'
                    f' "note": "정규장 전체가 0행 - 그날 이 원천에서 나오는 '
                    f'피처는 전 종목 미산출이다"}}'))
            except Exception as e:  # noqa: BLE001 - 기록 실패가 적재를 막지 않는다
                cur.execute("rollback to savepoint record_feed_gap")
                print(f"    ⚠ feed_gaps 기록 실패({src}): {type(e).__name__}: "
                      f"{str(e)[:90]}", flush=True)
            finally:
                cur.execute("release savepoint record_feed_gap")


def record_partial_feed_gaps(conn, day: date, gaps: list[dict]) -> None:
    """Persist large one-sided universe loss without blocking feature storage."""
    import json

    lo, hi = session_bounds(day)
    for gap in gaps:
        source = gap["source"]
        evidence = json.dumps({
            "kind": "partial_universe_loss",
            "day": day.isoformat(),
            "affected_rows": gap["affected_rows"],
            "total_rows": gap["total_rows"],
            "affected_fraction": round(gap["affected_fraction"], 6),
            "threshold_rows": PARTIAL_GAP_MIN_ROWS,
            "threshold_fraction": PARTIAL_GAP_MIN_FRACTION,
        })
        with conn.cursor() as cur:
            cur.execute("savepoint record_partial_feed_gap")
            try:
                cur.execute(_SQL_PARTIAL_FEED_GAP, (
                    "KRX-COLLECTOR", source, lo, hi, f"ext_src.{source}", evidence,
                    "KRX-COLLECTOR", source, lo, hi))
            except Exception as e:  # diagnostics must not destroy feature storage
                cur.execute("rollback to savepoint record_partial_feed_gap")
                print(f"    feed_gaps partial record failed ({source}): "
                      f"{type(e).__name__}: {str(e)[:90]}", flush=True)
            finally:
                cur.execute("release savepoint record_partial_feed_gap")


def build_day_external(market_conn, src_conn, day: date, *,
                       dry_run: bool = False,
                       replace: bool = False) -> dict:
    """저쪽 DB 에서 집계하고 **결과만** 우리 원장에 넣는다.

    종목 매핑은 우리 쪽 `market.symbol_map` 이 정본이다. 못 찾은 종목은
    **버리지 않고 센다** - 조용히 빠지면 유니버스가 왜 줄었는지 알 수 없다.

    `src_conn` 이 None 이면 **FDW 경로**로 우리 커넥션에서 `ext_src.*` 를 읽는다
    (자격증명을 따로 다루지 않는다). 집계 정의는 두 경로가 같다.
    """
    from psycopg2.extras import execute_values

    read_conn = src_conn if src_conn is not None else market_conn
    sql = _SQL_BUILD_EXTERNAL if src_conn is not None else _SQL_BUILD_FDW
    with read_conn.cursor() as cur:
        cur.execute(sql, session_params(day))
        rows = cur.fetchall()
    if not rows:
        if dry_run or not replace:
            return {"day": day, "rows": 0,
                    "note": "external source has no rows for this session"}
        # Explicit reconciliation must not leave stale target rows after the
        # source deleted an entire session. Remove the materialization under the
        # per-day transaction lock and leave an auditable gap tombstone.
        gate = day_origin_guard(market_conn, day, "external", replace=True)
        if gate != "insert":
            market_conn.rollback()
            return {"day": day, "rows": 0, "note": _GATE_NOTE[gate]}
        record_feed_gap(market_conn, day, ["quotes", "ticks"], 0)
        market_conn.commit()
        return {"day": day, "rows": 0, "missing": ["quotes", "ticks"],
                "tombstone": True,
                "note": "source-empty reconciliation removed stale features"}

    with market_conn.cursor() as cur:
        cur.execute("select symbol, instrument_id from market.symbol_map")
        iid_of = dict(cur.fetchall())

    bucket = datetime.combine(day, datetime.min.time(), tzinfo=KST)
    miss = missing_sources(rows)
    partial = partial_source_gaps(rows)
    miss_tag = f', "missing_source": "{"+".join(miss)}"' if miss else ""
    payload, grades, unmapped = [], {"PASS": 0, "WARN": 0, "FAIL": 0}, 0
    for (sym, spread, di, ofi, intensity, rvol, n_ticks, n_quotes,
         tvalue, tvolume, ofi_c, ofi_o, ofi_sd, cvwap, sp_ratio,
         depth_l1, depth_l10, depth_slope, size_ofi,
         book_depth_l1, book_depth_l10,
         quote_xor_0, quote_sum_1,
         trade_xor_0, trade_sum_1) in rows:
        assert_v4_bounds(
            order_flow_imbalance=ofi, ofi_close=ofi_c, ofi_open=ofi_o,
            depth_imbalance_l1=depth_l1, depth_imbalance_l10=depth_l10,
            depth_imbalance_slope=depth_slope, size_weighted_ofi=size_ofi)
        assert_v5_capacity(book_depth_notional_l1=book_depth_l1,
                           book_depth_notional_l10=book_depth_l10)
        iid = iid_of.get(str(sym).strip())
        if iid is None:
            unmapped += 1
            continue
        quote_fp, trade_fp, source_fp = external_content_fingerprints(
            day, str(sym), int(n_ticks), int(n_quotes),
            quote_xor_0, quote_sum_1, trade_xor_0, trade_sum_1)
        g = quality_of(int(n_ticks), int(n_quotes))
        grades[g] += 1
        evidence = {
            "n_ticks": int(n_ticks),
            "n_quotes": int(n_quotes),
            "origin": "external",
            "source_content_hash_contract": SOURCE_CONTENT_HASH_CONTRACT,
            "quote_content_fingerprint": quote_fp,
            "trade_content_fingerprint": trade_fp,
            "source_content_fingerprint": source_fp,
        }
        if miss:
            evidence["missing_source"] = "+".join(miss)
        payload.append((
            bucket,
            # 이관 구간과 같은 규칙: 관측 시각이 원본에 없으므로 자리 채움이고,
            # 그 사실은 market.pit_provenance 가 'NONE' 으로 못박는다.
            bucket, iid, "KRX", FEATURE_SET_VERSION,
            rvol, spread, di, ofi, intensity, tvalue, tvolume,
            ofi_c, ofi_o, ofi_sd, cvwap, sp_ratio,
            depth_l1, depth_l10, depth_slope, size_ofi,
            book_depth_l1, book_depth_l10,
            json.dumps(evidence, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")),
            g, bucket, source_fp))

    if dry_run:
        return {"day": day, "rows": len(payload), "unmapped": unmapped,
                **grades, "missing": miss, "partial": partial, "dry_run": True}
    gate = day_origin_guard(market_conn, day, "external", replace=replace)
    if gate != "insert":
        return {"day": day, "rows": 0, "unmapped": unmapped, **grades,
                "note": _GATE_NOTE[gate]}
    with market_conn.cursor() as cur:
        execute_values(cur, _SQL_INSERT, payload, page_size=2000)
    if miss:
        record_feed_gap(market_conn, day, miss, len(payload))
    if partial:
        record_partial_feed_gaps(market_conn, day, partial)
    market_conn.commit()
    return {"day": day, "rows": len(payload), "unmapped": unmapped,
            **grades, "missing": miss, "partial": partial}


def pending_days(market_conn, *, since: date | None = None) -> list[date]:
    """원천에는 있는데 아직 안 접은 날."""
    with market_conn.cursor() as cur:
        cur.execute(_SQL_SOURCE_DAYS)
        src = [r[0] for r in cur.fetchall()]
        cur.execute(_SQL_DONE_DAYS, (FEATURE_SET_VERSION,))
        done = {r[0] for r in cur.fetchall()}
    out = [d for d in src if d not in done and d.isoweekday() <= 5]
    return [d for d in out if since is None or d >= since]


def official_trading_days(meta_conn, start: date, end: date) -> set[date]:
    """Latest versioned KRX regular-session calendar, fail-closed when absent."""
    with meta_conn.cursor() as cur:
        cur.execute("""
            select s.trade_date
              from reference.market_sessions s
              join reference.market_calendar_versions v using (calendar_version_id)
             where s.market='KRX' and s.session_type='REGULAR'
               and s.is_trading_day and s.trade_date between %s and %s
               and v.version=(select max(version)
                                from reference.market_calendar_versions
                               where market='KRX')
             order by s.trade_date
        """, (start, end))
        days = {r[0] for r in cur.fetchall()}
    if not days:
        raise RuntimeError(
            f"KRX 공식 거래일 캘린더가 비었다: {start}~{end}; "
            "평일 추측으로 미시구조 날짜를 만들지 않는다")
    return days


# ── 자체 점검 (DB 없음) ────────────────────────────────────────────────────
def _check_quality_is_marked_not_dropped():
    """얇은 표본을 **버리지 않고 표시한다.** 조용히 빼면 유니버스가 왜 줄었는지 모른다."""
    assert quality_of(100, 100) == "PASS"
    assert quality_of(5, 100) == "WARN"
    assert quality_of(100, 5) == "WARN"
    assert quality_of(0, 0) == "FAIL"
    # 경계에서 통과한다 - 임계값 바로 위가 WARN 이면 하루치가 통째로 밀린다
    assert quality_of(MIN_TICKS_FOR_PASS, MIN_QUOTES_FOR_PASS) == "PASS"
    print("  표본 등급(버리지 않음)   OK")


def _check_session_bounds_exclude_after_hours():
    """정규장만 접는다. 시간외를 섞으면 스프레드·체결강도가 왜곡된다.

    NXT 애프터마켓은 20:00 까지 도는데, 그 구간의 넓은 스프레드가 정규장 통계에
    섞이면 유동성 순위가 뒤집힌다.
    """
    lo, hi = session_bounds(date(2026, 8, 11))
    assert lo.endswith("09:00+09") and hi.endswith("15:30+09"), (lo, hi)
    assert "2026-08-11" in lo and "2026-08-11" in hi
    start, end = external_session_content_window(date(2026, 8, 11))
    assert start.isoformat() == "2026-08-11T09:00:00+09:00"
    assert end.isoformat() == "2026-08-11T15:30:00+09:00"
    assert EXTERNAL_CONTENT_WINDOW_CONTRACT.endswith(
        "HALF_OPEN_0900_1530_KST_V1")
    print("  정규장 구간 한정         OK")


def _check_input_hash_tracks_inputs():
    """원천이 바뀌면 해시가 바뀐다 - 같은 해시면 같은 입력이라는 뜻이어야 한다."""
    d = date(2026, 8, 11)
    a = input_hash(d, 100, 200)
    assert a == input_hash(d, 100, 200)
    assert a != input_hash(d, 101, 200)
    assert a != input_hash(d, 100, 201)
    assert a != input_hash(date(2026, 8, 10), 100, 200)
    print("  입력 해시 결정론         OK")


def _check_external_content_hash_tracks_corrections():
    """Same row counts with corrected raw values must change experiment input."""
    d = date(2026, 8, 11)
    base = external_content_fingerprints(
        d, "005930", 100, 200, "11", "12", "21", "22")
    assert base == external_content_fingerprints(
        d, "005930", 100, 200, "11", "12", "21", "22")
    corrected_quote = external_content_fingerprints(
        d, "005930", 100, 200, "99", "12", "21", "22")
    corrected_trade = external_content_fingerprints(
        d, "005930", 100, 200, "11", "12", "21", "99")
    assert base[0] != corrected_quote[0] and base[2] != corrected_quote[2]
    assert base[1] != corrected_trade[1] and base[2] != corrected_trade[2]
    try:
        external_content_fingerprints(
            d, "005930", 100, 200, None, "12", "21", "22")
    except ValueError as exc:
        assert "quotes content digest missing" in str(exc)
    else:
        raise AssertionError("non-empty external source accepted without digest")
    # A genuinely empty side has a deterministic identity rather than a fake
    # aggregate value; this keeps one-sided trading days auditable.
    empty = external_content_fingerprints(
        d, "005930", 100, 0, None, None, "21", "22")
    assert len(empty[0]) == len(empty[1]) == len(empty[2]) == 64
    print("  외부 원천 내용 지문      OK")


def _check_sql_does_not_zero_fill():
    """**못 잰 것을 0 으로 채우지 않는다.**

    그날 호가가 없던 종목의 스프레드는 0 이 아니라 없음이다. 0 으로 채우면
    '스프레드가 가장 좁은 종목' 을 고를 때 거래가 없던 종목이 1등이 된다.
    """
    s = " ".join(_SQL_BUILD.split())
    for expr in ("coalesce(q.spread_bps", "coalesce(t.ofi",
                 "coalesce(q.depth_imbalance", "coalesce(t.realized_vol"):
        assert expr not in s, f"피처를 0/기본값으로 채우고 있다: {expr}"
    # 표본 수는 0 으로 채워도 된다 - 그건 '행이 없었다' 가 사실이다
    assert "coalesce(t.n_ticks, 0)" in s and "coalesce(q.n_quotes, 0)" in s
    # 분모 0 방어가 값을 0 으로 만들지 않고 NULL 로 두는가
    assert "nullif(price, 0)" in s
    assert "when sum(quantity) > 0" in s
    print("  결측을 0 으로 안 채움    OK")


def _check_full_outer_join_keeps_one_sided_days():
    """호가만 있고 체결이 없는 종목도 사실이다 - 버리면 거래정지가 안 보인다."""
    s = " ".join(_SQL_BUILD.split()).lower()
    assert "full outer join" in s, "한쪽만 있는 종목이 사라진다"
    assert "coalesce(q.instrument_id, t.instrument_id)" in s
    print("  한쪽만 있는 종목 보존    OK")


def _check_intensity_uses_observed_span():
    """체결강도를 고정 분수로 나누면 반나절 장에서 두 배로 부풀어 보인다.

    **주석이 아니라 실행되는 SQL 만 본다.** 처음엔 SQL 전체에서 정규장 길이
    숫자를 찾았는데, 그 숫자를 설명하는 주석 자체에 걸려 통과하지 못했다 -
    검사가 코드가 아니라 문서를 검사하고 있었다.
    """
    code = " ".join(ln.split("--")[0] for ln in _SQL_BUILD.splitlines())
    code = " ".join(code.split())
    assert "max(event_time) - min(event_time)" in code
    for const in ("390", "381", "360"):
        assert f"/ {const}" not in code and f"/{const}" not in code, \
            f"정규장 길이를 상수 {const} 로 박았다"
    print("  체결강도 관측폭 기준     OK")


def _check_external_sql_matches_local_definitions():
    """저쪽 집계가 **우리 것과 같은 것을 재는가.**

    같은 종목의 같은 날 값이 경로마다 다르면 어느 쪽으로 만든 피처인지에 따라
    백테스트 결과가 갈린다. 저쪽은 spread·bi 를 이미 갖고 있으므로 정의를 두
    번 구현하지 않고 그것을 쓴다 - 대신 **같은 단위**여야 한다.
    """
    ext = " ".join(ln.split("--")[0] for ln in _SQL_BUILD_EXTERNAL.splitlines())
    ext = " ".join(ext.split())
    loc = " ".join(ln.split("--")[0] for ln in _SQL_BUILD.splitlines())
    loc = " ".join(loc.split())

    # 스프레드는 양쪽 다 bps(× 10000)다
    assert "* 10000" in ext and "* 10000" in loc, "스프레드 단위가 갈렸다"
    # OFI 는 양쪽 다 부호 있는 체결량 / 총 체결량이고, 분모 0 이면 NULL.
    # 외부 side 는 1/5 코드이므로 반드시 수집기가 만든 ±volume 을 쓴다.
    assert "sum(side * quantity)" in loc
    assert "sum(ofi_contrib)" in ext
    assert "sum(side * volume)" not in ext, "외부 1/5 side 코드를 부호처럼 곱했다"
    for s in (ext, loc):
        assert "> 0 then" in s.replace("  ", " "), "분모 0 방어가 없다"
    # 체결강도는 양쪽 다 관측 폭 기준
    assert "max(ts) - min(ts)" in ext and "max(event_time) - min(event_time)" in loc
    # 결측을 0 으로 채우지 않는 규칙도 같다
    for expr in ("coalesce(q.spread_bps", "coalesce(t.ofi"):
        assert expr not in ext, f"외부 경로가 결측을 채운다: {expr}"
    assert "full outer join" in ext.lower(), "한쪽만 있는 종목이 사라진다"
    print("  외부/내부 정의 일치      OK")


class _GuardCur:
    def __init__(self, have):
        self.have, self._rows, self.deleted = have, [], 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        if s.startswith("delete"):
            self.deleted += 1
            self.have = set()
            self._rows = []
        else:
            self._rows = [(o,) for o in self.have]

    def fetchall(self):
        return list(self._rows)


class _GuardConn:
    def __init__(self, have):
        self.cur = _GuardCur(set(have))

    def cursor(self):
        return self.cur

    def commit(self):
        pass


def _check_one_day_one_origin():
    """**한 날은 한 출처만.** 이 규칙이 없어서 21일이 섞였다 (2026-08-12).

    `on conflict do nothing` 은 먼저 넣은 쪽이 이긴다 - 품질이 아니라 실행
    순서가 승자를 정한다. 로컬 빌드가 먼저 돌아 스프레드가 통째로 NULL 인
    행을 심었고, 나중 외부 빌드가 그것을 못 덮어 **한 날 안에서 종목마다
    품질이 달라졌다.** 횡단면 전략은 그 차이를 표본 선택으로 읽는다.
    """
    d = date(2026, 8, 11)
    # 비어 있으면 넣는다
    assert day_origin_guard(_GuardConn(set()), d, "local") == "insert"
    # 같은 출처면 건너뛴다(재실행이 멱등)
    assert day_origin_guard(_GuardConn({"local"}), d, "local") == "skip"
    # **다른 출처면 막는다** - 이게 핵심이다
    assert day_origin_guard(_GuardConn({"external"}), d, "local") == "blocked"
    assert day_origin_guard(_GuardConn({"local"}), d, "external") == "blocked"
    # 이미 섞인 날도 막는다
    assert day_origin_guard(_GuardConn({"local", "external"}), d, "local") == "blocked"

    # --replace 는 **그날을 통째로 지우고** 넣는다. 부분 덮어쓰기는 없다 -
    # 부분 덮어쓰기가 곧 섞인 상태를 만든다.
    conn = _GuardConn({"external"})
    assert day_origin_guard(conn, d, "local", replace=True) == "insert"
    assert conn.cur.deleted == 1, "교체인데 그날을 안 지웠다"
    # 같은 출처의 원천 정정/지문 계약 변경도 명시적 재구축이 가능해야 한다.
    conn = _GuardConn({"external"})
    assert day_origin_guard(conn, d, "external", replace=True) == "insert"
    assert conn.cur.deleted == 1, "같은 출처 --replace 가 재구축을 건너뛴다"

    # 막힌 이유가 사람이 읽는 문장으로 남아야 한다 - 조용한 0행은 원인을 숨긴다
    assert "섞지 않는다" in _GATE_NOTE["blocked"]
    assert "--replace" in _GATE_NOTE["blocked"]
    print("  한 날 한 출처            OK")


def _check_build_paths_go_through_guard():
    """두 빌드 경로가 **둘 다** 가드를 지나는가. 한쪽만 지나면 그쪽으로 샌다."""
    import inspect

    for fn, origin in ((build_day, '"local"'), (build_day_external, '"external"')):
        src = inspect.getsource(fn)
        assert "day_origin_guard(" in src, f"{fn.__name__} 이 가드를 안 지난다"
        assert origin in src, f"{fn.__name__} 이 출처를 안 밝힌다"
        # 가드가 INSERT 보다 앞서야 한다
        assert src.index("day_origin_guard") < src.index("_SQL_INSERT"), \
            f"{fn.__name__}: 가드가 적재 뒤에 온다"
    print("  두 경로 모두 가드 통과   OK")


def _check_fdw_path_is_the_same_aggregation():
    """**FDW 경로가 외부 경로와 같은 집계여야 한다** (2026-08-14).

    스키마 이름만 다르고 정의는 글자 하나까지 같아야 한다. 어긋나면 같은
    종목의 같은 날이 **어느 경로로 접었느냐에 따라 다른 값**을 갖는다 -
    그런 데이터로는 어떤 신호도 검증할 수 없다.
    """
    assert "ext_src.ticks" in _SQL_BUILD_FDW and "ext_src.quotes" in _SQL_BUILD_FDW
    assert "public.ticks" not in _SQL_BUILD_FDW, "치환이 덜 됐다"
    assert "public.quotes" not in _SQL_BUILD_FDW, "치환이 덜 됐다"
    # 스키마만 되돌리면 완전히 같은 문자열이어야 한다
    back = (_SQL_BUILD_FDW.replace("ext_src.quotes", "public.quotes")
                          .replace("ext_src.ticks", "public.ticks"))
    assert back == _SQL_BUILD_EXTERNAL, "두 경로의 집계 정의가 갈렸다"
    # 거래대금은 **두 경로 모두** 백만원으로 접는다 - 단위가 갈리면 유동성
    # 필터가 출처에 따라 다르게 판정한다
    for sql in (_SQL_BUILD, _SQL_BUILD_EXTERNAL):
        assert "/ 1e6" in sql, "거래대금을 원 단위로 담고 있다(백만원이어야 한다)"
        assert "traded_value" in sql

    # ▶ **원천을 두 번 읽지 않는다** (2026-08-14 사고)
    #   일중 구간 표준편차를 별도 CTE 로 내면서 `market_ticks`/`public.ticks` 를
    #   한 번 더 group by 했다. 로컬에서는 느릴 뿐이지만 **FDW 로는 하루
    #   1,900만 행을 두 번 끌어오는 것**이라 빌더가 죽었다(v3 가 39일에서 멈춤).
    #   `filter` + `unnest` 로 같은 스캔에서 내면 값은 그대로다.
    for name, sql, tbl in (("내부", _SQL_BUILD, "market.market_ticks"),
                           ("외부", _SQL_BUILD_EXTERNAL, "public.ticks")):
        assert sql.count(f"from {tbl}") == 1, \
            f"{name} 경로가 {tbl} 을 {sql.count(f'from {tbl}')}번 읽는다"
        assert "ofi_intraday_std" in sql, name
        assert "unnest(array[" in sql, f"{name} 경로가 구간 배열을 안 쓴다"
    print("  FDW=외부 같은 집계       OK")


def _check_v4_depth_and_size_axes_are_explicit():
    """출처마다 뜻이 달랐던 깊이를 다시 한 이름으로 뭉개지 않는가."""
    local = " ".join(_SQL_BUILD.split())
    external = " ".join(_SQL_BUILD_EXTERNAL.split())
    for sql in (local, external):
        for field in ("depth_imbalance_l1", "depth_imbalance_l10",
                      "depth_imbalance_slope", "size_weighted_ofi"):
            assert field in sql, (field, "v4 피처가 한 집계 경로에서 빠졌다")
        assert sql.count("size_weighted_ofi") >= 2
    assert "bid_sizes[1]" in local and "ask_sizes[1]" in local
    assert "avg(depth_imbalance) depth_imbalance_l10" in local
    assert "avg(bi::float8) depth_imbalance_l1" in external
    assert "bid_vol10" in external and "ask_vol10" in external
    assert "ofi_contrib::numeric * volume" in external
    # 이벤트 순번 창을 섣불리 추가해 FDW 전체 정렬을 일으키지 않는다.
    assert "row_number(" not in local.lower() and "row_number(" not in external.lower()
    print("  L1/L10/체결크기 축 명시   OK")


def _check_v4_bounds_fail_closed():
    assert_v4_bounds(order_flow_imbalance=-1, size_weighted_ofi=1,
                     depth_imbalance_slope=2)
    for name, value in (("order_flow_imbalance", 2.8),
                        ("depth_imbalance_l10", -1.1),
                        ("depth_imbalance_slope", 2.1)):
        try:
            assert_v4_bounds(**{name: value})
        except ValueError as exc:
            assert name in str(exc) and "outside" in str(exc)
        else:
            raise AssertionError(f"범위 밖 {name} 을 적재 허용했다")
    print("  v4 값 범위 fail-closed    OK")


def _check_v5_depth_capacity_is_explicit():
    for sql in (_SQL_BUILD, _SQL_BUILD_EXTERNAL):
        assert "book_depth_notional_l1" in sql
        assert "book_depth_notional_l10" in sql
        assert "/ 1e6" in sql, "절대 깊이 단위가 백만원이 아니다"
    assert "bid_prices[1] * bid_sizes[1]" in _SQL_BUILD
    assert "bid_vol10" in _SQL_BUILD_EXTERNAL and "ask_vol10" in _SQL_BUILD_EXTERNAL
    assert_v5_capacity(book_depth_notional_l1=0,
                       book_depth_notional_l10=100.0)
    try:
        assert_v5_capacity(book_depth_notional_l1=-0.01)
    except ValueError as exc:
        assert "book_depth_notional_l1" in str(exc) and "negative" in str(exc)
    else:
        raise AssertionError("음의 절대 호가 수용력을 적재 허용했다")
    print("  v5 절대 호가 수용력      OK")


def _check_whole_day_source_loss_is_not_a_row_grade():
    """**하루 전체가 빈 것과 한 종목이 얇은 것은 다른 사실이다** (2026-08-14 실측).

    2026-08-10 에 `market.market_quotes` 가 0행이었다(체결은 1,348만건 정상,
    저쪽 원본에는 1,851만건 있었다). 그런데 보이는 것은 `WARN 2,473` 뿐이었고
    `market.feed_gaps` 는 0행이었다 - **감지표가 있는데 채우는 쪽이 없었다.**
    그날 스프레드를 읽는 신호는 전 종목 미산출 = 조용히 거래 0 이 된다.
    """
    # (…, n_ticks, n_quotes, …) 자리만 맞춘 최소 행
    def row(nt, nq):
        return (None,) * 6 + (nt, nq) + (None,) * 9

    assert missing_sources([row(100, 50), row(80, 40)]) == []
    assert missing_sources([row(100, 0), row(80, 0)]) == ["quotes"]
    assert missing_sources([row(0, 50), row(0, 40)]) == ["ticks"]
    assert missing_sources([row(0, 0)]) == ["quotes", "ticks"]
    assert missing_sources([]) == []
    # **일부 종목만 0 인 것은 결손이 아니다** - 그건 원래 WARN 이 할 일이다
    assert missing_sources([row(100, 0), row(80, 30)]) == []

    # 등급 규칙은 안 건드렸다 - 새 진단이 기존 판정을 바꾸면 재현이 깨진다
    assert quality_of(100, 100) == "PASS"
    assert quality_of(100, 0) == "WARN"
    assert quality_of(0, 0) == "FAIL"

    # 두 경로 모두 결손을 보고 기록해야 한다 - 한쪽만 하면 출처에 따라 놓친다
    import inspect
    for fn in (build_day, build_day_external):
        src = inspect.getsource(fn)
        assert "missing_sources(rows)" in src, f"{fn.__name__} 이 결손을 안 본다"
        assert "record_feed_gap(" in src, f"{fn.__name__} 이 표에 안 남긴다"
        assert "missing_source" in src, f"{fn.__name__} 이 행에 표시를 안 남긴다"
    # 기록 실패가 적재를 막지 않는다 - 진단이 본작업을 죽이면 안 된다
    assert "except Exception" in inspect.getsource(record_feed_gap)
    print("  하루 전체 결손 = 별도 사실 OK")


def _check_partial_universe_loss_is_reported():
    def row(nt, nq):
        return (None,) * 6 + (nt, nq) + (None,) * 9

    normal = [row(100, 100)] * 95 + [row(100, 0)] * 5
    assert partial_source_gaps(normal) == []
    broken = [row(100, 100)] * 800 + [row(100, 0)] * 200
    gaps = partial_source_gaps(broken)
    assert len(gaps) == 1 and gaps[0]["source"] == "quotes"
    assert gaps[0]["affected_rows"] == 200
    # The absolute floor prevents tiny universes from raising an incident.
    assert partial_source_gaps([row(100, 100)] * 8 + [row(100, 0)] * 2) == []
    print("  partial universe source loss reporting OK")


def _selfcheck() -> int:
    print(f"{BUILDER_VERSION} 자체 점검 (DB 없음)")
    _check_one_day_one_origin()
    _check_build_paths_go_through_guard()
    _check_external_sql_matches_local_definitions()
    _check_quality_is_marked_not_dropped()
    _check_session_bounds_exclude_after_hours()
    _check_input_hash_tracks_inputs()
    _check_external_content_hash_tracks_corrections()
    _check_sql_does_not_zero_fill()
    _check_full_outer_join_keeps_one_sided_days()
    _check_intensity_uses_observed_span()
    _check_fdw_path_is_the_same_aggregation()
    _check_v4_depth_and_size_axes_are_explicit()
    _check_v4_bounds_fail_closed()
    _check_v5_depth_capacity_is_explicit()
    _check_whole_day_source_loss_is_not_a_row_grade()
    _check_partial_universe_loss_is_reported()
    print("마이크로구조 빌더 16개 영역 통과. 실행은 --build")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="호가·체결 -> 일별 마이크로구조 피처")
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    # 기존 날짜를 **통째로 지우고** 다시 넣는다. 같은 출처의 원천 정정과
    # 내용-지문 계약 갱신도 포함한다. 부분 덮어쓰기는 없다.
    ap.add_argument("--replace", action="store_true",
                    help="기존 날짜가 있으면 그날 전체를 지우고 재구축한다")
    ap.add_argument("--from", dest="since", default="")
    ap.add_argument("--through", dest="through", default="",
                    help="마지막 거래일(포함). 병렬 백필을 겹치지 않게 나눌 때 사용")
    # 저쪽(Trading_bot) DB 에서 집계한다. 우리 원장의 호가 구멍을 원시 이관
    # 없이 메우는 경로다 - 3.9억 행 대신 15만 행만 움직인다.
    ap.add_argument("--external-dsn", default="",
                    help="Trading_bot ticks DB DSN. 주면 그쪽에서 집계한다")
    ap.add_argument("--days", type=int, default=0,
                    help="--external-dsn/--fdw 와 함께: 최근 N 거래일만")
    # ▶ **자격증명 없이 저쪽을 읽는다** (2026-08-14). 우리 DB 에 `ext_src`
    #   스키마가 `trading_src` foreign server 로 이미 걸려 있다 - DSN 을 손으로
    #   나르는 대신 같은 커넥션에서 읽는다. 집계 정의는 두 경로가 같다.
    ap.add_argument("--fdw", action="store_true",
                    help="ext_src.* (foreign table)에서 집계한다. DSN 불필요")
    a = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if not a.build:
        return _selfcheck()

    import psycopg2
    from source_registry import load_project_env

    env = load_project_env()
    conn = meta = src = None
    try:
        conn = psycopg2.connect(env["TIMESCALE_DATABASE_URL"], connect_timeout=20)
        meta = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
        src = (psycopg2.connect(a.external_dsn, connect_timeout=20)
               if a.external_dsn else None)
        since = date.fromisoformat(a.since) if a.since else None
        through = date.fromisoformat(a.through) if a.through else None
        if since and through and since > through:
            raise SystemExit("--from 은 --through 보다 늦을 수 없다")
        # postgres_fdw가 이 복합 집계를 pushdown하지 않는 것을 2026-08-16
        # 실측했다: 원격에서 `FETCH 50000 FROM c1`로 원시 호가를 계속 보내며
        # 하루도 수분이 걸리고, 호출 컨테이너 종료 뒤 백엔드가 남았다. 진단용
        # 하루는 허용하되 다일 백필은 원천 DB에서 집계하는 --external-dsn만 쓴다.
        one_explicit_day = bool(since and through and since == through)
        if a.fdw and not (one_explicit_day or a.days == 1):
            raise SystemExit(
                "--fdw 복합 집계는 원시행을 전송하므로 한 번에 하루만 허용한다. "
                "다일 백필은 --external-dsn 으로 원천 DB에서 집계하라")
        external = src is not None or a.fdw
        if external:
            # 저쪽이 가진 거래일을 기준으로 삼는다. 우리 원장의 청크를 기준으로
            # 하면 호가가 빈 날이 "이미 있다" 로 잡혀 그대로 넘어간다.
            #
            # ▶ **FDW 로는 `distinct ts::date` 를 묻지 않는다** (2026-08-14 실측)
            #   postgres_fdw 는 DISTINCT 를 pushdown 하지 않는다 - 11.29억 행을
            #   통째로 끌어온다. 같은 실수가 오늘 다른 자리에서 5시간 33분짜리
            #   쿼리를 만들고 시장 API 를 3시간 넘게 세웠다.
            #   저쪽이 가진 거래일은 **이미 접어 둔 피처의 날짜**로 안다 -
            #   우리 테이블 조회라 즉시 끝나고, 그 날들이 곧 저쪽 원천의 날이다.
            if since and through:
                days = []
                cursor_day = since
                while cursor_day <= through:
                    days.append(cursor_day)
                    cursor_day += timedelta(days=1)
            elif src is not None:
                with src.cursor() as cur:
                    # ts는 timestamptz다. 연결의 기본 UTC에서 ts::date를 쓰면
                    # KST 다음 날 새벽의 시험/장외 이벤트가 전날로 분류된다
                    # (실측: 공식 거래일 2026-06-09가 목록에는 있었지만 KST
                    # 정규장 행은 0). 집계 session_bounds와 같은 KST 날짜로 센다.
                    cur.execute("""
                        select session_date from (
                          select distinct
                                 (ts at time zone 'Asia/Seoul')::date session_date
                            from public.ticks
                          union
                          select distinct
                                 (ts at time zone 'Asia/Seoul')::date session_date
                            from public.quotes
                        ) source_days
                        order by session_date""")
                    days = [r[0] for r in cur.fetchall()]
            else:
                with conn.cursor() as cur:
                    cur.execute("""
                        select distinct (event_time at time zone 'Asia/Seoul')::date
                          from market.microstructure_features
                         order by 1""")
                    days = [r[0] for r in cur.fetchall()]
            if a.replace:
                # Reconcile source ∪ target so a source-side deletion cannot
                # hide a stale target day. An explicit bounded range also adds
                # absent dates; the official calendar below removes holidays.
                with conn.cursor() as cur:
                    cur.execute(_SQL_DONE_DAYS, (FEATURE_SET_VERSION,))
                    target_days = {r[0] for r in cur.fetchall()}
                days = sorted(set(days) | target_days)
                if since and through:
                    cursor_day = since
                    while cursor_day <= through:
                        days.append(cursor_day)
                        cursor_day += timedelta(days=1)
                    days = sorted(set(days))
            # 외부 수집 DB에는 일요일 연결 시험/잔존 이벤트가 실제로 있었다
            # (2026-05-31~08-09 11일). 거래소 세션이 아닌 날짜를 일별 피처로
            # 만들면 walk-forward 달력이 조용히 늘어나므로 평일만 허용한다.
            days = [d for d in days if d.isoweekday() <= 5]
            if since:
                days = [d for d in days if d >= since]
            if through:
                days = [d for d in days if d <= through]
            if a.days:
                days = days[-a.days:]
            # 원천 집계는 하루 수천만 행을 읽는다. 같은 v5 날짜가 이미 있으면
            # `day_origin_guard`까지 전부 계산한 뒤 skip하지 말고 쿼리 전에 뺀다.
            # --replace와 --dry-run은 의도적으로 재계산하는 경로라 제외한다.
            if not a.replace and not a.dry_run:
                with conn.cursor() as cur:
                    cur.execute(_SQL_DONE_DAYS, (FEATURE_SET_VERSION,))
                    done = {r[0] for r in cur.fetchall()}
                days = [d for d in days if d not in done]
        else:
            days = pending_days(conn, since=since)
        if days:
            calendar = official_trading_days(meta, min(days), max(days))
            days = [d for d in days if d in calendar]
        print(f"{BUILDER_VERSION}: 접을 날 {len(days)}건"
              + (f" ({days[0]} ~ {days[-1]})" if days else "")
              + ("  [외부 원천 - FDW]" if a.fdw and src is None else
                 "  [외부 원천]" if external else ""), flush=True)
        total, unmapped = 0, 0
        for i, d in enumerate(days, 1):
            r = (build_day_external(conn, src, d, dry_run=a.dry_run,
                                    replace=a.replace)
                 if external
                 else build_day(conn, d, dry_run=a.dry_run, replace=a.replace))
            total += r["rows"]
            unmapped += r.get("unmapped", 0)
            print(f"  [{i}/{len(days)}] {d}: {r['rows']:,}종목 "
                  f"PASS {r.get('PASS', 0)} WARN {r.get('WARN', 0)} "
                  f"FAIL {r.get('FAIL', 0)}"
                  + (f" 미매핑 {r['unmapped']}" if r.get("unmapped") else "")
                  # **원천 결손은 종목 등급과 다른 사실이다.** 08-10 에 호가가
                  # 하루 통째로 빠졌는데 WARN 2,473 으로만 보여 지나갔다.
                  + (f"  ★ 원천 결손: {'+'.join(r['missing'])} 이 0행 "
                     f"- 그 원천에서 나오는 피처는 전 종목 미산출이다"
                     if r.get("missing") else "")
                  + (f"  {r['note']}" if r.get("note") else ""),
                  flush=True)
        # 미매핑을 총계로도 남긴다 - 하루씩 보면 작아 보여도 쌓이면 유니버스가 준다
        print(f"  완료: {total:,}행"
              + (f" / 종목 미매핑 누적 {unmapped:,}" if unmapped else ""), flush=True)
    finally:
        if conn is not None:
            conn.close()
        if meta is not None:
            meta.close()
        if src is not None:
            src.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
