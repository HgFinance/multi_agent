begin;

-- 2026-08-10: workforce.hiring_requests 에 requested_by/decided_by/decided_at/
-- decision_reason 을 추가한다. 최초 DDL(20260729000200)에는 없었는데,
-- "요청자와 승인자가 달라야 한다"(마스터플랜 4.3절 자기승인 금지)를 강제하려면
-- 누가 요청했고 누가 결정했는지가 스키마에 있어야 한다 - workforce.access_requests
-- 가 이미 requested_by/approvals 를 갖는 것과 같은 이유다. 이 테이블에 지금까지
-- 쓰기 코드가 전혀 없어 0건이므로(hiring/hiring_request.py 가 최초 writer) 기본값
-- 없이 requested_by 를 not null 로 추가해도 안전하다.

alter table workforce.hiring_requests
  add column if not exists requested_by text not null,
  add column if not exists decided_by text,
  add column if not exists decided_at timestamptz,
  add column if not exists decision_reason text;

commit;
