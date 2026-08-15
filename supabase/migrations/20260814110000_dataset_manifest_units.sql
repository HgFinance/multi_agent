begin;

-- 데이터셋 매니페스트에 금액·수량 **단위**를 선언한다.
--
-- ▶ 왜 (2026-08-14 실측 사고)
--   `krx-basket-daily/v3` 의 `notional` 은 백만원 단위인데 매니페스트에는 열
--   이름만 있고 단위가 없었다. 실행면은 코드에 박힌 상수를 유일한 근거로
--   삼았고, 그 가정이 실물과 어긋나자 체결가능 유니버스가 **0종목**이 됐다.
--   자체점검 12개가 전부 통과한 채로 실험 6건이 빈 유니버스를 돌았고,
--   그중 2건은 "전략이 나쁘다"(REJECT)로 기록되기까지 했다.
--
--   단위는 데이터의 성질이지 코드의 의견이 아니다. **데이터가 자기 단위를
--   말하게** 하고, 로더 경계에서 실행면 가정과 대조해 어긋나면 멈춘다.
--
-- ▶ 왜 전용 컬럼인가
--   `schema_definition` jsonb 에 넣어도 값은 담기지만, 준비도 진단
--   (`data_readiness.judge_unit_declaration`)이 컬럼 존재로 판정한다 -
--   선언 여부가 **스키마로 강제되어야** 새 데이터셋이 단위 없이 등재되는
--   것을 막을 수 있다. 검산 근거 같은 서술은 그대로 jsonb 에 남긴다.
--
-- ▶ nullable 로 둔다
--   기존 매니페스트 4건이 미선언 상태다. not null 로 만들면 그 행들이
--   막혀 공장이 선다. 로더는 "없음"(경고)과 "틀림"(중단)을 가른다 -
--   채워진 뒤에 어긋남을 확실히 잡는 것이 목적이지, 지금 세우는 것이 아니다.

alter table quant.dataset_manifests
  add column if not exists notional_unit text,
  add column if not exists volume_unit text;

comment on column quant.dataset_manifests.notional_unit is
  '거래대금 열의 단위. KRW / KRW_THOUSAND / KRW_MILLION / KRW_BILLION. '
  '로더(backtest_runner.assert_declared_units)가 실행면 가정과 대조한다.';
comment on column quant.dataset_manifests.volume_unit is
  '거래량 열의 단위. 보통 SHARES(주).';

-- 실측으로 확인한 것만 채운다. v3 는 2026-08-14 검산:
--   close x volume / notional 중앙값 1,003,967 (표본 400행)
--   오차 0.4% 는 종가 대 일평균가 차이다.
-- v1·v2 는 파티션 파일이 남아 있지 않아 검산할 수 없다 - **추측해서 채우지
-- 않는다.** 미선언인 채로 두면 로더가 경고를 남기고, 그것이 사실이다.
update quant.dataset_manifests
   set notional_unit = 'KRW_MILLION',
       volume_unit   = 'SHARES'
 where name = 'krx-basket-daily'
   and version = 'v3'
   and notional_unit is null;

commit;
