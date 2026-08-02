begin;

-- 담당자: 재일 (리서치본부)
-- 근거: 재일님 지시 2026-08-02 "발생한 모든 문제들 해결해놔".
--
-- 문제: Packet 이 발행한 주장 중 REGIME_FLIP·GEO_ESCALATION 이 **채점되지
--       않는다.** packet_outcome_scorer 가 매번 "라벨 주장 N건은 일별 라벨
--       이력 미보관으로 채점 제외"를 남기고 있었다. 선순환의 구멍이다.
--
-- 왜 지금 채울 수 있나: 레짐·지정학 라벨은 **전부 코드가 결정론으로 만든다**
--       (compute_regime_readout / compute_geo_readout). LLM 은 서술만 한다.
--       따라서 매일 한 번 계산해 남기면 그게 곧 그날의 사실이고, 나중에
--       다시 계산해 채우는 사후 재구성과 다르다.
--
-- 규율: **사후 소급 기록 금지.** as_of 는 계산한 날이며 과거 날짜로 행을
--       만들지 않는다(그건 그때의 판정이 아니다). 계산 근거(readout)를 함께
--       남겨 나중에 "왜 그 라벨이었나"를 재검산할 수 있게 한다.

create table research.daily_labels (
  label_id uuid primary key default gen_random_uuid(),
  as_of date not null,                     -- 그날의 판정 (KST 기준 거래일)
  label_kind text not null
    check (label_kind in ('REGIME', 'GEOPOLITICAL')),
  label_value text not null,               -- BREADTH_THRUST / SHOCK 등
  driver text,                             -- 지정학 전용: THREAT_LED / ACT_LED
  readout jsonb not null default '{}'::jsonb,   -- 재검산용 결정론 근거
  computed_at timestamptz not null default now(),
  producer text not null,                  -- 어느 코드가 만들었는가
  -- 같은 날 같은 종류는 하나. 재실행은 갱신이지 중복이 아니다.
  unique (as_of, label_kind)
);

create index daily_labels_kind_time_idx
  on research.daily_labels (label_kind, as_of desc);

alter table research.daily_labels enable row level security;

commit;
