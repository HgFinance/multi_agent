begin;

-- 담당자: 재일 (리서치본부)
-- 근거: 재일님 지시 2026-08-01 "output 이 풍성하면서 실제 투자 판단 -> 결과에
--       영향을 줄 때 선순환이 이뤄지게 만들어야 한다".
--
-- 문제: 지금 Packet 은 만들어져 reports/*.md 로 남고 거기서 끝난다. 무엇이
--       맞았는지 아무도 세지 않으므로 다음 Packet 이 더 나아질 근거가 없다 -
--       선순환의 고리가 끊겨 있다.
--
-- 해법의 전제: **기계가 채점할 수 있는 주장만 채점된다.** LLM 이 쓴 산문
--       무효화 조건("연계성 약화 가능성")은 사람도 판정이 갈린다. 그래서
--       판정 가능한 주장(packet_claims)은 **코드가** 결정론으로 발행하고,
--       채점기(packet_outcome_scorer.py)가 시세로 대조해 결과를 남긴다.
--       LLM 산문은 그대로 두되 채점 대상이 아니다 - 서술과 판정의 분리라는
--       본부 원칙(라벨은 코드, 서술은 LLM)을 사후 검증까지 확장한 것이다.

-- 1) 발행: Packet 시점에 코드가 만든 반증 가능한 주장
create table research.packet_claims (
  claim_id uuid primary key default gen_random_uuid(),
  trace_id uuid not null,                  -- research.pipeline_runs.trace_id
  symbol text not null,
  created_at timestamptz not null default now(),
  kind text not null,                      -- PRICE_DRAWDOWN / PRICE_RALLY / REGIME_FLIP ...
  metric text not null,                    -- close / regime_label / geo_risk_label
  op text not null check (op in ('<=', '>=', '!=', '==')),
  threshold numeric,                       -- 수치 비교용
  threshold_text text,                      -- 라벨 비교용 (둘 중 하나만 채운다)
  baseline numeric,                        -- 기준값 (Packet 시점 종가 등)
  baseline_text text,
  horizon_days integer not null check (horizon_days > 0),
  source_node text,                        -- 어느 분석가 readout 에서 나왔는지
  origin text not null default 'code' check (origin in ('code')),
  -- 발행 주체를 code 로 못박는다: LLM 이 자기 채점 기준을 쓰게 두면
  -- 맞히기 쉬운 조건만 내놓는 쪽으로 굽는다(자기 채점 금지).
  check (threshold is not null or threshold_text is not null),
  unique (trace_id, kind, metric, horizon_days)
);

create index packet_claims_trace_idx on research.packet_claims (trace_id);
create index packet_claims_symbol_time_idx
  on research.packet_claims (symbol, created_at desc);

-- 2) 채점: 지평(horizon)이 지난 뒤 실제로 어떻게 됐는지
create table research.packet_outcomes (
  outcome_id uuid primary key default gen_random_uuid(),
  trace_id uuid not null,
  symbol text not null,
  packet_date date not null,               -- Packet 생성일(KST)
  horizon_days integer not null check (horizon_days > 0),
  evaluated_at timestamptz not null default now(),
  baseline_close numeric,                  -- Packet 시점 종가
  horizon_close numeric,                   -- 지평 시점 종가
  forward_return_pct numeric,              -- (horizon/baseline - 1) * 100
  trading_days_used integer,               -- 실제로 쓴 거래일 수(휴장 보정 확인용)
  claims_total integer not null default 0,
  claims_triggered integer not null default 0,
  triggered jsonb not null default '[]'::jsonb,   -- 발동한 주장 상세
  analyst_verdicts jsonb not null default '{}'::jsonb,  -- 그때의 분석가 판정 스냅샷
  status text not null check (status in ('EVALUATED', 'PENDING_DATA')),
  -- PENDING_DATA: 지평은 지났는데 시세가 아직 없다. 0 이나 추정으로 채우지
  -- 않는다 - 없는 결과를 만들어내면 통계 전체가 오염된다.
  note text,
  unique (trace_id, horizon_days)
);

create index packet_outcomes_symbol_date_idx
  on research.packet_outcomes (symbol, packet_date desc);
create index packet_outcomes_horizon_idx
  on research.packet_outcomes (horizon_days, status);

-- 3) 되먹임: 분석가 판정별 사후 성과 (선순환이 눈에 보이는 지점)
--    "이 분석가가 X 라고 했을 때 실제로 어떻게 됐나"를 누적한다. 표본이
--    쌓이기 전까지 이 뷰의 숫자로 가중치를 바꾸지 않는다 - 소표본 과적합은
--    되먹임이 아니라 잡음 증폭이다.
create view research.analyst_calibration as
select
  v.node,
  v.verdict,
  o.horizon_days,
  count(*)                                        as n,
  round(avg(o.forward_return_pct), 3)             as avg_forward_return_pct,
  round(percentile_cont(0.5) within group (order by o.forward_return_pct)::numeric, 3)
                                                  as median_forward_return_pct,
  round(100.0 * avg((o.forward_return_pct > 0)::int), 1) as pct_positive,
  round(avg(o.claims_triggered::numeric), 2)      as avg_claims_triggered
from research.packet_outcomes o
cross join lateral jsonb_each_text(o.analyst_verdicts) as v(node, verdict)
where o.status = 'EVALUATED' and o.forward_return_pct is not null
group by v.node, v.verdict, o.horizon_days;

alter table research.packet_claims enable row level security;
alter table research.packet_outcomes enable row level security;

commit;
