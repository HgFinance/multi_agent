begin;

-- research: 주장에 확률과 인용을 붙인다 (선순환의 빠진 두 고리)
--
-- 소유: 재일 (리서치본부)
-- 근거: WHOLE_SYSTEM_ADVANCEMENT_ROADMAP.md 6.2 P0 5항(Claim/Citation/Forecast/
--       Resolution Rule/Outcome Schema) 및 6.2 핵심지표
--       (Claim Citation Correctness, Forecast Brier Score·Calibration Error).
--
-- ▶ 왜 지금 둘을 같이 넣는가
--   지금 packet_claims 는 "5거래일 안에 -10% 를 터치하나" 같은 **이진 조건**뿐이다.
--   그래서 두 가지를 못 한다:
--     ① 확률이 없어 Brier Score·Calibration Error 를 **원리적으로** 못 센다.
--        '발동했나/안 했나' 카운트로는 분석가가 과신하는지 소심한지 알 수 없다.
--     ② 어떤 문서에 근거했는지가 없어 Citation Correctness 를 못 센다. 나중에
--        어떤 소스가 틀린 것으로 밝혀져도 그에 기댄 과거 판단을 역추적 못 한다.
--   확률만 넣으면 "얼마나 확신했는지는 아는데 왜 그랬는지 모르는" 상태가 되고,
--   인용만 넣으면 그 반대다. 선순환에는 둘 다 필요하다.
--
-- ▶ 왜 research_packet_evidence 를 안 쓰는가
--   그 테이블(20260729000300:205)은 research_packets 를 참조하고, 그것은
--   governance.cases 와 accounting.funds 를 **NOT NULL FK** 로 참조한다.
--   두 테이블 모두 0행이라 영주님(GOV-01)의 Case 발급 전에는 **삽입 자체가
--   불가능**하다. 남의 본부를 기다리며 우리 인용을 못 남기는 것보다,
--   우리 스키마 안에서 claim 단위로 남기고 나중에 합치는 쪽이 낫다.
--   (합칠 때 claim_id -> research_packet_id 매핑만 있으면 된다.)
--
-- 되돌리기: drop table research.claim_evidence; alter table ... drop column ...

-- ── 1) 확률 예측 (Forecast) ────────────────────────────────────────────────
alter table research.packet_claims
  add column if not exists probability numeric,
  add column if not exists probability_method text,
  add column if not exists falsification_note text;

-- 확률은 0~1 이다. 범위를 코드에만 두면 언젠가 1.5 가 들어온다.
alter table research.packet_claims
  drop constraint if exists packet_claims_probability_range;
alter table research.packet_claims
  add constraint packet_claims_probability_range
  check (probability is null or (probability >= 0 and probability <= 1));

-- 확률이 있으면 **그 확률을 어떻게 냈는지도 있어야 한다.** 근거 없는 확률은
-- 숫자로 위장한 직감이고, 그것으로 Calibration 을 재면 재는 것 자체가 거짓이다.
alter table research.packet_claims
  drop constraint if exists packet_claims_probability_needs_method;
alter table research.packet_claims
  add constraint packet_claims_probability_needs_method
  check (probability is null or probability_method is not null);

comment on column research.packet_claims.probability is
  '이 주장이 지평 안에 발동할 사전 확률(0~1). 코드가 계산한다 - LLM 이 자기 '
  '확률을 쓰게 두면 맞히기 쉬운 쪽으로 굽는다(origin=code 와 같은 이유).';
comment on column research.packet_claims.probability_method is
  'evidence/methods.py 의 방법 key. 확률의 산출 근거 - 없는 확률은 등재 불가.';
comment on column research.packet_claims.falsification_note is
  '무엇이 관측되면 이 주장이 틀린 것인가(사람이 읽는 한 줄). 반증 조건을 '
  '적어두지 않으면 사후에 해석이 늘어나 채점이 무의미해진다.';

-- ── 2) 방법론 귀속 ─────────────────────────────────────────────────────────
--   analyst_calibration 은 node x verdict 로만 집계한다. '어떤 방법이
--   기여했나'가 없으면 표본이 쌓여도 methods.py 의 validated 를 켤 근거가
--   안 생기고, 방법론 채택·폐기가 계속 취향으로 남는다.
alter table research.packet_claims
  add column if not exists method_key text;

comment on column research.packet_claims.method_key is
  'evidence/methods.py 의 방법 key. 이 주장이 어느 기법에서 나왔는지 - '
  '성과를 방법 단위로 귀속하려면 이것이 있어야 한다.';

-- ── 3) 주장 ↔ 근거 문서 인용 ───────────────────────────────────────────────
create table if not exists research.claim_evidence (
  claim_id uuid not null
    references research.packet_claims(claim_id) on delete cascade,
  document_id uuid not null references research.documents(document_id),
  evidence_role text not null,            -- SUPPORT / CONTEXT / COUNTER
  rank integer check (rank is null or rank > 0),
  -- 인용 시점의 가중치(뉴스 시간감쇠 등). 나중에 재계산하면 달라지므로
  -- **그때 값**을 박아둔다 - 재현의 기준이다.
  weight_at_claim numeric,
  created_at timestamptz not null default now(),
  primary key (claim_id, document_id, evidence_role)
);

comment on table research.claim_evidence is
  '주장이 어떤 문서에 근거했는지. research_packet_evidence 와 목적이 같지만 '
  '그쪽은 governance.cases FK 에 막혀 있어(0행) claim 단위로 먼저 남긴다. '
  'Case 발급이 되면 claim_id -> research_packet_id 매핑으로 합칠 수 있다.';

create index if not exists claim_evidence_document_idx
  on research.claim_evidence (document_id);

-- ── 4) 방법 단위 성과 뷰 ───────────────────────────────────────────────────
--   기존 analyst_calibration(분석가별) 옆에 방법별을 둔다. 둘은 다른 질문에
--   답한다: "이 분석가를 믿을 수 있나" vs "이 기법이 실제로 먹히나".
create or replace view research.method_calibration as
select
  c.method_key,
  c.kind,
  c.horizon_days,
  count(*)                                             as claims,
  count(*) filter (where o.status = 'EVALUATED')       as scored,
  -- 발동률: 채점된 것 중 실제로 조건이 성립한 비율
  round(avg((o.claims_triggered > 0)::int)
        filter (where o.status = 'EVALUATED')::numeric, 4) as trigger_rate,
  -- Brier: (확률 - 실제)^2 의 평균. 낮을수록 잘 맞춘 것이다.
  -- 확률이 없는 주장은 **제외**한다(0 으로 치면 완벽 예측으로 위장된다).
  round(avg(power(c.probability - (o.claims_triggered > 0)::int, 2))
        filter (where o.status = 'EVALUATED' and c.probability is not null)::numeric, 4)
                                                       as brier_score,
  count(*) filter (where c.probability is not null)    as with_probability
from research.packet_claims c
left join research.packet_outcomes o
  on o.trace_id = c.trace_id and o.horizon_days = c.horizon_days
where c.method_key is not null
group by 1, 2, 3;

comment on view research.method_calibration is
  '방법론 단위 성과. Brier 는 확률이 있는 주장만으로 계산한다 - 확률 없는 '
  '것을 0 으로 채우면 완벽 예측으로 위장된다.';

commit;
