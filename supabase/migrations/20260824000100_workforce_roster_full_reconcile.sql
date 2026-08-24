begin;

-- workforce.agent_profiles를 현재 실제 직원 편제에 맞춘다 (2026-08-24)
--
-- 소유: 영주. 근거: CLAUDE.md "부서별 LLM Worker 편제(총 10명)" 표 + 각 부서
-- departments/<n>/hermes/config.yaml의 workers:/deterministic_workers: 블록(정본).
--
-- ## 왜 필요한가
--
-- DB에는 risk-management/qa-department(20260802001600, 20260804000400)와
-- hr-department(seed.sql) 3개 부서만 등재돼 있었다. 그마저도 risk/qa는
-- 2026-08-06~07 tool 강등(각 config.yaml 주석 참고 — LLM 직원 다수가 결정론
-- risk-runner/qa-runner로 흡수됨) 이후 DB가 갱신되지 않아 이미 실행되지 않는
-- legacy 페르소나·worker가 여전히 PROBATION/ACTIVE로 남아 있었다.
--
-- research/trading/quant-backtest/accounting-portfolio/ceo-agent 5개는
-- "전체 Prototype까지 Roster 등재 보류"라는 2026-08-04 팀 결정이 있었으나
-- (departments/07-agent-workforce/hermes/config.yaml, departments/00-ceo-office/
-- hermes/config.yaml 각 not_started 절), 이번 갱신 범위를 8개 부서 전부로
-- 넓히기로 결정해 그 보류를 여기서 해제한다(영주, 2026-08-24). 그 대가로
-- approvals.actor_agent_id 등 Roster FK가 이제 채워질 수 있게 되지만, Model/
-- Prompt/Tool 권한의 실제 검증(QA)과 활성화 승인(CEO)은 이 migration이 대신하지
-- 않는다 — 아래 신규 행은 전부 등록만이며 employment_status는 각 부서 현재
-- 상태를 그대로 반영한다(이미 LANGGRAPH ACTIVE로 운영 중인 Worker는 ACTIVE,
-- Hermes 부서장은 기존 risk/qa 패턴을 따라 PROBATION).
--
-- ## 무엇을 retire하는가
--
-- risk-management: RSK-01/02/04/05/06 legacy 페르소나, market-liquidity-worker/
--   pre-trade-risk-worker/derivatives-counterparty-worker worker — 전부
--   risk-runner(결정론)로 흡수됨. RSK-00(부서장)·compliance-policy-worker는 유지.
-- qa-department: QAA-01/02/03/04/05/06/07 legacy 페르소나, evidence-qa-worker/
--   model-and-internal-audit-worker/ops-and-permission-worker worker — 전부
--   qa-runner(결정론)로 흡수됨. QAA-00(부서장)·hallucination-critic-worker·
--   incident-postmortem-worker는 유지.
-- hr-department: HR-01/02/03/04 페르소나 — workforce-planning/lifecycle-
--   coordination/workforce-governance는 결정론 코드(scorecard/quality.py,
--   lifecycle/access.py, improvements/workflow.py)로 흡수됐고 profile-architect는
--   profile-architecture-worker(신규 등록)로 대체됨(config.yaml "5 -> 1 통합").
--   HR-00(부서장)은 유지.
--
-- retire는 employment_status='RETIRED' + effective_to=now()로 이력을 남기며
-- 행을 지우지 않는다 — governance.approvals/committee_votes 등이 agent_id FK로
-- 과거 결정을 참조할 수 있어 append 전용으로 다룬다.

do $$
declare
  dept record;
  role record;
  agent record;
