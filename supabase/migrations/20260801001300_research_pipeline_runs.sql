begin;

-- 담당자: 재일 (리서치본부)
-- 근거: 동규님 리스크본부의 결정론 리포트 패턴(2026-08-01)을 잇는 실행 기록층.
--       원래 audit.agent_runs 를 쓰려 했으나 workforce.agent_profiles FK 가
--       필수라 프로필 등재(HR·영주님 권한)가 선행돼야 한다 - 권한 경계를
--       넘지 않기 위해 우리 소유 research 스키마에 둔다. HR 등재 후
--       audit.agent_runs 로 승격하면 이 테이블은 그 사본/원장이 된다.
--
-- 목적: run_research_department 실행마다 무엇이(모델 버전) 무엇을(입력 해시)
--       무엇으로(Packet 해시·수치검사) 판단했는지 남긴다 - 재현성·QA 검토용.
--       Packet 본문은 reports/*.md 와 stdout 에 있고 여기는 좌표·지문만.

create table research.pipeline_runs (
  pipeline_run_id uuid primary key default gen_random_uuid(),
  trace_id uuid not null,
  pipeline_version text not null,
  symbol text not null,
  input_hash text not null,          -- sha256(symbol + 주요 설정) - 같은 입력 식별
  node_models jsonb not null,        -- 노드별 사용 모델 {sentiment: "...", ...}
  packet_hash text,                  -- sha256(packet json) - 산출물 지문
  evidence_quality text,
  numeric_check_ok boolean,
  status text not null check (status in ('COMPLETED', 'HALTED', 'FAILED')),
  halt_reason text,
  started_at timestamptz not null,
  ended_at timestamptz not null,
  report_path text,                  -- reports/*.md 상대 경로
  metadata jsonb not null default '{}'::jsonb,
  check (ended_at >= started_at)
);

create index pipeline_runs_symbol_time_idx
  on research.pipeline_runs (symbol, started_at desc);
create index pipeline_runs_trace_idx
  on research.pipeline_runs (trace_id);

alter table research.pipeline_runs enable row level security;

commit;
