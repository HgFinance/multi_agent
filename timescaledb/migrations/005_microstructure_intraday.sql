-- 일중 구간별 미시구조 피처. **일별 한 행이되 하루를 평균으로 뭉개지 않는다.**
--
-- ▶ 왜 (2026-08-14 실측, 재일님 "호가 체결만 있으면 가공하면 다 만들 수 있지 않음?")
--   맞다 - 원천은 이미 다 있다(체결 11.29억건, 호가 10단계). 문제는 접는 방식이었다.
--   현행 OFI 는 `sum(side*qty)/sum(qty)` 로 **하루 전체를 하나로 평균**한다.
--   오전에 팔고 오후에 산 종목은 중립으로 찍힌다 - 정보가 상쇄돼 사라진다.
--
--   2026-08-13 하루로 재 봤다(체결 30건 이상 2,420종목):
--     마감 30분 OFI 표준편차 0.4424  vs  하루 전체 OFI 0.2538   (1.74배)
--     둘의 상관 0.3713  <- 서로 다른 것을 잰다. 새 정보가 있다.
--   일별 OFI 로 돌린 첫 수식형 알파가 IC +0.012(t 0.79)로 죽은 것이 이 압축 때문이다.
--
-- ▶ **실행면은 한 줄도 안 바뀐다.** 여전히 (종목, 일자) 한 행이라
--   `Market.micro` 도 walk-forward 도 체결 규약(t-1 시그널 / t 시가)도 그대로다.
--   일중에 *거래*하는 것은 별개 작업이고 여기서 하지 않는다.
--
-- ▶ 구간(KST): 개장 09:00-09:30 · 오전 09:30-12:00 · 오후 12:00-14:50 ·
--   마감 14:50-15:30(동시호가 포함). 마감을 40분으로 잡은 것은 15:20 이후
--   동시호가에 체결이 몰려 30분으로 자르면 그 물량이 통째로 빠지기 때문이다.

alter table market.microstructure_features
  add column if not exists ofi_close          double precision,
  add column if not exists ofi_open           double precision,
  add column if not exists ofi_intraday_std   double precision,
  add column if not exists close_vs_vwap      double precision,
  add column if not exists spread_close_ratio double precision;

comment on column market.microstructure_features.ofi_close is
  '14:50~15:30 주문흐름불균형. 체결이 t+1 시가라 시점이 맞는다(PIT 성립)';
comment on column market.microstructure_features.ofi_open is
  '09:00~09:30 주문흐름불균형';
comment on column market.microstructure_features.ofi_intraday_std is
  '4구간 OFI 의 표본표준편차. 방향이 오락가락한 날과 한 방향인 날을 가른다';
comment on column market.microstructure_features.close_vs_vwap is
  '종가/VWAP - 1. 하루 내내 매수 압력이 있었으면 양수';
comment on column market.microstructure_features.spread_close_ratio is
  '마감 30분 평균 스프레드 / 개장 30분 평균 스프레드. 유동성의 일중 변화';
