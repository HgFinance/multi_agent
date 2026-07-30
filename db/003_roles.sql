-- Sprint D0: Service Role, Grant, RLS
-- 소유: 도현
-- 근거: docs/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md 5.1, 5.2
--       docs/HEDGE_FUND_MASTER_PLAN.md 5.6(권한 분리 원칙)
--
-- 스택 메모: 팀 가이드는 Supabase 기준이지만 스택은 마스터플랜 13.1(PostgreSQL)로
-- 확정됐다. RLS와 Role은 PostgreSQL 기본 기능이라 그대로 쓰고, Supabase 전용인
-- `api` 스키마 View/RPC와 JWT Claim은 뺐다. 외부 경계는 FastAPI가 담당한다.
--
-- 핵심: 어떤 에이전트도 DB에 직접 붙지 않는다. Hermes Agent에게는 DB Role을
-- 발급하지 않으며, API를 통해서만 제안한다 (팀 가이드 5.1 마지막 두 줄).

-- ---------------------------------------------------------------------------
-- Service Role. LOGIN 권한은 배포 시점에 부여하고 비밀번호는 Secret Store에서 온다.
-- ---------------------------------------------------------------------------

DO $$
DECLARE r text;
BEGIN
    FOREACH r IN ARRAY ARRAY[
        'svc_trading_workflow',  -- Trade Case와 Order Intent 제안까지만
        'svc_oms',               -- 주문 상태와 체결. 원장은 못 건드린다
        'svc_broker_adapter',    -- 브로커 원본 이벤트만
        'svc_ledger',            -- 분개와 projection
        'svc_reconciliation',    -- 대사와 Break
        'svc_nav'                -- 평가/PnL/NAV
    ] LOOP
        IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = r) THEN
            EXECUTE format('CREATE ROLE %I NOLOGIN', r);
        END IF;
    END LOOP;
END $$;

GRANT USAGE ON SCHEMA execution  TO svc_trading_workflow, svc_oms, svc_broker_adapter,
                                    svc_ledger, svc_reconciliation, svc_nav;
GRANT USAGE ON SCHEMA accounting TO svc_ledger, svc_reconciliation, svc_nav, svc_oms;

-- --- svc_trading_workflow: 제안만 --------------------------------------------
-- Order Intent까지만 만들 수 있다. orders에 직접 쓰면 Risk Gate를 우회하게 된다.
GRANT SELECT, INSERT, UPDATE ON execution.trade_cases, execution.order_intents
    TO svc_trading_workflow;
GRANT SELECT ON execution.orders, execution.fills, execution.execution_plans,
                execution.funds, execution.books
    TO svc_trading_workflow;
GRANT SELECT ON accounting.positions, accounting.cash_balances TO svc_trading_workflow;

-- --- svc_oms: 주문 생명주기 ---------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON execution.orders TO svc_oms;
GRANT SELECT, INSERT ON execution.order_events, execution.fills,
                        execution.execution_plans, execution.execution_exceptions TO svc_oms;
GRANT SELECT ON execution.order_intents, execution.order_state_transitions,
                execution.funds, execution.books TO svc_oms;
GRANT UPDATE (intent_status, risk_request_id) ON execution.order_intents TO svc_oms;
-- 원장 접근 없음. 회계 수치는 svc_ledger만 만든다.

-- --- svc_broker_adapter: 원본 이벤트만 ----------------------------------------
GRANT SELECT, INSERT, UPDATE ON execution.broker_sessions TO svc_broker_adapter;
GRANT SELECT ON execution.orders TO svc_broker_adapter;
GRANT INSERT ON accounting.external_statements TO svc_broker_adapter;
-- Order Intent 생성 권한 없음.

-- --- svc_ledger: 원장과 projection --------------------------------------------
GRANT SELECT, INSERT ON accounting.journals, accounting.journal_lines TO svc_ledger;
GRANT SELECT, INSERT, UPDATE ON accounting.positions, accounting.cash_balances TO svc_ledger;
GRANT SELECT ON accounting.ledger_accounts, accounting.strategy_allocations TO svc_ledger;
GRANT SELECT ON execution.fills, execution.orders, execution.funds, execution.books TO svc_ledger;
-- trade_cases/order_intents 쓰기 권한 없음. 원장이 시그널을 만들 수 없다.

-- --- svc_reconciliation: 대사 -------------------------------------------------
GRANT SELECT, INSERT, UPDATE ON accounting.reconciliations, accounting.reconciliation_items,
                                 accounting.breaks TO svc_reconciliation;
GRANT SELECT ON accounting.external_statements, accounting.journals, accounting.journal_lines,
                accounting.positions, accounting.cash_balances TO svc_reconciliation;
GRANT SELECT ON execution.orders, execution.fills, execution.order_events TO svc_reconciliation;
-- 분개 수정 권한 없음. 불일치를 발견해도 원장을 고칠 수 없고 Break만 남긴다.

