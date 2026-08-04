begin;

-- 가설 상태에 INCONCLUSIVE 추가
--
-- 담당: 재일 (퀀트·백테스트본부 QNT)
--
-- 왜: 2026-08-04 에 trial_pressure(다중검정 압력)를 상태 전이에 반영하면서
--     "ROBUST 인데 시도 예산 초과" 를 INCONCLUSIVE 로 두게 했다. 그런데 DB
--     제약에는 그 값이 없어 **예산 초과 전략이 실제로 나오는 순간 UPDATE 가
--     죽는다.** 코드에서만 새 상태를 만들고 스키마를 안 고친 결함이다.
--
-- 왜 REJECTED 로 안 쓰는가: 틀렸다는 뜻이 아니다. 이 표본으로는 실력과 운을
--     구분할 수 없다는 뜻이고, 표본이 쌓이면 다시 판정할 수 있다. 둘을 같은
--     값으로 뭉치면 "폐기" 와 "판정 보류" 가 구분되지 않는다.

alter table quant.hypotheses
  drop constraint if exists hypotheses_status_check;

alter table quant.hypotheses
  add constraint hypotheses_status_check
  check (status in ('PROPOSED', 'APPROVED', 'TESTING', 'SUPPORTED',
                    'REJECTED', 'INCONCLUSIVE', 'ARCHIVED'));

comment on column quant.hypotheses.status is
  'PROPOSED->TESTING->{SUPPORTED|REJECTED|INCONCLUSIVE}. '
  'INCONCLUSIVE 는 폐기가 아니라 판정 보류다 - 시도 예산을 넘겨 실력과 운을 '
  '구분할 수 없는 상태이며, 표본이 쌓이면 재판정한다.';

commit;
