begin;

-- 가설 상태 머신 통합 + 사전등록 지문
--
-- 담당: 재일 (퀀트·백테스트본부 QNT)
-- 근거: HEDGE_FUND_MASTER_PLAN.md 7.2·7.6절, 재일님 계획 2026-08-04
--       "PIT Dataset과 사전등록 강화 - 결과 확인 전에 고정한다"
--
-- ▶ 상태가 세 갈래로 갈라져 있었다 (2026-08-04 실측)
--     계약 quant_v2.HypothesisStatus : INTAKE / PREREGISTERED /
--                                      DATASET_CERTIFIED / RUNNING /
--                                      ROBUSTNESS_REVIEW / SUPPORTED /
--                                      REJECTED / NEEDS_DATA
--     DB 제약                        : PROPOSED / APPROVED / TESTING /
--                                      SUPPORTED / REJECTED / ARCHIVED
--     실행부                         : PROPOSED -> TESTING -> {SUPPORTED,
--                                      REJECTED, INCONCLUSIVE}
--
--   계약의 PREREGISTERED·DATASET_CERTIFIED 가 **결과를 본 뒤 설정을 바꾸는
--   것을 막는 관문**인데 호출처가 0개였다. 상태가 갈라진 채로는 그 관문을
--   켤 수 없다.
--
-- ▶ 옛 값을 지우지 않는다
--   기존 14행이 PROPOSED/TESTING 을 쓰고 있다. 지우면 그 행들이 제약을
--   위반해 마이그레이션이 죽는다. 둘 다 허용하고 실행부가 새 값을 쓰게 한 뒤
--   옛 값이 0행이 되면 그때 뺀다 - 한 번에 바꾸면 어느 하나만 먼저 반영돼도
--   UPDATE 가 죽는다(오늘 INCONCLUSIVE 로 실제 그럴 뻔했다).

alter table quant.hypotheses
  drop constraint if exists hypotheses_status_check;

alter table quant.hypotheses
  add constraint hypotheses_status_check
  check (status in (
    -- 계약(7.6절) 상태
    'INTAKE', 'PREREGISTERED', 'DATASET_CERTIFIED', 'RUNNING',
    'ROBUSTNESS_REVIEW', 'SUPPORTED', 'REJECTED', 'NEEDS_DATA',
    'INCONCLUSIVE',
    -- 이행기 호환. 기존 행이 쓰고 있어 남긴다(0행이 되면 제거)
    'PROPOSED', 'APPROVED', 'TESTING', 'ARCHIVED'
  ));

-- ▶ 사전등록 지문. **결과를 보기 전에** 실질 내용(Feature/Label/Split/비용/
--   폐기 기준/유니버스/지평/baseline 등 11개 필드)의 해시를 박아둔다.
--   실험이 끝난 뒤 이 값이 달라졌다면 결과를 보고 설정을 바꾼 것이다.
alter table quant.hypotheses
  add column if not exists preregistered_at timestamptz,
  add column if not exists material_fingerprint text;

comment on column quant.hypotheses.material_fingerprint is
  '사전등록 시점 실질 내용 해시(quant_v2.material_fingerprint). 실험 후 값이 '
  '다르면 결과를 보고 설정을 바꾼 것이다 - 그 실험은 무효이며 새 Version 이어야 '
  '한다. status·version·preregistered_at 은 실질이 아니라 해시에 안 들어간다.';

comment on column quant.hypotheses.preregistered_at is
  '사전등록 시각. 이 시각 이후의 설정 변경은 사후 조정이다.';

-- 같은 지문이 여러 번 사전등록되는 것을 막지 않는다 - 같은 가설을 다른
-- 기간에 다시 검증하는 것은 정상이다. 다만 **한 가설 안에서** 지문이 바뀌면
-- 그것이 문제이고, 그건 애플리케이션이 대조한다.
create index if not exists hypotheses_fingerprint_idx
  on quant.hypotheses (material_fingerprint)
  where material_fingerprint is not null;

commit;
