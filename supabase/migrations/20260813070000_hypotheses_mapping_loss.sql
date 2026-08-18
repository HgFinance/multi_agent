begin;

-- 사상 손실 각인 (2026-08-13)
-- 가설-실행 정합성 감사 실측: 실험이 돈 가설 41건이 서로 다른 config 19개로
-- 접혔고(SMA20 매도 4건이 전부 REV-5 로, MAX 가설이 LOWVOL 로 실행),
-- 원장 어디에도 번역에서 무엇이 사라졌는지 남지 않았다. 판정은 남은 것만 보고
-- 성적표를 붙였다 - 검증된 적 없는 가설에. 접수 시점에 기계 계산한 손실을
-- 가설 행에 각인해 판정·회고가 "무엇이 검증됐고 무엇이 관례였나"를 보게 한다.
alter table quant.hypotheses
  add column if not exists mapping_loss jsonb not null default '{}'::jsonb;

comment on column quant.hypotheses.mapping_loss is
  '가설→실행 번역 손실 각인. dropped_keys=실행면이 안 읽어 버려진 파라미터, '
  'defaulted_keys=가설이 안 정해 실행 관례가 채우는 손잡이, '
  'identity_hints=파라미터 칸에 적힌 정체성 값 중 전용 필드와 다른 것. '
  '빈 객체 = 무손실. factory_bridge.mapping_loss_of 가 접수 시점에 계산한다.';

commit;
