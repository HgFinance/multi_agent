begin;

-- Supabase 소스에는 있지만 AWS `control`로는 아직 안 옮겨진 첫 번째 사용자의
-- Mandate 관련 고정값(fixture)을 이 migration chain에 옮긴다.
--
-- 담당: 영주 (CEO Office)
-- 근거: docs/02-engineering/SUPABASE_TO_AWS_CONTROL_MIGRATION.md,
--       deploy/aws/supabase_control_migration_scope.json (governance.mandates: "COPY")
--
-- ## 왜 필요한가
--
-- `supabase/seed.sql`은 governance.user_profiles/accounting.funds/
-- governance.fund_memberships 3개 표만 채운다(로그인 불가 플레이스홀더 회원,
-- RFC 2606 .invalid 주소). governance.mandates와 accounting.investor_profiles는
-- 그 이후 실제 UI 온보딩 흐름(POST /ui/mandates, POST /ui/investor-profiles)으로
-- 만들어졌다 - migration에도 seed.sql에도 없었다.
--
-- `scripts/aws_database_bootstrap.py`는 `supabase/migrations/`만 AWS `control`에
-- replay하고 `supabase/seed.sql`은 replay하지 않는다(스크립트 자체: CONTROL_MIGRATIONS
-- = supabase/migrations). 그 결과 governance.mandates(COPY 대상,
-- deploy/aws/supabase_control_migration_scope.json)가 control에 아직 없어
-- 이 Mandate를 참조하는 화면이 조회에 실패했다(2026-08-24 진단, 이 세션에서
-- Supabase DATABASE_URL로 직접 조회해 확인).
--
-- 여기서는 Supabase에 실제로 저장된 값 그대로(첫 번째 사용자, user_id
-- ...cec0, fund_code TEST-CEO-MANDATE) 옮긴다 - 임의로 지어내지 않는다
-- (개발 원칙 9와 같은 취지, 확정되지 않은 값을 확정된 것처럼 넣지 않는다).
--
-- ## 충돌 처리
--
-- user_profiles/funds/fund_memberships 3개 표는 migration scope 상
-- CONTROL_AUTHORITATIVE(control이 이미 SoT)다 - 이미 있으면 손대지 않는다
-- (ON CONFLICT DO NOTHING, 값을 덮어쓰지 않음). governance.mandates만 COPY
-- 정책이라 내용까지 소스 값으로 맞춘다(ON CONFLICT DO UPDATE).
-- accounting.investor_profiles는 version 이력이라 이미 있으면 그대로 둔다
-- (DO NOTHING - 과거 추천의 근거가 나중 수정으로 바뀌면 안 된다, 개발 원칙 5).

-- 1) 사용자 (없으면 생성, 있으면 손대지 않음)
insert into governance.user_profiles (user_id, display_name, timezone, status)
values (
  '00000000-0000-4000-8000-00000000cec0',
  'Fund Owner',
  'Asia/Seoul',
  'ACTIVE'
)
on conflict (user_id) do nothing;

-- 2) Fund
insert into accounting.funds (
  fund_id, fund_code, name, base_currency, inception_date, status, legal_entity
)
values (
  'b13f5cd1-5df0-4025-92cf-9be03b1a0296',
  'TEST-CEO-MANDATE',
  'Test CEO Mandate Fund',
  'USD',
  date '2026-08-01',
  'ACTIVE',
  '{}'::jsonb
)
on conflict (fund_id) do nothing;

-- 3) Fund membership (OWNER)
insert into governance.fund_memberships (fund_id, user_id, role, status, effective_from)
values (
  'b13f5cd1-5df0-4025-92cf-9be03b1a0296',
  '00000000-0000-4000-8000-00000000cec0',
  'OWNER',
  'ACTIVE',
  timestamptz '2026-08-18 06:15:10.482767+00'
)
on conflict (fund_id, user_id, role) do nothing;

-- 4) Mandate 현재 상태 (2026-08-14 UI 저장 경로가 만든 metadata override, COPY 정책)
insert into governance.mandates (
  mandate_id, fund_id, owner_user_id, name, status, current_version, metadata,
  created_at, updated_at
)
values (
  '2a88fba0-4566-4335-a8ac-744fb2bc8453',
  'b13f5cd1-5df0-4025-92cf-9be03b1a0296',
  '00000000-0000-4000-8000-00000000cec0',
  '00000000-0000-4000-8000-00000000cec0 운용 지침',
  'ACTIVE',
  0,
  jsonb_build_object(
    'objective_text',
    '위험을 감수하더라도 시장 이상의 극단적인 수익을 노린다. 투자 기간은 5년, 현금이 필요한 시점은 3년 후',
    'objective', '{}'::jsonb,
    'content_hash', 'afc603fe65039d3cb121a19540002c784c11074a055c902419de00e5420671ee',
    'updated_by', '00000000-0000-4000-8000-00000000cec0',
    'updated_at', '2026-08-23T09:47:02.110679+00:00',
    'policy', jsonb_build_object(
      'allowed_assets', '[]'::jsonb,
      'forbidden_assets', '[]'::jsonb,
      'risk_bounds', jsonb_build_object(
        'base_capital', '500000000',
        'currency', 'KRW',
        'max_instrument_weight', '0.1500',
        'max_sector_weight', '0.35',
        'max_gross_exposure', '1.5000',
        'max_concurrent_positions', 8,
        'max_daily_loss', '0.0300',
        'max_drawdown_pct', '0.2000'
      ),
      'universe_policy', jsonb_build_object(
        'allowed_markets', '["KRX"]'::jsonb,
        'allowed_asset_classes',
          '["KOREA_EQUITY","PROVISIONAL_ETF","LEVERAGED_ETF","PROVISIONAL_FUTURES","PROVISIONAL_OPTIONS","DERIVATIVES_HEDGE"]'::jsonb,
        'forbidden_asset_classes', '["PROVISIONAL_CRYPTO"]'::jsonb,
        'preferred_sectors', '[]'::jsonb,
        'excluded_sectors', '[]'::jsonb,
        'trading_start', '09:00',
        'trading_end', '15:30'
      ),
      'approval_rules', jsonb_build_object(
        'paper_order_mode', 'USER_APPROVAL',
        'risk_expansion_requires_user_approval', true
      ),
      'execution_rules', '{}'::jsonb
    )
  ),
  timestamptz '2026-08-18 08:39:35.396033+00',
  timestamptz '2026-08-23 09:47:03.301219+00'
)
on conflict (mandate_id) do update
  set fund_id = excluded.fund_id,
      owner_user_id = excluded.owner_user_id,
      name = excluded.name,
      status = excluded.status,
      current_version = excluded.current_version,
      metadata = excluded.metadata,
      updated_at = excluded.updated_at;

-- 5) 적합성 프로필 (advisory, 이미 있으면 그대로 둔다)
insert into accounting.investor_profiles (
  investor_profile_id, user_id, fund_id, version, mindset, experience,
  investment_horizon_years, max_drawdown_pct, liquidity_need, as_of, created_by, created_at
)
values (
  '03b00a0e-a646-4db5-aa20-5f0041ddcbed',
  '00000000-0000-4000-8000-00000000cec0',
  'b13f5cd1-5df0-4025-92cf-9be03b1a0296',
  1,
  'RISK_SEEKING',
  'EXPERIENCED',
  1,
  0.35000000,
  'LOW',
  timestamptz '2026-08-18 10:26:31.652+00',
  '00000000-0000-4000-8000-00000000cec0',
  timestamptz '2026-08-18 10:26:35.320635+00'
)
on conflict (investor_profile_id) do nothing;

commit;