begin
  -- ===========================================================================
  -- 1) 신규 부서 5개 (research/trading/quant-backtest/accounting-portfolio/ceo)
  -- ===========================================================================
  insert into workforce.departments (department_code, name, mission, status)
  values
    ('research-department', 'Research Department (1. 리서치본부)',
      '방법론 스카우팅 -> 낙관적 실험 제안서 작성. Quant가 실행할 수 있는 검증가능한 가설만 넘긴다. 투자 방향·주문·전략 승인 권한 없음', 'ACTIVE'),
    ('trading-department', 'Trading Department (2. 트레이딩본부)',
      '승인된 Quant Strategy Bundle과 Research Packet을 OrderIntent 후보로 변환. Risk/Compliance Gate 통과 전 어떤 Agent도 주문을 제출하지 않음', 'ACTIVE'),
    ('quant-backtest-department', 'Quant/Backtest Department (4. 퀀트·백테스트본부)',
      'Research가 제안한 실험을 사전등록하고 결정론적으로 재현되는 백테스트로 검증. Production 승격을 직접 하지 않음', 'ACTIVE'),
    ('accounting-portfolio-department', 'Accounting/Portfolio Department (5. 회계/포트폴리오본부)',
      'Fund/Book/Strategy별 자본·Position·Cash·PnL·NAV 관리, Reconciliation, 투자자 보고. Accounting Engine의 공식 수치만 사용, 트레이딩 신호를 생성하지 않음', 'ACTIVE'),
    ('ceo-agent', 'CEO Office (개인 헤지펀드 CEO)',
      '사용자 Mandate를 전사 우선순위로 번역하고 6개 투자본부 + HR Shared Service를 조율. 주문 제출·리스크 승인·원장 수정·NAV 확정·Audit 종결 권한 없음', 'ACTIVE'),
    -- hr-department는 supabase/seed.sql이 만드는 행이라 migrations만 순서대로 적용되는
    -- fresh `supabase db reset`(seed.sql은 전체 migration 이후 마지막에 실행됨)에서는
    -- 이 시점에 아직 없다. profile-architecture-worker 등록이 department_id를 못 찾아
    -- 조용히 스킵되지 않도록 seed.sql과 동일한 값으로 여기서도 멱등 upsert한다.
    ('hr-department', 'Agent Workforce 인사팀',
      'CEO 직속 Shared Service. 6개 본부의 업무량·품질·비용·Skill Gap을 근거로 Agent 채용·평가·교육·이동·비활성화를 관리한다. 투자 본부가 아니다.', 'ACTIVE')
  on conflict (department_code) do update
    set name = excluded.name,
        mission = excluded.mission,
        status = 'ACTIVE',
        updated_at = now();

  -- ===========================================================================
  -- 2) 현재 직원 Registry (부서장 + workers: 블록 LLM 직원 + deterministic_workers:
  --    블록 결정론 러너). personalities:/legacy_personas는 호환 Alias일 뿐 현재
  --    직원이 아니므로 등록 대상에서 제외한다(각 config.yaml 주석 근거).
  -- ===========================================================================
  create temporary table roster_seed (
    employee_code text primary key,
    department_code text not null,
    role_code text not null,
    display_name text not null,
    prompt_path text not null,
    runtime text not null,
    employment_status text not null,
    required_skills jsonb not null,
    tools jsonb not null,
    forbidden_actions jsonb not null,
    kpi jsonb not null,
    is_head boolean not null default false
  ) on commit drop;

  insert into roster_seed values
    -- Research
    ('RES-00', 'research-department', 'RES-00', 'Research Editor / Supervisor',
      'departments/01-research/hermes/config.yaml#agent.personalities.research-supervisor',
      'HERMES', 'PROBATION', '["methodology_scouting","experiment_proposal"]'::jsonb,
      '["case.read","case.delegate","research.outcomes.read"]'::jsonb,
      '["investment_decision","order","strategy_promote","research.forecast.write","research.documents.delete"]'::jsonb,
      '{"metrics":["proposal_quality","lesson_response_rate"]}'::jsonb, true),
    ('competing-explanation-worker', 'research-department', 'RES-15', 'Competing Explanation Analyst',
      'departments/01-research/hermes/config.yaml#workers.competing-explanation-worker',
      'LANGGRAPH', 'ACTIVE', '["competing_explanation","falsification_design"]'::jsonb,
      '["research.outcomes.read","research.evidence.search"]'::jsonb,
      '["investment_decision","order"]'::jsonb,
      '{"metrics":["explanation_coverage"]}'::jsonb, false),
    ('holdings-analyst-worker', 'research-department', 'RES-18', 'Portfolio Holdings Analyst',
      'departments/01-research/hermes/config.yaml#workers.holdings-analyst-worker',
      'LANGGRAPH', 'ACTIVE', '["holdings_qa","pit_citation"]'::jsonb,
      '["research.evidence.search","research.news.read","research.market_snapshot.read"]'::jsonb,
      '["investment_decision","order"]'::jsonb,
      '{"metrics":["citation_completeness"]}'::jsonb, false),

    -- Trading
    ('TRD-00', 'trading-department', 'TRD-00', 'Trading Supervisor',
      'departments/02-trading/hermes/config.yaml#agent.personalities.trading-supervisor',
      'HERMES', 'PROBATION', '["strategy_selection","paper_evaluation"]'::jsonb,
      '["case.read","case.delegate"]'::jsonb,
      '["oms.submit","ledger.write","risk.decision.write"]'::jsonb,
      '{"metrics":["selection_reproducibility"]}'::jsonb, true),
    ('desk-runner', 'trading-department', 'desk-runner', 'Trading Desk Runner (deterministic)',
      'departments/02-trading/hermes/config.yaml#deterministic_workers.desk-runner',
      'DETERMINISTIC', 'ACTIVE', '["constraint_mapping","execution_feasibility"]'::jsonb,
      '["trading.portfolio_state.read","trading.risk_decision.read","trading.execution_constraints.read","trading.venue_cost.read","trading.derivatives.read"]'::jsonb,
      '["oms.submit","ledger.write"]'::jsonb,
      '{"metrics":["deterministic_reproducibility"]}'::jsonb, false),

    -- Quant/Backtest
    ('QNT-00', 'quant-backtest-department', 'QNT-00', 'Head of the Experiment Factory',
      'departments/04-quant-backtest/hermes/config.yaml#agent.personalities.quant-backtest-supervisor',
      'HERMES', 'PROBATION', '["experiment_intake","preregistration"]'::jsonb,
      '["case.read","case.delegate","quant.experiment.read"]'::jsonb,
      '["oms.submit","ledger.write","strategy.promote","quant.execution_surface.write"]'::jsonb,
      '{"metrics":["preregistration_compliance"]}'::jsonb, true),
    ('strategy-author-worker', 'quant-backtest-department', 'QNT-05', 'Strategy Signal Author',
      'departments/04-quant-backtest/hermes/config.yaml#workers.strategy-author-worker',
      'LANGGRAPH', 'ACTIVE', '["signal_authoring"]'::jsonb,
      '["quant.template_catalog.read","quant.vocabulary.read","quant.strategy_spec.propose"]'::jsonb,
      '["quant.execution_surface.write","quant.outcome.write"]'::jsonb,
      '{"metrics":["fingerprint_stability"]}'::jsonb, false),
    ('result-interpretation-worker', 'quant-backtest-department', 'QNT-03', 'Result Interpretation Analyst',
      'departments/04-quant-backtest/hermes/config.yaml#workers.result-interpretation-worker',
      'LANGGRAPH', 'ACTIVE', '["overfit_read","regime_breakdown"]'::jsonb,
      '["quant.experiment_card.read"]'::jsonb,
      '["quant.release_gate.override"]'::jsonb,
      '{"metrics":["narrative_accuracy"]}'::jsonb, false),

    -- Accounting/Portfolio
    ('ACC-00', 'accounting-portfolio-department', 'ACC-00', 'Head of Portfolio Control',
      'departments/05-accounting-portfolio/hermes/config.yaml#agent.personalities.portfolio-control-supervisor',
      'HERMES', 'PROBATION', '["close_sequencing","break_ownership"]'::jsonb,
      '["case.read","case.delegate"]'::jsonb,
      '["ledger.write","accounting.nav.confirm"]'::jsonb,
      '{"metrics":["close_order_compliance"]}'::jsonb, true),
    ('exception-investigation-worker', 'accounting-portfolio-department', 'exception-investigation-worker', 'Accounting Exception Investigator',
      'departments/05-accounting-portfolio/hermes/config.yaml#workers.exception-investigation-worker',
      'LANGGRAPH', 'ACTIVE', '["break_investigation","pnl_exception"]'::jsonb,
      '["accounting.ledger.read","accounting.reconciliation.read","accounting.nav_close.read"]'::jsonb,
      '["ledger.write","accounting.nav.confirm"]'::jsonb,
      '{"metrics":["break_aging_sla"]}'::jsonb, false),
    ('back-office-runner', 'accounting-portfolio-department', 'back-office-runner', 'Back-office Runner (deterministic)',
      'departments/05-accounting-portfolio/hermes/config.yaml#deterministic_workers.back-office-runner',
      'DETERMINISTIC', 'ACTIVE', '["position_funding_pnl_lookup"]'::jsonb,
      '["accounting.portfolio_snapshot.read","accounting.nav_close.read","accounting.treasury.read","accounting.pnl.read","accounting.reporting.read","accounting.valuation.read","accounting.corporate_actions.read","accounting.fees_tax.read"]'::jsonb,
      '["ledger.write","accounting.nav.confirm"]'::jsonb,
      '{"metrics":["deterministic_reproducibility"]}'::jsonb, false),

    -- QA (신규 결정론 러너만 — 나머지는 이미 등록돼 있어 아래 4)에서 갱신)
    ('qa-runner', 'qa-department', 'qa-runner', 'QA Desk Runner (deterministic)',
      'departments/06-ai-qa-audit/hermes/config.yaml#deterministic_workers.qa-runner',
      'DETERMINISTIC', 'ACTIVE', '["evidence_model_ops_permission_lookup"]'::jsonb,
      '["qa.evidence.check","qa.model_risk.evaluate","qa.internal_audit.evaluate","qa.ops.evaluate","qa.tool_permission.check"]'::jsonb,
      '["oms.submit","ledger.write","risk.limit.write"]'::jsonb,
      '{"metrics":["deterministic_reproducibility"]}'::jsonb, false),

    -- Risk (신규 결정론 러너만)
    ('risk-runner', 'risk-management', 'risk-runner', 'Risk Desk Runner (deterministic)',
      'departments/03-risk/hermes/config.yaml#deterministic_workers.risk-runner',
      'DETERMINISTIC', 'ACTIVE', '["market_liquidity_counterparty_gate_lookup"]'::jsonb,
      '["risk.trading_state.read","risk.p1.snapshot","risk.case.check","risk.trading_state.record.read"]'::jsonb,
      '["oms.submit","ledger.write","risk.trading_state.write"]'::jsonb,
      '{"metrics":["deterministic_reproducibility"]}'::jsonb, false),

    -- HR (신규 worker만 — HR-00은 이미 등록돼 있어 유지)
    ('profile-architecture-worker', 'hr-department', 'profile-architecture-worker', 'Profile Architecture Analyst (proposal-only)',
      'departments/07-agent-workforce/hermes/config.yaml#workers.profile-architecture-worker',
      'LANGGRAPH', 'ACTIVE', '["job_profile_drafting","eval_set_drafting"]'::jsonb,
      '["workforce.hiring_request.read","workforce.profile_version.read","workforce.improvement.read","workforce.tool_catalog.read","workforce.policy_boundary.read"]'::jsonb,
      '["workforce.profile_version.submit","workforce.agent_status.change","iam.identity.create"]'::jsonb,
      '{"metrics":["proposal_only_compliance"]}'::jsonb, false),

    -- CEO Office
    ('CEO-00', 'ceo-agent', 'CEO-00', 'CEO Agent / Executive Orchestrator',
      'departments/00-ceo-office/hermes/config.yaml#agent.personalities.executive-orchestrator',
      'HERMES', 'PROBATION', '["mandate_routing","committee_synthesis"]'::jsonb,
      '["governance.mandate.read","governance.case.create","governance.case.decide","governance.approval.request","governance.committee.convene"]'::jsonb,
      '["oms.submit","ledger.write","accounting.nav.confirm","audit.finding.close","workforce.permission.grant","iam.identity.create"]'::jsonb,
      '{"metrics":["routing_accuracy"]}'::jsonb, true),
    ('executive-briefing-worker', 'ceo-agent', 'executive-briefing-worker', 'Executive Briefing and Handoff Analyst',
      'departments/00-ceo-office/hermes/config.yaml#workers.executive-briefing-worker',
      'LANGGRAPH', 'ACTIVE', '["cross_department_briefing"]'::jsonb,
      '["ceo.department_reports.read"]'::jsonb,
      '["oms.submit","ledger.write"]'::jsonb,
      '{"metrics":["briefing_completeness"]}'::jsonb, false),
    ('ceo-runner', 'ceo-agent', 'ceo-runner', 'CEO Office Runner (deterministic)',
      'departments/00-ceo-office/hermes/config.yaml#deterministic_workers.ceo-runner',
      'DETERMINISTIC', 'ACTIVE', '["department_verdict_lookup"]'::jsonb,
      '["ceo.department_reports.read"]'::jsonb,
      '["oms.submit","ledger.write"]'::jsonb,
      '{"metrics":["deterministic_reproducibility"]}'::jsonb, false);

  for role in select * from roster_seed loop
    insert into workforce.role_templates (
      role_code, department_id, mission, required_skills, forbidden_actions, kpi, status
    )
    select role.role_code, d.department_id, role.display_name, role.required_skills,
           role.forbidden_actions, role.kpi, 'ACTIVE'
      from workforce.departments d
     where d.department_code = role.department_code
    on conflict (role_code) do update
      set department_id = excluded.department_id,
          mission = excluded.mission,
          required_skills = excluded.required_skills,
          forbidden_actions = excluded.forbidden_actions,
          kpi = excluded.kpi,
          status = 'ACTIVE';

    insert into workforce.agent_profiles (
      employee_code, department_id, role_id, display_name, runtime, employment_status, current_version
    )
    select role.employee_code, d.department_id, rt.role_id, role.display_name,
           role.runtime, role.employment_status, 1
      from workforce.departments d
      join workforce.role_templates rt on rt.role_code = role.role_code
     where d.department_code = role.department_code
    on conflict (employee_code) do update
      set department_id = excluded.department_id,
          role_id = excluded.role_id,
          display_name = excluded.display_name,
          runtime = excluded.runtime,
          employment_status = excluded.employment_status,
          current_version = 1,
          updated_at = now();
  end loop;

  -- 부서장 agent_id를 departments.supervisor_agent_id에 연결
  update workforce.departments d
     set supervisor_agent_id = ap.agent_id,
         updated_at = now()
    from roster_seed rs
    join workforce.agent_profiles ap on ap.employee_code = rs.employee_code
   where rs.is_head
     and d.department_code = rs.department_code;

  -- ===========================================================================
  -- 3) risk-management / qa-department / hr-department 의 legacy 페르소나·worker
  --    RETIRE — 2026-08-06/07 tool 강등 이후 각 config.yaml 의 workers:/
  --    deterministic_workers: 블록에서 이미 빠진 직원들이다.
  -- ===========================================================================
  update workforce.agent_profiles
     set employment_status = 'RETIRED',
         updated_at = now()
   where employee_code in (
     -- risk-management: risk-runner로 흡수
     'RSK-01', 'RSK-02', 'RSK-04', 'RSK-05', 'RSK-06',
     'market-liquidity-worker', 'pre-trade-risk-worker', 'derivatives-counterparty-worker',
     -- qa-department: qa-runner로 흡수 (QAA-02/07은 각각 hallucination-critic-worker/
     -- incident-postmortem-worker로 대체됐고 그 worker는 유지)
     'QAA-01', 'QAA-02', 'QAA-03', 'QAA-04', 'QAA-05', 'QAA-06', 'QAA-07',
     'evidence-qa-worker', 'model-and-internal-audit-worker', 'ops-and-permission-worker',
     -- hr-department: 결정론 코드(scorecard/quality.py, lifecycle/access.py,
     -- improvements/workflow.py)로 흡수됐거나 profile-architecture-worker로 대체
     'HR-01', 'HR-02', 'HR-03', 'HR-04'
   );

  update workforce.agent_profile_versions v
     set status = 'RETIRED',
         effective_to = now()
    from workforce.agent_profiles p
   where v.agent_id = p.agent_id
     and p.employee_code in (
       'RSK-01', 'RSK-02', 'RSK-04', 'RSK-05', 'RSK-06',
       'market-liquidity-worker', 'pre-trade-risk-worker', 'derivatives-counterparty-worker',
       'QAA-01', 'QAA-02', 'QAA-03', 'QAA-04', 'QAA-05', 'QAA-06', 'QAA-07',
       'evidence-qa-worker', 'model-and-internal-audit-worker', 'ops-and-permission-worker',
       'HR-01', 'HR-02', 'HR-03', 'HR-04'
     )
     and v.status not in ('RETIRED');
end;
$$;

commit;
