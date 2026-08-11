-- 전략 공장 핸드오프 테이블 - 방법론 리드 / 실험 기획안 / 실험 결과 환류
--
-- 담당: 재일 (리서치본부 + 퀀트·백테스트본부)
-- 계약: departments/01-research/contracts/factory_contracts.py
-- 설계: docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md 6.3·6.4·7.6.2절
--
-- ▶ 왜 research 스키마인가
--   experiment_outcomes 는 퀀트가 쓰지만 **읽는 쪽이 리서치**다. 소비자 곁에 두어야
--   리서치가 자기 스키마 조회면(research-api)으로 읽을 수 있다 - 리서치 Agent 는
--   DB Credential 을 받지 않으므로(통합계획 6.2) API 를 통해서만 닿는다.
--
-- ▶ 제약이 계약을 따라간다
--   Pydantic 이 막는 것을 DB 도 막는다. 한쪽만 막으면 다른 경로(직접 INSERT, 마이그레이션
--   스크립트)로 계약 위반 행이 들어오고, 그때부터 조회하는 쪽이 방어 코드를 쓰게 된다.

begin;

create schema if not exists research;

-- ── 방법론 리드 ─────────────────────────────────────────────────────────────
create table if not exists research.methodology_leads (
    lead_id                text primary key,       -- 출처 해시. 같은 소스 재수집 시 접힌다
    case_id                text        not null,
    scout_lens             text        not null,
    source_type            text        not null,
    as_known_at            timestamptz not null,
    refs                   jsonb       not null,   -- [{url,title,accessed_at,excerpt,...}]
    claimed_edge           text        not null,
    stated_mechanism       text        not null default '',
    inferred               boolean     not null default false,
    market_context         text        not null default '',
    stated_failure_mode    text        not null default '',
    independent_mentions   integer     not null default 1,
    testability            text        not null default 'RULE_EXPRESSIBLE',
    status                 text        not null default 'COMPLETE',
    model_version          text        not null default '',
    prompt_version         text        not null default '',
    created_at             timestamptz not null default now(),

    -- 출처 없는 리드는 리드가 아니다. 기억으로 재구성하는 것이 파이프라인의 첫 오염원이다.
    constraint chk_leads_refs_not_empty
        check (jsonb_typeof(refs) = 'array' and jsonb_array_length(refs) > 0),
    constraint chk_leads_edge_not_blank check (btrim(claimed_edge) <> ''),
    constraint chk_leads_mentions check (independent_mentions >= 1),
    constraint chk_leads_lens
        check (scout_lens in ('ACADEMIC','PRACTITIONER','COMMUNITY','CROSS_DOMAIN')),
    constraint chk_leads_source_type
        check (source_type in ('PAPER','BLOG','VIDEO','COMMUNITY',
                               'INVESTOR_LETTER','INTERNAL_FAILURE')),
    constraint chk_leads_testability
        check (testability in ('RULE_EXPRESSIBLE','VAGUE','UNUSABLE')),
    constraint chk_leads_status
        check (status in ('COMPLETE','PARTIAL','UNUSABLE','BLOCKED'))
);

comment on table research.methodology_leads is
  '스카우트가 웹에서 가져온 방법론 리드. lead_id 는 출처(url+title) 해시라 같은 소스를 '
  '다시 주워도 같은 행이다 - 상주 운영에서 중복이 접히는 자리.';

create index if not exists idx_leads_case on research.methodology_leads (case_id);
create index if not exists idx_leads_lens_time
    on research.methodology_leads (scout_lens, as_known_at desc);

