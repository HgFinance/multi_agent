begin;

-- `execution.order_events`의 브로커 이벤트 중복 방지 제약을 고친다.
--
-- 소유: 도현 (트레이딩본부 — `execution` 스키마)
-- 근거: docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 4.3 (불변식 4:
--       "같은 broker event를 두 번 받아도 체결이 두 번 잡히지 않는다")
--       supabase/migrations/20260729000400_execution_risk_accounting.sql
--
-- ## 무엇이 잘못됐나
--
-- 원래 제약이 `unique nulls not distinct (broker_adapter, broker_event_id)`였다.
-- `NULLS NOT DISTINCT`는 NULL끼리도 같다고 보므로, **broker_event_id가 NULL인 행이
-- adapter당 표 전체에서 딱 하나만** 존재할 수 있다.
--
-- 그런데 주문 생애의 상당수 이벤트는 브로커가 준 것이 아니라 우리 내부 전이다:
--   order_created / submitted / cancel_requested / unknown
-- 전부 broker_event_id가 NULL이다. 즉 원래 제약대로면 **Event Store에 내부 전이를
-- 한 건밖에 못 넣는다.** 두 번째부터는 조용히 거부되고, 그러면 Event Store로
-- 상태를 재구축한다는 불변식 6이 성립하지 않는다.
--
-- 실제로 psycopg OrderStore를 붙이자마자 order_created 다음의 submitted가
-- 사라졌다(2026-08-04 확인).
--
-- ## 어떻게 고치나
--
-- 평범한 `unique (broker_adapter, broker_event_id)`로 바꾼다. 표준 SQL에서 NULL은
-- 서로 다르므로 내부 전이는 몇 건이든 들어가고, **브로커가 준 id는 여전히 한 번만**
-- 들어간다 - 원래 의도했던 것이 정확히 이거다.
--
-- 내부 전이의 중복 방지는 `unique (order_id, sequence)`가 이미 맡고 있다.
-- 같은 주문에 같은 순번이 두 번 생길 수 없다.

alter table execution.order_events
  drop constraint if exists order_events_broker_adapter_broker_event_id_key;

alter table execution.order_events
  add constraint order_events_broker_event_unique
  unique (broker_adapter, broker_event_id);

commit;
