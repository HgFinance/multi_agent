begin;

-- 사용자 적합성 프로필(InvestorProfile) 저장소.
-- 소유: 도현 (회계·포트폴리오본부)
-- 근거: docs/02-engineering/USER_INPUT_API_SPEC.md 1.5(**G-2**) DDL 그대로
--       docs/01-product/USER_INPUT_SPEC.md 2(계층 1 - 화면 직접 선택)
--       departments/05-accounting-portfolio/portfolio/suitability.py InvestorProfile
--
-- **왜 회계·포트폴리오본부가 소유하나**: Mandate 기준을 리스크본부가 갖는 것과
-- 같은 원칙이다(API_SPEC 1.5). 적합성 판단은 포트폴리오 구성 판단이라 이쪽이다.
--
-- **이 테이블이 없어서 생겼던 공백**: mindset·experience는 USER_INPUT_SPEC 2절의
-- 필수 항목(1·2번)인데 저장 위치가 없었다. 그래서 지금까지
-- `POST /ui/portfolio-recommendations`가 매 요청 body로 성향을 받아왔다 - 저장된
-- 값을 읽는 게 아니라 매번 다시 받는 구조였고, "최초 1회 입력 후 재사용"이
-- 불가능했다. 이 테이블이 그 재사용의 근거가 된다.
--
-- **Version을 두는 이유**(API_SPEC 1.5): 성향은 바뀔 수 있고 "그때 어떤 성향으로
-- 추천했는가"가 감사 대상이다. 다만 Mandate와 달리 **승인 절차는 없다** -
-- advisory 입력이라 Risk/QA 검토를 타지 않는다. append-only가 아니라 새 version을
-- 쌓는 방식이라 과거 추천의 근거가 나중 수정으로 바뀌지 않는다(개발 원칙 5).
--
-- **max_drawdown_pct가 Mandate와 양쪽에 있다**: USER_INPUT_SPEC 8절 미확정 3번 -
-- 두 값이 항상 같아야 하는지 독립인지 아직 정하지 않았다. 그래서 이 마이그레이션은
-- 두 값을 강제로 일치시키는 제약을 걸지 않는다. 임의로 정하면 나중에 결정이
-- 뒤집힐 때 데이터를 되돌릴 수 없다.

create table if not exists accounting.investor_profiles (
  investor_profile_id uuid primary key default gen_random_uuid(),
  user_id  uuid not null references governance.user_profiles(user_id),
  fund_id  uuid not null references accounting.funds(fund_id),
  version  integer not null check (version > 0),

  -- suitability.py의 InvestmentMindset / ExperienceLevel / LiquidityNeed Enum과
  -- 1:1이다. CHECK를 DB에도 두는 이유: 이 테이블은 API 말고 마이그레이션·수동
  -- 조회로도 접근되므로, Pydantic만 믿으면 우회 경로로 잘못된 값이 들어온다.
  mindset    text not null check (mindset in ('SAFETY_FIRST','BALANCED','RISK_SEEKING')),
  experience text not null check (experience in ('BEGINNER','INTERMEDIATE','EXPERIENCED')),
  investment_horizon_years integer not null check (investment_horizon_years between 1 and 100),
  -- 0~1 분수다(0.15 = 15%). `_pct` 접미사인데 분수인 것은 기존 계약
  -- (apps/api/main.py PortfolioRecommendationRequest.max_drawdown_pct)과 맞춘 것이다.
  max_drawdown_pct numeric(9,8) not null check (max_drawdown_pct > 0 and max_drawdown_pct <= 1),
  liquidity_need text not null check (liquidity_need in ('HIGH','MEDIUM','LOW')),

  -- as_of: 이 프로필이 어느 시점 기준인가(PIT). created_at과 다르다 - 과거
  -- 시점으로 소급 입력할 수 있어야 replay가 성립한다(개발 원칙 5).
  as_of      timestamptz not null,
  created_by uuid references governance.user_profiles(user_id),
  created_at timestamptz not null default now(),

  unique (user_id, fund_id, version)
);

-- `current` 조회(GET /portfolio/v1/investor-profiles/current)가 매번
-- max(version)을 찾으므로 정렬 인덱스를 준다.
create index if not exists investor_profiles_user_fund_version_idx
  on accounting.investor_profiles (user_id, fund_id, version desc);

comment on table accounting.investor_profiles is
  '사용자 적합성 프로필. 새 version을 쌓는 방식이며 Risk/QA 승인 절차가 없는 advisory 입력이다 (USER_INPUT_API_SPEC.md 1.5).';

commit;
