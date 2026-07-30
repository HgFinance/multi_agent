-- Sprint D0: execution 스키마
-- 소유: 도현 (트레이딩본부)
-- 근거: docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 4.2, 4.3
--       docs/HEDGE_FUND_MASTER_PLAN.md 5.7(Fund/Book 계층), 12(OMS)
--
-- 원칙:
--   - 수량·가격은 절대 float를 쓰지 않는다. numeric만 사용한다.
--   - order_events는 append-only. orders.state는 이벤트에서 재구축 가능한 projection이다.
--   - 허용되지 않은 상태 전이는 DB가 거부한다. 애플리케이션 코드를 신뢰하지 않는다.

CREATE SCHEMA IF NOT EXISTS execution;

-- ---------------------------------------------------------------------------
-- Fund / Book / Strategy 계층
-- 초기에는 Fund 1개, Pod 1개로 운영하지만 마스터플랜 5.7에 따라 데이터 모델은
-- 처음부터 다중 Fund·Book·Strategy를 지원한다. 나중에 컬럼을 추가하는 것보다
-- 지금 넣고 값 하나만 쓰는 편이 싸다.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS execution.funds (
    fund_id         uuid PRIMARY KEY,
    name            text        NOT NULL,
    base_currency   char(3)     NOT NULL DEFAULT 'KRW',
    inception_date  date        NOT NULL,
    status          text        NOT NULL DEFAULT 'active',
    created_at      timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT funds_status_chk CHECK (status IN ('active', 'closed', 'suspended'))
);

CREATE TABLE IF NOT EXISTS execution.books (
    book_id     uuid PRIMARY KEY,
    fund_id     uuid        NOT NULL REFERENCES execution.funds(fund_id),
    pod_id      uuid        NOT NULL,
    name        text        NOT NULL,
    book_type   text        NOT NULL,
    status      text        NOT NULL DEFAULT 'active',
    created_at  timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT books_type_chk CHECK (book_type IN ('strategy', 'hedge', 'overlay'))
);

-- ---------------------------------------------------------------------------
-- Trade Case: Research Packet + 승인된 Strategy Signal이 하나의 거래 검토 단위로 묶인 것
-- 트레이딩본부는 시그널을 만들지 않는다. research_packet_id와 signal_id는 외부
-- (research-api, strategy-registry-api)에서 받은 참조값이므로 FK를 걸지 않는다.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS execution.trade_cases (
    trade_case_id      uuid PRIMARY KEY,
    fund_id            uuid        NOT NULL REFERENCES execution.funds(fund_id),
    book_id            uuid        NOT NULL REFERENCES execution.books(book_id),
    strategy_id        uuid        NOT NULL,
    strategy_version   text        NOT NULL,
    instrument_id      uuid        NOT NULL,
    research_packet_id uuid,
    signal_id          uuid,
    case_status        text        NOT NULL DEFAULT 'open',
    thesis             jsonb       NOT NULL DEFAULT '[]'::jsonb,
    counter_thesis     jsonb       NOT NULL DEFAULT '[]'::jsonb,
    invalidation       jsonb       NOT NULL DEFAULT '[]'::jsonb,
    expires_at         timestamptz NOT NULL,
    created_by         text        NOT NULL,
    trace_id           text        NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT trade_cases_status_chk
        CHECK (case_status IN ('open', 'proposed', 'executing', 'closed', 'expired', 'abandoned'))
);

CREATE INDEX IF NOT EXISTS trade_cases_book_idx ON execution.trade_cases (book_id, created_at DESC);
CREATE INDEX IF NOT EXISTS trade_cases_open_idx ON execution.trade_cases (expires_at)
    WHERE case_status IN ('open', 'proposed');

