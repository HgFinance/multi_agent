-- Sprint D2: 계정과목과 초기 Fund 시드
-- 소유: 도현
-- 근거: docs/HEDGE_FUND_MASTER_PLAN.md 5.7(Fund/Book 계층), 1.2(자본은 사용자가 정한다)
--
-- account_code는 accounting/ledger.py의 상수와 반드시 일치한다.
-- 한쪽만 바꾸면 분개가 존재하지 않는 계정을 참조하게 된다.

-- ---------------------------------------------------------------------------
-- 계정과목 (최소 세트)
-- 계정은 테이블 row라서 나중에 추가해도 스키마 변경이 없다.
-- 미수배당·미지급보수·성과보수·증거금은 필요해질 때 여기 INSERT만 추가한다.
-- ---------------------------------------------------------------------------

INSERT INTO accounting.ledger_accounts (account_id, account_code, name, account_type, currency) VALUES
    ('00000000-0000-0000-0000-000000001000', '1000', '현금',       'asset',     'KRW'),
    ('00000000-0000-0000-0000-000000001100', '1100', '유가증권',   'asset',     'KRW'),
    ('00000000-0000-0000-0000-000000001200', '1200', '미수금',     'asset',     'KRW'),
    ('00000000-0000-0000-0000-000000002000', '2000', '미지급금',   'liability', 'KRW'),
    ('00000000-0000-0000-0000-000000003000', '3000', '자본금',     'equity',    'KRW'),
    ('00000000-0000-0000-0000-000000004000', '4000', '실현손익',   'income',    'KRW'),
    ('00000000-0000-0000-0000-000000004100', '4100', '평가손익',   'income',    'KRW'),
    ('00000000-0000-0000-0000-000000005000', '5000', '수수료비용', 'expense',   'KRW'),
    ('00000000-0000-0000-0000-000000005100', '5100', '세금비용',   'expense',   'KRW')
ON CONFLICT (account_code) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 초기 Fund / Pod / Book
--
-- Fund 1개, Pod 1개, Book 1개로 시작하지만 데이터 모델은 다중을 지원한다
-- (마스터플랜 5.7). 여기서 늘리는 것은 INSERT 추가일 뿐이다.
-- ---------------------------------------------------------------------------

INSERT INTO execution.funds (fund_id, name, base_currency, inception_date, status) VALUES
    ('00000000-0000-0000-0000-0000000000f1', 'Paper Fund I', 'KRW', CURRENT_DATE, 'active')
ON CONFLICT (fund_id) DO NOTHING;

INSERT INTO execution.books (book_id, fund_id, pod_id, name, book_type, status) VALUES
    ('00000000-0000-0000-0000-0000000000b1',
     '00000000-0000-0000-0000-0000000000f1',
     '00000000-0000-0000-0000-0000000000d1',
     'KR Equity Long Book', 'strategy', 'active')
ON CONFLICT (book_id) DO NOTHING;

-- ---------------------------------------------------------------------------
-- 초기 자본 납입: 10억원
--
-- 사용자 Mandate 값이다. 바꾸려면 이 분개를 수정하지 말고 (Posted Journal은
-- 수정 금지) 추가 납입 분개를 새로 넣거나 반대 분개 후 재납입한다.
-- ---------------------------------------------------------------------------

SET app.fund_id = '00000000-0000-0000-0000-0000000000f1';  -- RLS 통과용

INSERT INTO accounting.journals (
    journal_id, fund_id, book_id, event_type, source_event_id,
    effective_at, accounting_date, currency, created_by_service, trace_id
) VALUES (
    '00000000-0000-0000-0000-0000000ca001'::uuid,
    '00000000-0000-0000-0000-0000000000f1',
    '00000000-0000-0000-0000-0000000000b1',
    'capital_injection', 'seed_capital_001',
    now(), CURRENT_DATE, 'KRW', 'svc_ledger', 'seed'
) ON CONFLICT (event_type, source_event_id) DO NOTHING;

-- 차) 현금 10억  /  대) 자본금 10억
INSERT INTO accounting.journal_lines (journal_line_id, journal_id, account_id, debit, credit, currency)
SELECT
    gen_random_uuid(),
    '00000000-0000-0000-0000-0000000ca001'::uuid,
    a.account_id,
    CASE WHEN a.account_code = '1000' THEN 1000000000 ELSE 0 END,
    CASE WHEN a.account_code = '3000' THEN 1000000000 ELSE 0 END,
    'KRW'
FROM accounting.ledger_accounts a
WHERE a.account_code IN ('1000', '3000')
  AND NOT EXISTS (
      SELECT 1 FROM accounting.journal_lines
      WHERE journal_id = '00000000-0000-0000-0000-0000000ca001'::uuid
  );

INSERT INTO accounting.strategy_allocations (allocation_id, book_id, strategy_id, capital_limit, effective_from)
VALUES (
    gen_random_uuid(),
    '00000000-0000-0000-0000-0000000000b1',
    '00000000-0000-0000-0000-0000000000e1',
    1000000000,
    now()
) ON CONFLICT DO NOTHING;