-- --- svc_nav: 평가와 NAV -------------------------------------------------------
GRANT SELECT, INSERT ON accounting.valuations, accounting.pnl_snapshots,
                        accounting.nav_runs, accounting.nav_components,
                        accounting.performance_attribution TO svc_nav;
GRANT UPDATE (status, approved_by, approved_at, evidence_path) ON accounting.nav_runs TO svc_nav;
GRANT SELECT ON accounting.positions, accounting.cash_balances, accounting.journals,
                accounting.journal_lines TO svc_nav;
-- fills 수정 권한 없음.

-- ---------------------------------------------------------------------------
-- Row Level Security: Fund 단위 격리
--
-- 지금은 Fund가 1개지만 마스터플랜 5.7이 다중 Fund를 요구하고, RLS는 나중에
-- 붙이는 비용이 훨씬 크다. 세션에서 app.fund_id를 설정해야 행이 보인다.
--
-- 미설정 시 current_setting이 NULL을 반환해 **아무 행도 보이지 않는다**(fail-closed).
-- 조회가 비어 나오면 SET app.fund_id 를 빠뜨린 것이다.
-- ---------------------------------------------------------------------------

DO $$
DECLARE t text;
BEGIN
    FOREACH t IN ARRAY ARRAY[
        'execution.trade_cases',
        'accounting.journals',
        'accounting.positions',
        'accounting.cash_balances',
        'accounting.valuations',
        'accounting.pnl_snapshots',
        'accounting.nav_runs'
    ] LOOP
        EXECUTE format('ALTER TABLE %s ENABLE ROW LEVEL SECURITY', t);
        -- 소유자도 우회하지 못하게 강제한다. FORCE가 없으면 RLS는 장식이다.
        EXECUTE format('ALTER TABLE %s FORCE ROW LEVEL SECURITY', t);
        EXECUTE format('DROP POLICY IF EXISTS fund_isolation ON %s', t);
        EXECUTE format(
            'CREATE POLICY fund_isolation ON %s USING (fund_id::text = current_setting(''app.fund_id'', true))',
            t
        );
    END LOOP;
END $$;

-- ---------------------------------------------------------------------------
-- Audit: 누가 무엇을 바꿨는지. AI QA/감사본부의 Evidence가 된다 (마스터플랜 5.5 #6).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS execution.audit_log (
    audit_id    bigserial PRIMARY KEY,
    table_name  text        NOT NULL,
    operation   text        NOT NULL,
    row_pk      text,
    db_role     text        NOT NULL DEFAULT current_user,
    changed_at  timestamptz NOT NULL DEFAULT now(),
    old_row     jsonb,
    new_row     jsonb
);

CREATE OR REPLACE FUNCTION execution.audit_row() RETURNS trigger AS $$
BEGIN
    INSERT INTO execution.audit_log (table_name, operation, row_pk, old_row, new_row)
    VALUES (
        TG_TABLE_SCHEMA || '.' || TG_TABLE_NAME,
        TG_OP,
        COALESCE(to_jsonb(NEW), to_jsonb(OLD)) ->> (TG_ARGV[0]),
        CASE WHEN TG_OP = 'INSERT' THEN NULL ELSE to_jsonb(OLD) END,
        CASE WHEN TG_OP = 'DELETE' THEN NULL ELSE to_jsonb(NEW) END
    );
    RETURN NULL;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER SET search_path = execution, pg_temp;

DROP TRIGGER IF EXISTS audit_orders ON execution.orders;
CREATE TRIGGER audit_orders AFTER INSERT OR UPDATE OR DELETE ON execution.orders
    FOR EACH ROW EXECUTE FUNCTION execution.audit_row('order_id');

DROP TRIGGER IF EXISTS audit_journals ON accounting.journals;
CREATE TRIGGER audit_journals AFTER INSERT OR UPDATE OR DELETE ON accounting.journals
    FOR EACH ROW EXECUTE FUNCTION execution.audit_row('journal_id');

DROP TRIGGER IF EXISTS audit_nav_runs ON accounting.nav_runs;
CREATE TRIGGER audit_nav_runs AFTER INSERT OR UPDATE OR DELETE ON accounting.nav_runs
    FOR EACH ROW EXECUTE FUNCTION execution.audit_row('nav_run_id');

-- audit_log는 어떤 서비스도 지울 수 없다.
GRANT INSERT ON execution.audit_log TO svc_trading_workflow, svc_oms, svc_ledger,
                                       svc_reconciliation, svc_nav, svc_broker_adapter;
GRANT USAGE ON SEQUENCE execution.audit_log_audit_id_seq TO svc_trading_workflow, svc_oms,
                                       svc_ledger, svc_reconciliation, svc_nav, svc_broker_adapter;
REVOKE UPDATE, DELETE ON execution.audit_log FROM PUBLIC;