-- ---------------------------------------------------------------------------
-- Order Intent: Agent가 만들 수 있는 유일한 산출물.
-- Agent는 여기까지만 제안하고 Broker를 직접 호출하지 않는다 (팀 가이드 2장 원칙 1).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS execution.order_intents (
    order_intent_id    uuid PRIMARY KEY,
    trade_case_id      uuid        NOT NULL REFERENCES execution.trade_cases(trade_case_id),
    instrument_id      uuid        NOT NULL,
    side               text        NOT NULL,
    order_type         text        NOT NULL,
    quantity           numeric(20, 4) NOT NULL,
    limit_price        numeric(20, 4),
    time_in_force      text        NOT NULL DEFAULT 'DAY',
    valid_until        timestamptz NOT NULL,

    -- 주문 시점의 시장 상태를 Evidence로 고정한다. Tick 전체를 복제하지 않고
    -- 재현에 필요한 값만 박제한다 (팀 가이드 3.1).
    market_snapshot_id text        NOT NULL,
    snapshot_as_of     timestamptz NOT NULL,
    snapshot_bid       numeric(20, 4),
    snapshot_ask       numeric(20, 4),
    snapshot_spread_bps numeric(10, 2),
    snapshot_quality   text        NOT NULL DEFAULT 'ok',

    risk_request_id    uuid,
    intent_status      text        NOT NULL DEFAULT 'DRAFT',
    idempotency_key    text        NOT NULL UNIQUE,
    schema_version     text        NOT NULL,
    created_by         text        NOT NULL,
    trace_id           text        NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT order_intents_side_chk CHECK (side IN ('BUY', 'SELL')),
    CONSTRAINT order_intents_type_chk CHECK (order_type IN ('LIMIT', 'MARKET')),
    CONSTRAINT order_intents_tif_chk  CHECK (time_in_force IN ('DAY', 'IOC', 'FOK')),
    CONSTRAINT order_intents_quality_chk CHECK (snapshot_quality IN ('ok', 'stale', 'wide', 'suspect')),

    -- 수량은 양수여야 한다. 방향은 side가 정한다.
    CONSTRAINT order_intents_qty_chk CHECK (quantity > 0),
    -- LIMIT 주문에 가격이 없으면 주문이 아니다.
    CONSTRAINT order_intents_limit_price_chk
        CHECK (order_type <> 'LIMIT' OR limit_price IS NOT NULL),
    CONSTRAINT order_intents_price_positive_chk
        CHECK (limit_price IS NULL OR limit_price > 0)
);

CREATE INDEX IF NOT EXISTS order_intents_case_idx ON execution.order_intents (trade_case_id);
CREATE INDEX IF NOT EXISTS order_intents_pending_idx ON execution.order_intents (valid_until)
    WHERE intent_status IN ('DRAFT', 'RISK_PENDING');

-- ---------------------------------------------------------------------------
-- OMS 상태 머신 (팀 가이드 4.3)
-- 전이 규칙을 트리거 코드가 아니라 참조 테이블 + FK로 강제한다.
-- 코드로 검사하면 우회 경로가 생기지만, FK는 우회할 수 없다.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS execution.order_state_transitions (
    from_state text NOT NULL,
    to_state   text NOT NULL,
    PRIMARY KEY (from_state, to_state)
);

