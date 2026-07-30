-- Sprint D0: accounting 스키마
-- 소유: 도현 (회계/포트폴리오본부)
-- 근거: docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 4.4, 8.2
--       docs/HEDGE_FUND_MASTER_PLAN.md 12.3(Fund Ledger), 12.4(NAV Close)
--
-- 원칙:
--   - Posted Journal은 수정·삭제하지 않는다. 반대 분개(Reversal)를 추가한다.
--   - positions와 cash_balances는 projection이다. journal에서 재구축 가능해야 한다.
--   - 회계 수치는 LLM 문장이 아니라 체결·원장 이벤트에서만 나온다.

CREATE SCHEMA IF NOT EXISTS accounting;

-- ---------------------------------------------------------------------------
-- 계정과목과 자본 배분
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS accounting.ledger_accounts (
    account_id   uuid PRIMARY KEY,
    account_code text        NOT NULL UNIQUE,
    name         text        NOT NULL,
    account_type text        NOT NULL,
    currency     char(3)     NOT NULL DEFAULT 'KRW',
    parent_id    uuid        REFERENCES accounting.ledger_accounts(account_id),
    created_at   timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ledger_accounts_type_chk
        CHECK (account_type IN ('asset', 'liability', 'equity', 'income', 'expense'))
);

CREATE TABLE IF NOT EXISTS accounting.strategy_allocations (
    allocation_id  uuid PRIMARY KEY,
    book_id        uuid        NOT NULL REFERENCES execution.books(book_id),
    strategy_id    uuid        NOT NULL,
    capital_limit  numeric(20, 4) NOT NULL,
    effective_from timestamptz NOT NULL,
    effective_to   timestamptz,
    CONSTRAINT strategy_allocations_limit_chk CHECK (capital_limit >= 0),
    CONSTRAINT strategy_allocations_period_chk
        CHECK (effective_to IS NULL OR effective_to > effective_from)
);

-- ---------------------------------------------------------------------------
-- 이중분개 원장
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS accounting.journals (
    journal_id         uuid PRIMARY KEY,
    fund_id            uuid        NOT NULL REFERENCES execution.funds(fund_id),
    book_id            uuid        NOT NULL REFERENCES execution.books(book_id),
    event_type         text        NOT NULL,
    source_event_id    text        NOT NULL,
    effective_at       timestamptz NOT NULL,
    accounting_date    date        NOT NULL,
    currency           char(3)     NOT NULL DEFAULT 'KRW',
    status             text        NOT NULL DEFAULT 'posted',
    reversal_of        uuid        REFERENCES accounting.journals(journal_id),
    created_by_service text        NOT NULL,
    approved_by        text,
    trace_id           text        NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),

    -- 같은 체결 이벤트로 분개가 두 번 생기지 않는다 (멱등성).
    UNIQUE (event_type, source_event_id),
    CONSTRAINT journals_status_chk CHECK (status IN ('posted', 'reversed')),
    -- 자기 자신을 반대분개할 수 없다.
    CONSTRAINT journals_reversal_chk CHECK (reversal_of IS NULL OR reversal_of <> journal_id)
);

CREATE INDEX IF NOT EXISTS journals_date_idx ON accounting.journals (fund_id, accounting_date);

CREATE TABLE IF NOT EXISTS accounting.journal_lines (
    journal_line_id uuid PRIMARY KEY,
    journal_id      uuid        NOT NULL REFERENCES accounting.journals(journal_id),
    account_id      uuid        NOT NULL REFERENCES accounting.ledger_accounts(account_id),
    instrument_id   uuid,
    debit           numeric(20, 4) NOT NULL DEFAULT 0,
    credit          numeric(20, 4) NOT NULL DEFAULT 0,
    quantity        numeric(20, 4),
    unit_price      numeric(20, 4),
    currency        char(3)     NOT NULL DEFAULT 'KRW',
    fx_rate         numeric(20, 8),
    metadata        jsonb       NOT NULL DEFAULT '{}'::jsonb,

    CONSTRAINT journal_lines_sign_chk CHECK (debit >= 0 AND credit >= 0),
    -- 한 줄은 차변이거나 대변이다. 둘 다이거나 둘 다 0일 수 없다.
    CONSTRAINT journal_lines_side_chk CHECK ((debit > 0) <> (credit > 0))
);

CREATE INDEX IF NOT EXISTS journal_lines_journal_idx ON accounting.journal_lines (journal_id);

-- Posted Journal은 수정·삭제 금지. Reversal Journal로만 정정한다 (팀 가이드 8.2).
CREATE OR REPLACE RULE journals_no_delete AS ON DELETE TO accounting.journals DO INSTEAD NOTHING;
CREATE OR REPLACE RULE journal_lines_no_update AS ON UPDATE TO accounting.journal_lines DO INSTEAD NOTHING;
CREATE OR REPLACE RULE journal_lines_no_delete AS ON DELETE TO accounting.journal_lines DO INSTEAD NOTHING;

