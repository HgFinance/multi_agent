-- 시도 Family 를 실험에 기록한다
--
-- 담당: 재일 (퀀트·백테스트본부 QNT)
-- 근거: contracts/quant_v2 "trial_family_id 로 비슷한 실험 횟수를 누적해
--       몇 번 만에 나온 결과인지 Card 에 남긴다"
--
-- ▶ 왜 저장하는가 (매번 계산하지 않고)
--   Family 는 유니버스 서술을 통제 어휘로 사상해 만든다. 어휘를 늘리면
--   **과거 실험의 Family 배정이 소급 변경**되어 "12번째 시도" 가 어제와
--   오늘 다른 값이 된다. 시도 압력은 기록된 사실이어야 한다 - 배정 시점의
--   값을 그대로 남긴다.
--
-- ▶ trial_number 는 배정 시점의 순번이다
--   나중에 같은 Family 실험이 더 생겨도 이 실험이 몇 번째였는지는 안 바뀐다.

alter table quant.experiments
  add column if not exists trial_family_id text,
  add column if not exists trial_number    integer;

-- Family 단위 계수를 자주 돌린다
create index if not exists idx_quant_experiments_trial_family
  on quant.experiments (trial_family_id)
  where trial_family_id is not null;

-- 순번은 1부터다. **0 이나 음수는 "안 셌다" 와 구분이 안 된다.**
alter table quant.experiments
  drop constraint if exists chk_quant_experiments_trial_number;
alter table quant.experiments
  add constraint chk_quant_experiments_trial_number
  check (trial_number is null or trial_number >= 1);

-- Family 를 못 정한 실험은 순번도 없다. 한쪽만 있으면 계수가 틀린다.
alter table quant.experiments
  drop constraint if exists chk_quant_experiments_trial_pair;
alter table quant.experiments
  add constraint chk_quant_experiments_trial_pair
  check ((trial_family_id is null) = (trial_number is null));

comment on column quant.experiments.trial_family_id is
  '같은 컨셉의 변형을 묶는 식별자. 파라미터(lookback, top_n)는 제외하고 '
  'edge type + 유니버스 통제어휘 + 라벨 + baseline 으로 만든다. '
  '파라미터를 넣으면 변형마다 새 Family 가 되어 다중검정을 못 센다. '
  '사상 실패 시 null - 억지로 묶으면 남의 시도가 내 압력이 된다.';

comment on column quant.experiments.trial_number is
  '배정 시점 기준 이 Family 의 몇 번째 시도인가(1부터). '
  '12번째 시도에서 나온 Sharpe 1.5 는 1번째와 다르게 읽어야 한다.';