INSERT INTO execution.order_state_transitions (from_state, to_state) VALUES
    ('DRAFT',            'RISK_PENDING'),
    ('RISK_PENDING',     'APPROVED'),
    ('RISK_PENDING',     'RESIZED'),      -- 리스크본부의 수량 축소
    ('RISK_PENDING',     'REJECTED'),
    ('APPROVED',         'READY_TO_SUBMIT'),
    ('RESIZED',          'READY_TO_SUBMIT'),
    ('READY_TO_SUBMIT',  'SUBMITTED'),
    ('READY_TO_SUBMIT',  'EXPIRED'),      -- Risk 승인 만료 후 미전송
    ('SUBMITTED',        'ACKNOWLEDGED'),
    ('SUBMITTED',        'REJECTED'),     -- Broker 거부
    ('SUBMITTED',        'UNKNOWN'),      -- 응답 없음. 추정 금지 (팀 가이드 2장 원칙 3)
    ('ACKNOWLEDGED',     'PARTIALLY_FILLED'),
    ('ACKNOWLEDGED',     'FILLED'),
    ('ACKNOWLEDGED',     'CANCELLED'),
    ('ACKNOWLEDGED',     'EXPIRED'),
    ('ACKNOWLEDGED',     'UNKNOWN'),
    ('PARTIALLY_FILLED', 'PARTIALLY_FILLED'),
    ('PARTIALLY_FILLED', 'FILLED'),
    ('PARTIALLY_FILLED', 'CANCELLED'),
    ('PARTIALLY_FILLED', 'EXPIRED'),
    ('PARTIALLY_FILLED', 'UNKNOWN'),
    ('UNKNOWN',          'ACKNOWLEDGED'), -- 조회로 상태 복구
    ('UNKNOWN',          'PARTIALLY_FILLED'),
    ('UNKNOWN',          'FILLED'),
    ('UNKNOWN',          'CANCELLED'),
    ('UNKNOWN',          'REJECTED')
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- Orders: Risk 승인을 통과한 주문만 존재한다.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS execution.orders (
    order_id           uuid PRIMARY KEY,
    order_intent_id    uuid        NOT NULL UNIQUE REFERENCES execution.order_intents(order_intent_id),
    client_order_id    text        NOT NULL UNIQUE,
    broker_order_id    text,
    broker_adapter     text        NOT NULL,
    state              text        NOT NULL DEFAULT 'DRAFT',

    -- Risk 승인 없이 SUBMITTED로 갈 수 없다 (팀 가이드 2장 원칙 2, DoD 2번).
    risk_decision_id   uuid,
    risk_approved_qty  numeric(20, 4),
    risk_decision_expires_at timestamptz,

    requested_quantity numeric(20, 4) NOT NULL,
    filled_quantity    numeric(20, 4) NOT NULL DEFAULT 0,
    average_fill_price numeric(20, 4),
    submitted_at       timestamptz,
    last_event_at      timestamptz NOT NULL DEFAULT now(),
    version            integer     NOT NULL DEFAULT 0,

    CONSTRAINT orders_qty_chk CHECK (requested_quantity > 0),
    -- 체결 수량이 주문 수량을 넘을 수 없다 (팀 가이드 4.3).
    CONSTRAINT orders_fill_chk CHECK (filled_quantity >= 0 AND filled_quantity <= requested_quantity),
    -- 전송된 주문에는 반드시 Risk Decision이 붙어 있어야 한다.
    CONSTRAINT orders_risk_required_chk CHECK (
        state IN ('DRAFT', 'RISK_PENDING', 'REJECTED') OR risk_decision_id IS NOT NULL
    ),
    -- 승인 수량을 초과해 주문할 수 없다.
    CONSTRAINT orders_risk_qty_chk CHECK (
        risk_approved_qty IS NULL OR requested_quantity <= risk_approved_qty
    )
);

CREATE INDEX IF NOT EXISTS orders_open_idx ON execution.orders (state, last_event_at)
    WHERE state NOT IN ('FILLED', 'CANCELLED', 'REJECTED', 'EXPIRED');

-- ---------------------------------------------------------------------------
-- Order Events: append-only. orders.state는 여기서 재구축 가능해야 한다.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS execution.order_events (
    order_event_id  uuid PRIMARY KEY,
    order_id        uuid        NOT NULL REFERENCES execution.orders(order_id),
    event_type      text        NOT NULL,
    sequence        bigint      NOT NULL,

    -- 세 시각을 분리해 기록한다 (팀 가이드 8.1). 브로커 시각과 우리 수신 시각이
    -- 다르고, 순서가 뒤바뀌어 도착할 수 있다.
    event_time      timestamptz NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    processed_at    timestamptz,

    broker_adapter  text        NOT NULL,
    broker_event_id text,
    from_state      text        NOT NULL,
    to_state        text        NOT NULL,
    payload         jsonb       NOT NULL,
    payload_hash    text        NOT NULL,
    trace_id        text        NOT NULL,

    -- 허용된 전이가 아니면 INSERT 자체가 실패한다.
    FOREIGN KEY (from_state, to_state)
        REFERENCES execution.order_state_transitions (from_state, to_state),
    -- 같은 브로커 이벤트를 두 번 받아도 한 번만 기록된다 (멱등성).
    UNIQUE (broker_adapter, broker_event_id),
    UNIQUE (order_id, sequence)
);