-- 차변 합계 = 대변 합계. 이 불변식이 깨지면 원장이 아니다.
-- 라인 단위 CHECK로는 표현할 수 없어 트리거로 검증한다. 지연 제약(DEFERRABLE)이라
-- 트랜잭션 커밋 시점에 검사하므로 라인을 여러 번 나눠 INSERT해도 된다.
CREATE OR REPLACE FUNCTION accounting.assert_journal_balanced() RETURNS trigger AS $$
DECLARE
    d numeric(20, 4);
    c numeric(20, 4);
    n integer;
BEGIN
    SELECT COALESCE(sum(debit), 0), COALESCE(sum(credit), 0), count(*)
      INTO d, c, n
      FROM accounting.journal_lines
     WHERE journal_id = COALESCE(NEW.journal_id, OLD.journal_id);

    IF n = 0 THEN
        RAISE EXCEPTION 'journal % 에 분개 라인이 없습니다', COALESCE(NEW.journal_id, OLD.journal_id);
    END IF;
    IF d <> c THEN
        RAISE EXCEPTION 'journal % 불균형: 차변 % <> 대변 %',
            COALESCE(NEW.journal_id, OLD.journal_id), d, c;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS journal_lines_balanced ON accounting.journal_lines;
CREATE CONSTRAINT TRIGGER journal_lines_balanced
    AFTER INSERT ON accounting.journal_lines
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION accounting.assert_journal_balanced();

-- ---------------------------------------------------------------------------
-- Projection: Position / Cash
-- 공식 상태이지만 source of truth는 아니다. journal에서 재계산할 수 있어야 한다.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS accounting.positions (
    position_id     uuid PRIMARY KEY,
    fund_id         uuid        NOT NULL REFERENCES execution.funds(fund_id),
    book_id         uuid        NOT NULL REFERENCES execution.books(book_id),
    strategy_id     uuid        NOT NULL,
    instrument_id   uuid        NOT NULL,
    quantity        numeric(20, 4) NOT NULL DEFAULT 0,
    average_cost    numeric(20, 4) NOT NULL DEFAULT 0,
    last_journal_id uuid        REFERENCES accounting.journals(journal_id),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fund_id, book_id, strategy_id, instrument_id)
);

CREATE TABLE IF NOT EXISTS accounting.cash_balances (
    cash_balance_id uuid PRIMARY KEY,
    fund_id         uuid        NOT NULL REFERENCES execution.funds(fund_id),
    book_id         uuid        REFERENCES execution.books(book_id),
    account_id      uuid        NOT NULL REFERENCES accounting.ledger_accounts(account_id),
    currency        char(3)     NOT NULL DEFAULT 'KRW',
    balance         numeric(20, 4) NOT NULL DEFAULT 0,
    last_journal_id uuid        REFERENCES accounting.journals(journal_id),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fund_id, book_id, account_id, currency)
);

-- ---------------------------------------------------------------------------
-- Valuation / PnL / NAV
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS accounting.valuations (
    valuation_id   uuid PRIMARY KEY,
    fund_id        uuid        NOT NULL REFERENCES execution.funds(fund_id),
    instrument_id  uuid        NOT NULL,
    valuation_date date        NOT NULL,
    price          numeric(20, 4) NOT NULL,
    -- 가격의 출처와 품질을 항상 남긴다. NAV Evidence다 (팀 가이드 8.2).
    price_source   text        NOT NULL,
    price_time     timestamptz NOT NULL,
    data_quality   text        NOT NULL DEFAULT 'ok',
    fx_rate        numeric(20, 8) NOT NULL DEFAULT 1,
    fx_source      text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fund_id, instrument_id, valuation_date, price_source),
    CONSTRAINT valuations_dq_chk CHECK (data_quality IN ('ok', 'stale', 'estimated', 'suspect'))
);