-- ── 실험 기획안 (리서치 -> 퀀트 정본) ───────────────────────────────────────
create table if not exists research.experiment_proposals (
    proposal_id                  text primary key,
    case_id                      text        not null,
    as_known_at                  timestamptz not null,
    lead_ids                     text[]      not null,

    economic_rationale           text        not null,
    counterparty                 text        not null,
    competing_explanation        text        not null,
    competing_explanation_codes  text[]      not null,
    skeptic_sign                 text        not null,

    edge_type                    text        not null,
    universe_key                 text        not null,
    label                        text        not null default 'forward_return',
    baseline                     text        not null default 'equal_weight_buy_and_hold',
    falsification_tests          text[]      not null,
    data_requirements            jsonb       not null,
    suggested_params             jsonb       not null default '{}'::jsonb,
    trial_budget                 integer     not null default 5,
    prior_check                  jsonb       not null default '{}'::jsonb,
    -- 소스가 보고한 값. **우리 실험 결과와 분리된 열이다** - 남의 시장 숫자가
    -- 우리 백테스트 결과처럼 읽히면 미검증 값이 근거로 승격된다.
    source_reported_effect       jsonb       not null default '{}'::jsonb,

    research_packet_ids          text[]      not null default '{}',
    claim_ids                    text[]      not null default '{}',
    status                       text        not null default 'PUBLISHED',
    created_at                   timestamptz not null default now(),

    -- 발행 게이트가 요구하는 것을 DB 도 요구한다(한쪽만 막으면 다른 경로로 샌다)
    constraint chk_prop_counterparty check (btrim(counterparty) <> ''),
    constraint chk_prop_rationale    check (btrim(economic_rationale) <> ''),
    constraint chk_prop_skeptic      check (btrim(skeptic_sign) <> ''),
    constraint chk_prop_edge         check (btrim(edge_type) <> ''),
    constraint chk_prop_universe     check (btrim(universe_key) <> ''),
    constraint chk_prop_leads        check (array_length(lead_ids, 1) >= 1),
    constraint chk_prop_competing    check (array_length(competing_explanation_codes, 1) >= 1),
    constraint chk_prop_falsify      check (array_length(falsification_tests, 1) >= 1),
    constraint chk_prop_budget       check (trial_budget >= 1),
    constraint chk_prop_status
        check (status in ('PUBLISHED','ACCEPTED','REJECTED','SUPERSEDED'))
);

comment on table research.experiment_proposals is
  '리서치본부의 정본 산출물. 퀀트가 질문 없이 사전 등록할 수 있어야 한다 - '
  '반대편 주체·경쟁 설명·회의론자 서명·반증 검사가 전부 NOT NULL 제약으로 걸려 있다.';

create index if not exists idx_prop_status_time
    on research.experiment_proposals (status, as_known_at desc);
create index if not exists idx_prop_edge_universe
    on research.experiment_proposals (edge_type, universe_key);

-- ── 실험 결과 환류 (퀀트 -> 리서치) ─────────────────────────────────────────
create table if not exists research.experiment_outcomes (
    outcome_id        text primary key,
    experiment_id     text        not null,
    hypothesis_id     text        not null,
    trial_family_id   text        not null,
    trial_number      integer     not null,
    decision          text        not null,
    decided_at        timestamptz not null,
    proposal_id       text        not null default '',   -- 자체 생성 가설이면 빈 값
    failed_criteria   text[]      not null default '{}',
    oos_summary       jsonb       not null default '{}'::jsonb,  -- 미측정은 키 부재/null
    regime_concerns   text[]      not null default '{}',
    lesson_codes      text[]      not null default '{}',
    notes             text        not null default '',   -- 자유 서술은 여기만
    created_at        timestamptz not null default now(),

    -- 순번은 1부터. 0/음수는 "안 셌다" 와 구분이 안 된다.
    constraint chk_out_trial check (trial_number >= 1),
    constraint chk_out_decision
        check (decision in ('REJECT','REVISE','SUBMIT_TO_QA','GATE_HOLD','BLOCKED',
                            'PROMOTED','KILLED','DEMOTED','ARCHIVED')),
    -- 부정 종결인데 사유가 없으면 다음 기획안이 대조할 것이 없다 = 환류가 성립 안 한다
    constraint chk_out_reason_for_negative check (
        decision not in ('REJECT','GATE_HOLD','KILLED','DEMOTED')
        or array_length(failed_criteria, 1) >= 1
        or array_length(lesson_codes, 1) >= 1
    )
);

comment on table research.experiment_outcomes is
  '퀀트 실험의 모든 종결(성공 포함)을 통제 어휘로 환류한다. Gate 0 가 trial_family_id 로 '
  '이 표를 조회해 대응 없는 재도전을 막는다 - 적재가 실험 종결의 전제 조건이다.';

-- Gate 0 의 조회 경로. 같은 계열의 기각 이력을 최신순으로 읽는다.
create index if not exists idx_out_family_time
    on research.experiment_outcomes (trial_family_id, decided_at desc);
create index if not exists idx_out_proposal
    on research.experiment_outcomes (proposal_id) where proposal_id <> '';
create index if not exists idx_out_decision
    on research.experiment_outcomes (decision);

commit;
