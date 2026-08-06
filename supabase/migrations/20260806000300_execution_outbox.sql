begin;
-- Migration sequence is after the already-applied 20260804001200 revision.

-- Transactional Outbox — 트레이딩 OMS 상태 변경을 부서 밖으로 내보내는 유일한 경로
--
-- 소유: 도현 (트레이딩본부)
-- 근거: docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md (Override v2.0) P0-2
--         "Transactional Outbox와 Relay, consumer idempotency를 PLAT-03 계약에 맞춰 구현한다"
--       docs/02-engineering/UNIFIED_DOMAIN_API_SPEC.md 6.1 event-envelope-v1
--         "Event Bus는 at-least-once를 전제로 하며 Outbox 또는 동등한 재시도 가능한 발행,
--          DLQ, 원인 코드를 사용한다"
--
-- **왜 별도 테이블인가.** execution.order_events 는 우리 상태 재구축용 Event Store 이고
-- 발행 상태(PENDING/SENT/DLQ)·재시도 횟수·backoff 를 갖지 않는다. 거기에 발행 컬럼을
-- 붙이면 상태 재구축 테이블이 전송 큐를 겸하게 되고, 재구축 Replay 가 전송 부작용을
-- 일으킬 수 있다. 두 관심사를 분리한다.
--
-- **핵심 불변식**: outbox row 는 상태 변경과 **같은 트랜잭션**에서 들어간다.
-- 상태가 롤백되면 outbox 도 같이 사라져야 하며, 이는 store_postgres 자체 점검이 검사한다.

create table if not exists execution.outbox (
  -- 발행 순서 = 삽입 순서. Relay 가 이 순서로 집는다.
  outbox_id       bigserial primary key,
  -- event-envelope-v1 필드. 소비자는 이 봉투만 보고 판단한다.
  event_id        uuid not null unique,
  event_type      text not null
                  check (event_type ~ '^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+\.v[0-9]+$'),
  schema_version  text not null default 'event-envelope-v1'
                  check (schema_version = 'event-envelope-v1'),
  case_id         uuid,
  trace_id        uuid not null,
  producer        text not null check (length(producer) > 0),
  occurred_at     timestamptz not null,
  -- 같은 사실을 두 번 넣지 못한다. 생산자 쪽 중복 방지선.
  idempotency_key text not null unique,
  payload_ref     jsonb,

  -- 발행 상태. DLQ 는 조용히 버리는 자리가 아니라 원인을 남기는 자리다.
  status          text not null default 'PENDING'
                  check (status in ('PENDING', 'SENT', 'FAILED', 'DLQ')),
  attempts        integer not null default 0 check (attempts >= 0),
  last_error      text,
  -- 지수 backoff. Relay 는 available_at 이 지난 것만 집는다.
  available_at    timestamptz not null default now(),
  sent_at         timestamptz,
  created_at      timestamptz not null default now(),

  -- SENT 인데 시각이 없거나, 안 보냈는데 시각이 있으면 발행 여부를 신뢰할 수 없다.
  constraint outbox_sent_at_matches_status
    check ((status = 'SENT') = (sent_at is not null))
);

-- Relay 가 집는 조건 그대로. status/available_at 으로 좁히고 outbox_id 순서로 읽는다.
create index if not exists outbox_pending_idx
  on execution.outbox (status, available_at, outbox_id)
  where status in ('PENDING', 'FAILED');

create index if not exists outbox_trace_idx on execution.outbox (trace_id);
create index if not exists outbox_case_idx on execution.outbox (case_id)
  where case_id is not null;

comment on table execution.outbox is
  'Transactional Outbox. OMS 상태 변경과 같은 트랜잭션에서 기록되고 Relay 가 at-least-once 로 발행한다.';

-- 소비자 중복 제거. at-least-once 전제이므로 소비자가 같은 event_id 를 여러 번 본다.
-- **소비자별로** 기록한다 - 회계가 처리했다고 QA 가 건너뛰면 안 된다.
create table if not exists execution.outbox_consumed (
  consumer     text not null check (length(consumer) > 0),
  event_id     uuid not null references execution.outbox (event_id),
  processed_at timestamptz not null default now(),
  primary key (consumer, event_id)
);

comment on table execution.outbox_consumed is
  'Consumer idempotency. at-least-once 발행에서 소비자가 같은 event_id 를 두 번 처리하지 않게 한다.';

alter table execution.outbox enable row level security;
alter table execution.outbox_consumed enable row level security;

commit;