CREATE TABLE IF NOT EXISTS accounting.pnl_snapshots (
    pnl_snapshot_id uuid PRIMARY KEY,
    fund_id         uuid        NOT NULL REFERENCES execution.funds(fund_id),
    book_id         uuid        REFERENCES execution.books(book_id),
    strategy_id     uuid,
    as_of_date      date        NOT NULL,
    realized_pnl    numeric(20, 4) NOT NULL DEFAULT 0,
    unrealized_pnl  numeric(20, 4) NOT NULL DEFAULT 0,
    fee_pnl         numeric(20, 4) NOT NULL DEFAULT 0,
    fx_pnl          numeric(20, 4) NOT NULL DEFAULT 0,
    created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS accounting.nav_runs (
    nav_run_id     uuid PRIMARY KEY,
    fund_id        uuid        NOT NULL REFERENCES execution.funds(fund_id),
    valuation_date date        NOT NULL,
    -- preliminary와 official을 분리하고 승인 근거를 남긴다 (팀 가이드 10장).
    nav_type       text        NOT NULL,
    total_nav      numeric(20, 4) NOT NULL,
    status         text        NOT NULL DEFAULT 'draft',
    approved_by    text,
    approved_at    timestamptz,
    evidence_path  text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (fund_id, valuation_date, nav_type),
    CONSTRAINT nav_runs_type_chk CHECK (nav_type IN ('preliminary', 'official')),
    CONSTRAINT nav_runs_status_chk CHECK (status IN ('draft', 'approved', 'rejected')),
    -- official NAV는 승인자 없이 존재할 수 없다 (마스터플랜 5.6, 19.2).
    CONSTRAINT nav_runs_official_approval_chk CHECK (
        nav_type <> 'official' OR status <> 'approved' OR approved_by IS NOT NULL
    )
);

CREATE TABLE IF NOT EXISTS accounting.nav_components (
    nav_component_id uuid PRIMARY KEY,
    nav_run_id       uuid        NOT NULL REFERENCES accounting.nav_runs(nav_run_id),
    component_type   text        NOT NULL,
    amount           numeric(20, 4) NOT NULL,
    detail           jsonb       NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT nav_components_type_chk
        CHECK (component_type IN ('cash', 'position', 'accrual', 'fee', 'adjustment'))
);

CREATE TABLE IF NOT EXISTS accounting.performance_attribution (
    attribution_id uuid PRIMARY KEY,
    fund_id        uuid        NOT NULL REFERENCES execution.funds(fund_id),
    book_id        uuid        REFERENCES execution.books(book_id),
    strategy_id    uuid,
    as_of_date     date        NOT NULL,
    dimension      text        NOT NULL,
    dimension_key  text        NOT NULL,
    contribution   numeric(20, 4) NOT NULL,
    created_at     timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT performance_attribution_dim_chk
        CHECK (dimension IN ('strategy', 'sector', 'instrument'))
);

-- ---------------------------------------------------------------------------
-- Reconciliation
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS accounting.external_statements (
    statement_id   uuid PRIMARY KEY,
    provider       text        NOT NULL,
    statement_date date        NOT NULL,
    object_path    text        NOT NULL,
    content_hash   text        NOT NULL,
    parser_version text        NOT NULL,
    ingested_at    timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, statement_date, content_hash)
);

CREATE TABLE IF NOT EXISTS accounting.reconciliations (
    reconciliation_id uuid PRIMARY KEY,
    fund_id           uuid        NOT NULL REFERENCES execution.funds(fund_id),
    statement_id      uuid        REFERENCES accounting.external_statements(statement_id),
    recon_type        text        NOT NULL,
    rule_version      text        NOT NULL,
    as_of             timestamptz NOT NULL,
    result            text        NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT reconciliations_type_chk
        CHECK (recon_type IN ('order', 'fill', 'position', 'cash')),
    CONSTRAINT reconciliations_result_chk CHECK (result IN ('matched', 'break', 'partial'))
);

CREATE TABLE IF NOT EXISTS accounting.reconciliation_items (
    item_id           uuid PRIMARY KEY,
    reconciliation_id uuid        NOT NULL REFERENCES accounting.reconciliations(reconciliation_id),
    internal_ref      text,
    external_ref      text,
    internal_value    numeric(20, 4),
    external_value    numeric(20, 4),
    difference        numeric(20, 4),
    -- fuzzy match는 후보만 제시하고 자동 확정하지 않는다 (팀 가이드 4.5).
    match_method      text        NOT NULL,
    CONSTRAINT reconciliation_items_method_chk
        CHECK (match_method IN ('broker_id', 'client_order_id', 'attribute', 'fuzzy_candidate', 'unmatched'))
);

CREATE TABLE IF NOT EXISTS accounting.breaks (
    break_id          uuid PRIMARY KEY,
    reconciliation_id uuid        NOT NULL REFERENCES accounting.reconciliations(reconciliation_id),
    severity          text        NOT NULL,
    owner             text,
    due_date          date,
    status            text        NOT NULL DEFAULT 'open',
    resolution        text,
    evidence_path     text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    resolved_at       timestamptz,
    CONSTRAINT breaks_severity_chk CHECK (severity IN ('low', 'medium', 'high', 'material')),
    CONSTRAINT breaks_status_chk CHECK (status IN ('open', 'investigating', 'resolved', 'escalated'))
);

CREATE INDEX IF NOT EXISTS breaks_open_idx ON accounting.breaks (status, due_date)
    WHERE status IN ('open', 'investigating');