CREATE INDEX IF NOT EXISTS order_events_order_idx ON execution.order_events (order_id, sequence);

-- order_events는 수정·삭제하지 않는다. append만 허용한다.
CREATE OR REPLACE RULE order_events_no_update AS ON UPDATE TO execution.order_events DO INSTEAD NOTHING;
CREATE OR REPLACE RULE order_events_no_delete AS ON DELETE TO execution.order_events DO INSTEAD NOTHING;

-- ---------------------------------------------------------------------------
-- Execution Plan / Fills / TCA / 예외
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS execution.execution_plans (
    execution_plan_id      uuid PRIMARY KEY,
    order_id               uuid        NOT NULL REFERENCES execution.orders(order_id),
    philosophy             text        NOT NULL,   -- departments/02-trading/contracts/philosophies.yaml 키
    urgency                text        NOT NULL,
    slices                 integer     NOT NULL,
    limit_offset_bps       numeric(10, 2) NOT NULL,
    max_participation_rate numeric(6, 4) NOT NULL,
    slippage_budget_bps    numeric(10, 2) NOT NULL,
    cancel_after_min       integer     NOT NULL,
    expected_slippage_bps  numeric(10, 2),
    created_at             timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT execution_plans_slices_chk CHECK (slices > 0),
    CONSTRAINT execution_plans_participation_chk
        CHECK (max_participation_rate > 0 AND max_participation_rate <= 1)
);

CREATE TABLE IF NOT EXISTS execution.fills (
    fill_id         uuid PRIMARY KEY,
    order_id        uuid        NOT NULL REFERENCES execution.orders(order_id),
    order_event_id  uuid        NOT NULL REFERENCES execution.order_events(order_event_id),
    broker_fill_id  text,
    broker_adapter  text        NOT NULL,
    quantity        numeric(20, 4) NOT NULL,
    price           numeric(20, 4) NOT NULL,
    fee             numeric(20, 4) NOT NULL DEFAULT 0,
    tax             numeric(20, 4) NOT NULL DEFAULT 0,
    liquidity_flag  text,
    event_time      timestamptz NOT NULL,
    received_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fills_qty_chk CHECK (quantity > 0),
    CONSTRAINT fills_price_chk CHECK (price > 0),
    UNIQUE (broker_adapter, broker_fill_id)
);

CREATE INDEX IF NOT EXISTS fills_order_idx ON execution.fills (order_id, event_time);

CREATE TABLE IF NOT EXISTS execution.broker_sessions (
    session_id     uuid PRIMARY KEY,
    broker_adapter text        NOT NULL,
    state          text        NOT NULL,
    connected_at   timestamptz,
    last_heartbeat timestamptz,
    safe_state     boolean     NOT NULL DEFAULT false,
    detail         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    updated_at     timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS execution.tca_results (
    tca_id            uuid PRIMARY KEY,
    order_id          uuid        NOT NULL REFERENCES execution.orders(order_id),
    arrival_price     numeric(20, 4),
    mid_price         numeric(20, 4),
    vwap              numeric(20, 4),
    slippage_bps      numeric(10, 2),
    market_impact_bps numeric(10, 2),
    total_cost        numeric(20, 4),
    computed_at       timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS execution.execution_exceptions (
    exception_id   uuid PRIMARY KEY,
    order_id       uuid REFERENCES execution.orders(order_id),
    exception_type text        NOT NULL,
    severity       text        NOT NULL,
    detail         jsonb       NOT NULL DEFAULT '{}'::jsonb,
    status         text        NOT NULL DEFAULT 'open',
    detected_at    timestamptz NOT NULL DEFAULT now(),
    resolved_at    timestamptz,
    CONSTRAINT execution_exceptions_type_chk CHECK (
        exception_type IN ('reject', 'stuck_order', 'cancel_mismatch', 'unknown_state', 'duplicate_command')
    )
);
