begin;

-- 가설 계보 - 이 가설이 어느 기획안·어느 리드에서 왔는가
--
-- 담당: 재일 (퀀트·백테스트본부 QNT)
-- 계약: departments/04-quant-backtest/contracts/quant_v2.py (HypothesisOrigin)
--       departments/01-research/contracts/factory_contracts.py (ExperimentProposalV1)
--
-- ▶ 무엇이 문제였나
--   `HypothesisOrigin(research_packet_ids, claim_ids)` 는 Pydantic 계약에만 있고
--   **DB 컬럼도, 채우는 코드도 없었다**(2026-08-06 감사 실측: 계약 인스턴스화 0건,
--   INSERT 3곳 모두 8컬럼만 기록). 그래서 실험 카드에서 원 기획안까지 역추적이
--   끊겨 있었고, 기각 교훈이 어느 아이디어의 것인지 알 수 없었다.
--
--   계약만 있고 컬럼이 없는 상태가 이 저장소의 지배적 결함 유형이다 - 계약은
--   앞서 있는데 실행부가 안 따라간다. 여기서 그 한 건을 닫는다.
--
-- ▶ 왜 경제적 근거·경쟁 설명까지 옮기나
--   기획안은 research 스키마에 있고 퀀트 Agent 는 그 스키마를 조회하지 않는다.
--   실험 카드를 만들 때마다 조인하러 가면 결합이 생기므로, **사전등록 시점의 값을
--   그대로 복사해 둔다** - 기획안이 나중에 수정돼도 이 실험이 무엇을 등록했는지는
--   변하지 않아야 한다(사전등록의 의미가 그것이다).


alter table quant.hypotheses
  -- 계보: 어느 기획안에서 왔는가. 자체 생성 가설이면 빈 값(전환기 한정).
  add column if not exists proposal_id           text,
  add column if not exists lead_ids              text[] not null default '{}',
  add column if not exists research_packet_ids   text[] not null default '{}',
  add column if not exists claim_ids             text[] not null default '{}',
  -- 사전등록 시점의 값 사본. 기획안이 수정돼도 이 실험이 등록한 내용은 안 변한다.
  add column if not exists economic_rationale    text,
  add column if not exists counterparty          text,
  add column if not exists competing_explanation text,
  add column if not exists competing_explanation_codes text[] not null default '{}',
  add column if not exists skeptic_sign          text,
  -- 소스가 보고한 값. **우리 실험 결과와 다른 열이다** - 남의 시장 숫자가 우리
  -- 결과처럼 읽히면 미검증 값이 근거로 승격된다.
  add column if not exists source_reported_effect jsonb not null default '{}'::jsonb;

comment on column quant.hypotheses.proposal_id is
  '이 가설을 낳은 리서치 기획안. 빈 값이면 퀀트 자체 생성(전환기 한정) - '
  '공장이 정상 가동되면 모든 가설에 값이 있어야 한다.';
comment on column quant.hypotheses.source_reported_effect is
  '소스(논문·서한)가 보고한 효과. 우리 백테스트 결과가 아니다. '
  '결과 해석 시 대조 기준으로만 쓰고 같은 지표처럼 제시하지 않는다.';

-- 기획안에서 가설을 역추적하는 경로(계보 조회).
create index if not exists idx_quant_hypotheses_proposal
  on quant.hypotheses (proposal_id) where proposal_id is not null;

-- ▶ 기획안에서 온 가설은 근거를 **반쪽으로 두지 않는다.**
--   proposal_id 가 있는데 경제적 근거나 회의론자 서명이 비어 있으면, 그 가설은
--   발행 게이트를 통과하지 않은 경로로 들어온 것이다. 한쪽만 채우는 것을 막는다.
alter table quant.hypotheses drop constraint if exists chk_quant_hypotheses_lineage_pair;
alter table quant.hypotheses add constraint chk_quant_hypotheses_lineage_pair check (
    proposal_id is null
    or (coalesce(btrim(economic_rationale), '') <> ''
        and coalesce(btrim(counterparty), '') <> ''
        and coalesce(btrim(skeptic_sign), '') <> ''
        and array_length(competing_explanation_codes, 1) >= 1)
);

commit;
