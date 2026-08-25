from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SUPABASE_MIGRATIONS = ROOT / "supabase" / "migrations"
TIMESCALE_MIGRATIONS = ROOT / "timescaledb" / "migrations"


def read_sql_files(directory: Path) -> list[tuple[Path, str]]:
    return [(path, path.read_text(encoding="utf-8")) for path in sorted(directory.glob("*.sql"))]


def created_tables(sql: str) -> set[tuple[str, str]]:
    return set(
        re.findall(
            r"(?im)^create\s+table\s+(?:if\s+not\s+exists\s+)?([a-z_][a-z0-9_]*)\.([a-z_][a-z0-9_]*)",
            sql,
        )
    )


class SupabaseSchemaContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.files = read_sql_files(SUPABASE_MIGRATIONS)
        cls.sql = "\n".join(content for _, content in cls.files)
        cls.tables = created_tables(cls.sql)

    def test_migration_sequence_is_complete(self) -> None:
        expected = [
                "20260729000100_foundation_reference.sql",
                "20260729000200_governance_workforce.sql",
                "20260729000300_research_quant_strategy.sql",
                "20260729000400_execution_risk_accounting.sql",
                "20260729000500_audit_api_security.sql",
                "20260730000600_workforce_improvement_candidates.sql",
                "20260731000700_workforce_access_lifecycle.sql",
                "20260731000701_news_ingest_latency.sql",
                "20260731000800_workforce_plan_quality_probation.sql",
                "20260731000801_news_recency_weight.sql",
                "20260731000900_public_dashboard_views.sql",
                "20260731001000_qa_decisions_reproducibility.sql",
                "20260731001100_dash_news_published_date.sql",
                "20260801001200_evidence_chunk_embedding_1024.sql",
                "20260801001300_research_pipeline_runs.sql",
                "20260802001400_research_packet_outcomes.sql",
                "20260802001500_research_collector_runs.sql",
                "20260802001600_risk_qa_runtime_registration.sql",
                "20260802001700_evidence_embedding_1024_match.sql",
                "20260802001800_risk_qa_p1_rls.sql",
                "20260802001900_research_daily_labels.sql",
                "20260802002000_research_symbol_restrictions.sql",
                "20260802002100_risk_qa_run_log_replay.sql",
                # 리서치 (재일, 2026-08-02~03)
                "20260802002200_research_as_known_at.sql",
                "20260803002300_research_claim_forecast.sql",
                "20260803002400_research_document_revisions.sql",
                "20260803002500_research_production_authorized.sql",
                "20260804000100_align_current_runtime_models.sql",
                # CEO Office / 인사팀 (영주, 2026-08-04)
                "20260804000200_governance_case_status.sql",
                "20260804000300_unify_department_code.sql",
                "20260804000400_risk_qa_runtime_activation.sql",
                "20260804000500_api_accounting_read_views.sql",
                "20260804001000_quant_hypothesis_inconclusive.sql",
                "20260804001100_order_events_broker_id_unique.sql",
                # ⚠ 아래 001150·001250 은 원래 001100·001200 으로 **버전이 겹쳤다**
                # (2026-08-12 감사에서 4쌍 발견). 위 000300 사례와 같은 처리 -
                # supabase_migrations 이력에 기록된 쪽(order_events / risk_qa)을 남기고
                # 퀀트 쪽을 빈 슬롯으로 옮겼다. 둘 다 `if not exists` 가드가 있어
                # 기존 DB 에서 재실행돼도 안전한 것을 확인한 뒤 옮겼다.
                "20260804001150_quant_preregistration.sql",
                "20260804001200_risk_qa_production_read_paths.sql",
                "20260804001250_quant_status_changed_at.sql",
                "20260804001300_quant_experiment_jobs.sql",
                "20260804001400_quant_trial_family.sql",
                # CEO Office (영주, 2026-08-05) - P0-2 GOV-02 Replay가 실 DB로 잡은 버그 수정
                "20260805000100_notifications_dedup_key_per_channel.sql",
                # 위 마이그레이션의 ADD CONSTRAINT가 재실행에 안전하지 않아 병합 후
                # Supabase 자동 적용이 "already exists"로 실패한 것을 고치는 후속 마이그레이션
                "20260805000200_notifications_dedup_key_constraint_idempotent.sql",
                # 000200 병합 후에도 재발 - 진짜 원인은 supabase_migrations.schema_migrations
                # 이력 누락이었다(수동 수복 완료). 방어적 재확인 + 사고 기록
                "20260805000300_notifications_dedup_key_history_repair_note.sql",
                # HR-03 P1-1: Eval HOLD 종료와 후보별 관찰 Scorecard
                "20260806000100_workforce_improvement_hold_and_scorecards.sql",
                # HR-04 P1-2: quality_snapshots에 누락됐던 recorded_by 추가
                "20260806000200_workforce_quality_snapshot_recorded_by.sql",
                # 도현 2026-08-06: Transactional Outbox + consumer idempotency (P0-2, PLAT-03)
                # ⚠ 원래 20260806000100 이었으나 머지에서 HR-03 과 **버전이 겹쳤다.**
                # Supabase 는 앞 14자리를 version 으로 쓰므로 중복이면 하나가 "이미 적용됨"
                # 으로 조용히 건너뛴다. 아직 main 에 없던 이쪽을 000300 으로 옮겼다.
                "20260806000300_execution_outbox.sql",
                "20260809000100_qa_eval_results_append_only.sql",
                # HR: hiring_request.py가 requested_by/decided_by/decided_at/decision_reason으로
                # 요청자-승인자 분리(마스터플랜 4.3절 자기승인 금지)를 강제하려 추가
                "20260810000100_workforce_hiring_requests_requester_decision.sql",
                # 리서치 전략 공장. 원래 20260810000100 으로 인사팀 것과 겹쳤다 - 000150 으로 옮김
                "20260810000150_research_strategy_factory.sql",
                "20260810000200_quant_hypothesis_lineage.sql",
                "20260810000300_quant_one_hypothesis_per_proposal.sql",
                # ▶ 시세 마이그레이션 2건은 여기 있으면 안 됐다 (2026-08-14)
                #   `market` 스키마는 **TimescaleDB** 에 있고 Supabase 에는 없다.
                #   그런데 `20260810000400_market_pit_provenance` /
                #   `..000500_market_received_at_nullable` 이 이 목록에 있어서,
                #   `supabase db push` 를 돌리면 없는 스키마에 DDL 을 날려 거기서
                #   멈춘다. CLAUDE.md 규약대로(운영 DB=supabase/, 시계열=
                #   timescaledb/) `timescaledb/migrations/002·003` 으로 옮겼다.
                #   둘 다 TimescaleDB 에는 이미 적용돼 있다(실측 확인).
                # 회계: 보수 발생주의 계정 3개(2100/5200/5300). 거래 수수료(5000)와
                # 섞으면 TCA가 집행 비용과 운용 보수를 분리하지 못한다
                "20260811000100_accounting_fee_accounts.sql",
                # 제안 프롬프트 버전. 위 회계 것과 겹쳤다 - 000150 으로 옮김
                "20260811000150_proposal_prompt_versions.sql",
                "20260812000100_quant_service_role.sql",
                "20260812000200_accounting_investor_profiles.sql",
                # FRACAS 폐루프(REJECT 는 근본원인·시정조치·검증창 없이 안 닫힌다)
                # + 가설을 쓴 모델·지식 컷오프 각인. 전부 additive·nullable.
                "20260813040000_fracas_and_llm_stamp.sql",
                # 가설→실행 번역에서 무엇이 사라졌는지 원장에 각인. 실측: 실험이
                # 돈 가설 41건이 config 19개로 접혔는데 어디에도 안 남아 있었다
                "20260813070000_hypotheses_mapping_loss.sql",
                # CEO mandate 현재 설정에 요청 메타데이터를 보존한다.
                "20260814000100_mandate_current_metadata.sql",
                # Library 조회 면(뷰 3종). 공장은 적재만 하고 읽을 면이 없었다
                "20260814090000_library_read_views.sql",
                # 서가가 낙폭을 최악값으로만 요약해 "관문을 넘은 적 있다" 를
                # 감췄다. best_mdd·risk_controlled_runs 를 덧붙인다(select 전용)
                "20260814100000_shelf_risk_control_visibility.sql",
                # 데이터셋 거래대금·거래량 단위를 매니페스트가 직접 선언한다.
                # 실행면의 단위 가정과 어긋나면 백테스트 전에 중단할 수 있다.
                "20260814110000_dataset_manifest_units.sql",
                "20260815100000_methodology_lead_ast_contract.sql",
                # 판정 환류가 가설에 남은 proposal 계보를 자동 상속하고,
                # 과거 빈 행도 같은 결정론 조인으로 복구한다.
                "20260815110000_outcome_proposal_lineage.sql",
                # 공개 문헌 AST를 대조군으로 보존하고, 별도 메커니즘 파생 후보만
                # 실험 기획에 노출한다. 기존 리드는 안전하게 직접복제로 분류한다.
                "20260815120000_literature_derivation_policy.sql",
                # 같은 출처의 다른 AST 해석은 원 행을 덮지 않고 결정론적 revision
                # lead로 분기한다. 같은 해석 재수집만 mentions로 접는다.
                "20260816120000_methodology_lead_revision_lineage.sql",
                # Typed research lane + governed raw quote/trade dataset contract.
                "20260816150000_intraday_alpha_factory.sql",
                # Independent skeptic STOP/PROCEED artifacts survive MCP restarts.
                "20260816170000_proposal_review_outcomes.sql",
                # Intraday causal coverage probes stay outside experiment trials.
                "20260816180000_intraday_data_feasibility.sql",
                # quant-api's scoped role may read only the packet claims used
                # by its research seed endpoint; no research write is granted.
                "20260817000100_quant_api_seed_read.sql",
                # Append-only intraday candidate ancestry, adaptive rungs,
                # first-session exposure lockbox, and independent forward gate.
                "20260817000200_intraday_trial_lockbox.sql",
                # Authoritative forward outcome/report revisions, PASS-only QA
                # request, and a fair leased/backoff scheduler.
                "20260818000100_intraday_forward_publication_queue.sql",
                # Cross-table forward decision guards and empty-lesson-safe
                # current trial-family aggregation.
                "20260818000200_intraday_forward_semantic_guards.sql",
                # Restart-safe QA event dispatch receipts and the terminal
                # forward-work schedule nullability correction.
                "20260818000300_intraday_forward_qa_dispatch.sql",
                # Remove broad default-schema write grants so quant can only
                # append transport state through the validated handoff trigger.
                "20260818000400_intraday_forward_qa_least_privilege.sql",
                # Runtime processes explicitly reduce the shared pool login to
                # their least-privileged quant or QA service role.
                "20260818000500_runtime_service_role_selection.sql",
                # Split the QA HTTP and relay processes into distinct
                # NOLOGIN/NOBYPASSRLS roles with exact RLS surfaces.
                "20260818000600_qa_runtime_role_separation.sql",
                # Independently reproduce frozen forward reports through a
                # fenced lease API and retain one immutable verdict per request.
                "20260818000700_intraday_forward_qa_reproducer.sql",
                # Restore only the RLS operations exercised by the scoped
                # quant runtime; immutable dataset/universe catalogs stay
                # read-only and the QA transport split remains unchanged.
                "20260818000800_quant_runtime_rls_repair.sql",
                # Classify LS-flagged SPACs separately so STOCK-only evidence
                # and runtime RLS boundaries exclude acquisition vehicles.
                "20260818000900_reference_spac_instrument_type.sql",
                 # Independent QA reproduction is the final hypothesis-status
                 # authority; pending/inconclusive outcomes stay fail closed.
                 "20260818001000_intraday_qa_verdict_authority.sql",
                 # Final stock-only SUPPORTED guard: current FORWARD reference
                 # revalidation, governed daily evidence, and durable SPAC type.
                 "20260818001100_stock_supported_transition_guard.sql",
                 # Narrow owner-evaluated stock identity projection for the
                 # scoped quant runtime; raw instrument metadata stays private.
                 "20260818001200_quant_stock_identity_projection.sql",
                 # Fund 생성 시 보수 발생주의 계정(2100/5200/5300)을 같은
                 # 트랜잭션에서 만들고, 기존 Fund도 idempotent하게 보정한다.
                 "20260818001300_fund_fee_account_provisioning.sql",
                 # The imported 61-session completed-second archive is a
                 # distinct historical-search authority, never the live
                 # receipt-clock event manifest.
                 "20260818001400_intraday_completed_second_dataset.sql",
                 # Local fixture USER-priority PAPER directives use durable
                 # roots/proofs/legs/reservations plus a per-book barrier.
                 "20260818001500_paper_user_directive_execution.sql",
                 # User authority is durably bound before CEO/Kanban/Hermes
                 # interpretation and only the trusted PAPER orchestrator may
                 # attach the resulting directive.
                 "20260818001600_ceo_hermes_paper_order_workflow.sql",
                 # Trading may verify canonical Risk evidence but remains
                 # unable to author or mutate Risk-owned state.
                 "20260818001700_trading_runtime_risk_read.sql",
                 # PAPER order payloads and legs accept canonical six-character
                 # uppercase alphanumeric KRX stock codes.
                 "20260819000100_krx_alphanumeric_trading_symbols.sql",
                 # Factory planning sessions may read only their governed
                 # Research inputs through svc_quant.
                 "20260819000200_factory_autopilot_research_read.sql",
                 # Dataset publication uses a dedicated NOINHERIT role rather
                 # than broadening the quant planning session.
                 "20260819000300_dataset_builder_runtime_role.sql",
                 # factory_autopilot reads the governed research queue it
                 # measures through svc_quant and gains no research write path.
                 "20260820000100_factory_quant_research_reads.sql",
                 # Only the PUBLISHED -> ACCEPTED/REJECTED lifecycle status may
                 # move; the immutable research payload stays read-only.
                 "20260820000200_factory_proposal_lifecycle_status.sql",
                 # Authenticated standing PAPER rules are a separate authority
                 # from immediate USER_DIRECTIVE requests and automated
                 # strategy OrderIntents.
                 "20260820000300_conditional_paper_rules.sql",
                 # 사용자 주문 요청과 브로커 주문 식별자를 상관키로 묶는다.
                 "20260820000400_user_order_broker_correlation.sql",
                 # LS PAPER 브로커의 주문 접수 응답을 멱등하게 기록한다.
                 "20260820000500_ls_paper_broker_order_ack.sql",
                 # 8개 부서 로스터를 재정합한다(on conflict do update).
                 "20260824000100_workforce_roster_full_reconcile.sql",
                 # MemoHarness 경험 축적 저장소. 20260824000100 과 버전이
                 # 충돌해 000150 으로 옮겼다 - 원본 000100 은 이미 hosted
                 # Supabase 에 workforce 로스터로 기록되어 있어 그쪽을
                 # 건드리면 이력이 어긋난다.
                 "20260824000150_memo_harness_experience_bank.sql",
                 # 보존 워커에게 관계 한정 delete 만 준다.
                 "20260824000200_memo_harness_retention_privileges.sql",
                 # 복합 PAPER 요청은 기존 즉시주문/조건부규칙의 합성일 뿐
                 # 두 번째 주문 원장이 아니다.
                 "20260824000600_compound_paper_order_bundles.sql",
                 # 조건부 거래 평가 경로에 읽기 권한만 준다.
                 "20260824000700_conditional_trading_evaluation_read.sql",
                 # 기존 반론을 보지 않는 skeptic 입력 계약을 구버전
                 # review cache와 분리한다.
                 "20260824000800_skeptic_review_contract_version.sql",
                 # 조건부 거래 평가의 bounded retention 인덱스와 기존
                 # worker principal 재사용 권한을 등록한다.
                 "20260824000900_conditional_rule_retention.sql",
                 # 복합 PAPER 활성화가 연결된 즉시 주문 상태를 읽을 수
                 # 있도록 조건부 워커에 요청 테이블 SELECT 정책을 준다.
                 "20260824001000_conditional_worker_bundle_request_read.sql",
                 # Strategy PAPER OMS executor role and its SECURITY DEFINER
                 # access grant are the next canonical migration pair.
                 "20260824001100_strategy_paper_oms_runtime.sql",
                 "20260824001200_strategy_paper_oms_function_grants.sql",
                 # OMS와 USER_DIRECTIVE 양쪽 outbox를 발행하되 어느 쪽에도
                 # INSERT 권한이 없는 전용 relay 역할을 둔다.
                 "20260825000100_trading_outbox_relay_role.sql",
                 # 조건주문 알림 소비자가 최소 outbox payload를 기존 사용자
                 # 요청 권위와 읽기 전용으로 다시 연결할 수 있게 한다.
                 "20260825000200_conditional_notification_context_read.sql",
                 # cost_snapshots 에 writer 를 붙이면서 보고자(recorded_by)와
                 # 같은 창 재보고를 갱신으로 접는 unique key 를 추가한다 -
                 # reader 가 창 안을 합산하므로 중복 행은 곧 사용량 2배다.
                 # 아래 4개는 원래 000100~000400 이었다. main 이 같은 날짜
                 # 000100/000200 을 먼저 쓰면서 Supabase 가 같은 version 으로
                 # 보는 충돌이 생겨 000300~000600 으로 옮겼다(20260824000150
                 # 과 같은 이유). 미적용 상태였으므로 옮기는 쪽이 이쪽이다.
                 "20260825000300_workforce_cost_snapshot_writer.sql",
                 # capacity_snapshots 에도 같은 이유로 writer 를 붙인다 - reader가
                 # 창 안에서 최신 1건을 고르므로 재보고는 갱신이어야 한다.
                 # department_id/agent_id 는 하나만 있어도 되므로 nulls not distinct
                 # unique index 를 쓴다.
                 "20260825000400_workforce_capacity_snapshot_writer.sql",
                 # performance_reviews 에 writer 를 붙이면서 decision 값 어휘를
                 # 앱 계약(review.py ReviewDecision)과 같은 check 로 고정하고,
                 # 형제 테이블에 다 있는 작성자 칸(reviewer)을 추가한다.
                 "20260825000500_workforce_performance_review_writer.sql",
                 # 같은 Agent 에 열린 수습은 하나뿐이다 - 기준(success_metrics)을
                 # 미리 고정한다는 규칙이 의미를 가지려면 그 기준이 하나여야 한다.
                 # 행 하나만 보는 check 로는 못 막아 부분 unique index 를 쓴다.
                 "20260825000600_workforce_probation_single_open.sql",
         ]
        self.assertEqual([path.name for path, _ in self.files], expected)

    def test_migrations_are_transactional(self) -> None:
        """마이그레이션은 통째로 적용되거나 통째로 안 된다.

        ▶ 왜 별도 시험인가 (2026-08-14 실측)
          이 검사는 원래 `test_migration_sequence_is_complete` 꼬리에 붙어
          있었다. 그런데 목록 대조가 **먼저** 터지면 거기서 시험이 끝나
          트랜잭션 검사는 아예 안 돈다. 실제로 새 마이그레이션 3개가 목록에
          없었고, 그 셋이 동시에 `begin;`/`commit;` 도 빠져 있었는데 **한쪽이
          다른 쪽을 가려서** 안 보였다(56개 중 그 셋만 안 감싸져 있었다).

          관문이 둘이면 따로 세운다 - 하나가 터졌다고 나머지를 못 보면
          그 나머지는 있으나 마나다.
        """
        for path, sql in self.files:
            with self.subTest(path=path.name):
                self.assertRegex(sql.lstrip().lower(), r"^begin;")
                self.assertRegex(sql.rstrip().lower(), r"commit;$")

    def test_fee_account_seed_respects_fund_scoped_uniqueness(self) -> None:
        """A clean database must replay the fee-account migration.

        ``accounting.ledger_accounts`` has a required ``fund_id`` and its
        unique key is ``(fund_id, account_code)``.  A global account-code
        conflict target is neither valid PostgreSQL nor the ledger contract.
        """
        migration = (SUPABASE_MIGRATIONS /
                     "20260811000100_accounting_fee_accounts.sql").read_text(
                         encoding="utf-8").lower()
        self.assertIn("(fund_id, account_code, name, account_type, currency)",
                      migration)
        self.assertIn("from accounting.funds", migration)
        self.assertIn("on conflict (fund_id, account_code) do nothing",
                      migration)
        self.assertNotIn("on conflict (account_code)", migration)

        foundation = (
            SUPABASE_MIGRATIONS / "20260729000100_foundation_reference.sql"
        ).read_text(encoding="utf-8").lower()
        profile_start = foundation.index("create table governance.user_profiles")
        profile_end = foundation.index(
            "create table governance.user_preferences", profile_start
        )
        profile_ddl = foundation[profile_start:profile_end]
        self.assertIn("user_id uuid primary key", profile_ddl)
        self.assertNotIn("auth.users", profile_ddl)

    def test_private_control_db_bootstraps_inert_postgrest_grant_targets(self) -> None:
        foundation = (
            SUPABASE_MIGRATIONS / "20260729000100_foundation_reference.sql"
        ).read_text(encoding="utf-8").lower()
        for role in ("anon", "authenticated", "service_role"):
            self.assertIn(f"create role {role} nologin noinherit", foundation)
        self.assertIn("nobypassrls", foundation)

        risk_activation = (
            SUPABASE_MIGRATIONS
            / "20260804000400_risk_qa_runtime_activation.sql"
        ).read_text(encoding="utf-8").lower()
        self.assertNotIn("auth.role()", risk_activation)
        self.assertNotIn("create policy risk_input_snapshots_service_role_all", risk_activation)

    def test_runtime_pool_role_selection_is_login_name_neutral(self) -> None:
        for filename in (
            "20260818000500_runtime_service_role_selection.sql",
            "20260818000600_qa_runtime_role_separation.sql",
            "20260818000700_intraday_forward_qa_reproducer.sql",
        ):
            migration = (SUPABASE_MIGRATIONS / filename).read_text(
                encoding="utf-8"
            ).lower()
            self.assertIn("session_user", migration)
            self.assertNotIn("to postgres with set true", migration)
            self.assertNotIn("member_role.rolname = 'postgres'", migration)

    def test_migration_versions_are_unique(self) -> None:
        """버전 접두사(타임스탬프)가 겹치면 Supabase 가 적용을 꼬아 Preview 가
        깨진다 - 2026-07-31 실측: 같은 날 두 사람이 각각 000700/000800 을 잡아
        'access_requests already exists' 로 전 커밋이 실패했다. 파일 이름이
        달라도 접두사가 같으면 같은 버전이다."""
        versions = [path.name.split("_", 1)[0] for path, _ in self.files]
        dup = {v for v in versions if versions.count(v) > 1}
        self.assertFalse(
            dup,
            f"마이그레이션 버전 접두사 중복: {sorted(dup)} - 새 파일을 만들기 전에 "
            f"`ls supabase/migrations | tail` 로 마지막 번호를 확인할 것",
        )

    def test_order_event_constraint_migration_is_idempotent(self) -> None:
        migration = next(
            sql
            for path, sql in self.files
            if path.name == "20260804001100_order_events_broker_id_unique.sql"
        ).lower()
        self.assertIn(
            "drop constraint if exists order_events_broker_event_unique",
            migration,
        )

    def test_notifications_dedup_key_constraint_migration_is_idempotent(self) -> None:
        """2026-08-05 실측: ADD CONSTRAINT만 있고 DROP CONSTRAINT IF EXISTS가 없는
        마이그레이션을 개발 DB에 먼저 수동 적용한 뒤 그대로 커밋했더니, 병합 후
        Supabase 자동 마이그레이션 적용기가 재실행하면서 "already exists"로 실패했다
        (order_events_broker_id_unique.sql과 같은 종류의 실수 - 위 테스트와 같은
        이유로 재발 방지)."""
        migration = next(
            sql
            for path, sql in self.files
            if path.name == "20260805000200_notifications_dedup_key_constraint_idempotent.sql"
        ).lower()
        self.assertIn(
            "drop constraint if exists notifications_dedup_key_channel_unique",
            migration,
        )

    def test_improvement_hold_constraint_migration_is_idempotent(self) -> None:
        migration = next(
            sql
            for path, sql in self.files
            if path.name == "20260806000100_workforce_improvement_hold_and_scorecards.sql"
        ).lower()
        self.assertIn("drop constraint if exists improvement_candidates_status_check", migration)
        self.assertIn("drop trigger if exists improvement_candidate_scorecards_append_only", migration)
        self.assertIn("'hold'", migration)

    def test_qa_eval_migration_hardening_contract(self) -> None:
        migration = next(
            sql
            for path, sql in self.files
            if path.name == "20260809000100_qa_eval_results_append_only.sql"
        ).lower()

        # Existing eval_runs rows must be repaired and explicitly validated
        # before identity NOT NULL/CHECK constraints are installed.
        self.assertIn("update audit.eval_runs", migration)
        self.assertIn("nullif(btrim(candidate_id), '')", migration)
        self.assertIn("nullif(btrim(candidate_profile_version), '')", migration)
        self.assertIn("raise exception using", migration)
        self.assertIn("alter column candidate_id set not null", migration)
        self.assertIn("alter column candidate_profile_version set not null", migration)

        self.assertIn("eval_runs_environment_check", migration)
        self.assertIn("check (environment in ('shadow', 'mock'))", migration)
        self.assertIn("alter column environment set not null", migration)

        for table in ("eval_runs", "eval_results", "eval_comparisons"):
            self.assertIn(f"alter table audit.{table} enable row level security", migration)
        self.assertIn("validate_eval_run_transition", migration)
        self.assertIn("eval_runs_lifecycle_guard", migration)
        self.assertIn("on delete restrict", migration)
        self.assertIn("eval_results_append_only", migration)
        self.assertIn("eval_comparisons_append_only", migration)

    def test_intraday_trial_lockbox_contract(self) -> None:
        migration = next(
            sql
            for path, sql in self.files
            if path.name == "20260817000200_intraday_trial_lockbox.sql"
        ).lower()

        tables = (
            "intraday_candidate_lineages",
            "intraday_experiment_rungs",
            "intraday_session_accesses",
            "intraday_session_exposures",
            "intraday_forward_confirmations",
            "intraday_report_manifests",
        )
        for table in tables:
            with self.subTest(table=table):
                self.assertIn(f"create table quant.{table}", migration)
                self.assertIn(
                    f"alter table quant.{table} enable row level security",
                    migration,
                )
                self.assertIn(f"{table}_append_only", migration)

        self.assertIn("uq_intraday_root_session_exposure", migration)
        self.assertIn("unique\n    (root_lineage_id, session_date)", migration)
        self.assertIn("fk_intraday_candidate_parent_root", migration)
        self.assertIn("deferrable initially deferred", migration)
        self.assertIn("validate_intraday_rung_allocation", migration)
        self.assertIn("validate_intraday_session_access", migration)
        self.assertIn("validate_intraday_session_exposure", migration)
        self.assertIn("validate_intraday_forward_confirmation", migration)
        self.assertGreaterEqual(migration.count("pg_advisory_xact_lock"), 3)
        self.assertIn("'calibration', 'discovery_6'", migration)
        self.assertIn("when 'discovery_6' then 'calibration'", migration)
        self.assertIn("calibration rung requires calibration exposure purpose", migration)
        self.assertIn("forward exposure requires new arrival-time-causal evidence", migration)
        self.assertIn("actual_raw_replay_v1", migration)
        self.assertIn("event_time_historical_only", migration)
        self.assertIn("arrival_time_causal", migration)
        self.assertIn("rung = 'discovery_6' and planned_session_count = 6", migration)
        self.assertIn("rung = 'validation_20' and planned_session_count = 20", migration)
        self.assertIn("rung = 'full_60' and planned_session_count = 60", migration)
        self.assertIn("rung = 'forward' and planned_session_count >= 20", migration)
        self.assertIn("forward_session_count >= 20", migration)
        self.assertIn("intraday_forward_test_index_seq", migration)
        self.assertIn("uq_intraday_forward_test_index", migration)
        self.assertIn("forward test index is database assigned", migration)
        self.assertIn("planned_instrument_ids", migration)
        self.assertIn("planned intraday instruments must be sorted, unique, and exact", migration)
        self.assertIn("session exposure differs from the exact frozen instrument universe", migration)
        self.assertIn("prior rung of the same experiment and candidate lineage", migration)
        self.assertIn("validation and full rungs must contain every prior search session", migration)
        self.assertIn("forward allocation requires complete full_60 exposure evidence", migration)
        self.assertNotIn("uq_intraday_forward_candidate unique", migration)
        self.assertIn("revoke update, delete, truncate on", migration)

        # Full governance JSON is append-only but deliberately absent from the
        # indexed experiment_metrics.dimensions value.
        self.assertIn("create table quant.intraday_report_manifests", migration)
        self.assertRegex(migration, r"\breport\s+jsonb\s+not\s+null\b")
        self.assertIn("experiment_metrics stores only its compact sha-256 reference", migration)

        # There is intentionally no migration-time claim that the historical
        # 61 sessions were unused. Runtime must append first exposure facts.
        self.assertNotIn("insert into quant.intraday_session_exposures", migration)
        self.assertNotIn("insert into quant.intraday_session_accesses", migration)
        self.assertIn("matching durable pre-read access marker", migration)
        self.assertIn("new.knowledge_cutoff <> declared_rung.dataset_cutoff", migration)

        # Stock-only universe metadata is a column-scoped, RLS-filtered read;
        # missing metadata and non-STOCK products fail closed.
        self.assertIn("grant usage on schema reference to svc_quant", migration)
        self.assertIn("grant usage on schema reference, quant to service_role", migration)
        self.assertIn("grant select (", migration)
        self.assertIn("instrument_id, instrument_type, asset_class, market", migration)
        self.assertIn("market, venue", migration)
        self.assertIn("reference_instruments_svc_quant_stock_only_select", migration)
        self.assertIn("upper(instrument_type) = 'stock'", migration)
        self.assertIn("market_calendar_versions_svc_quant_krx_select", migration)
        self.assertIn("market_sessions_svc_quant_krx_select", migration)
        self.assertIn("on reference.market_calendar_versions to svc_quant", migration)
        self.assertIn("on reference.market_sessions to svc_quant", migration)

    def test_intraday_forward_publication_and_queue_contract(self) -> None:
        migration = next(
            sql
            for path, sql in self.files
            if path.name ==
            "20260818000100_intraday_forward_publication_queue.sql"
        ).lower()

        immutable = (
            "research.experiment_outcome_revisions",
            "quant.intraday_forward_report_revisions",
            "quant.intraday_forward_qa_handoffs",
        )
        for table in immutable:
            schema, name = table.split(".")
            self.assertIn(f"create table {table}", migration)
            self.assertIn(f"alter table {table} enable row level security",
                          migration)
            self.assertIn(f"{name}_append_only", migration)
        self.assertIn("create view research.v_current_experiment_outcomes",
                      migration)
        self.assertIn("fk_intraday_forward_report_outcome", migration)
        self.assertIn("fk_intraday_forward_qa_report", migration)
        self.assertIn("uq_intraday_forward_qa_confirmation", migration)

        self.assertIn("create table quant.intraday_forward_work_items",
                      migration)
        self.assertIn("lease_token", migration)
        self.assertIn("next_attempt_at", migration)
        self.assertIn("error_count", migration)
        self.assertIn("max_error_count", migration)
        self.assertIn("'failed'", migration)
        self.assertIn("idx_intraday_forward_work_due", migration)
        self.assertIn("idx_intraday_forward_work_expired_lease", migration)
        self.assertNotIn("intraday_forward_work_items_append_only", migration)

        self.assertIn("experiment_outcome_revisions_svc_quant_insert",
                      migration)
        self.assertIn("intraday_forward_work_items_svc_quant_update",
                      migration)
        self.assertIn("hypotheses_svc_quant_update", migration)
        self.assertIn("revoke update, delete, truncate", migration)

    def test_intraday_forward_semantic_guards_contract(self) -> None:
        migration = next(
            sql
            for path, sql in self.files
            if path.name ==
            "20260818000200_intraday_forward_semantic_guards.sql"
        ).lower()

        self.assertIn(
            "create or replace function "
            "quant.validate_intraday_outcome_revision()", migration)
        self.assertIn(
            "create or replace function "
            "quant.validate_intraday_forward_report_revision()", migration)
        self.assertIn(
            "intraday_forward_report_revision_semantic_guard", migration)
        self.assertIn("new.decision is distinct from confirmation_decision",
                      migration)
        self.assertIn(
            "outcome_decision is distinct from expected_outcome_decision",
            migration)
        self.assertIn(
            "new.hypothesis_status is distinct from "
            "expected_hypothesis_status", migration)
        for expected in ("'pass' then 'submit_to_qa'",
                         "'fail' then 'reject'",
                         "'inconclusive' then 'gate_hold'",
                         "'pass' then 'supported'",
                         "'fail' then 'rejected'"):
            self.assertIn(expected, migration)

        self.assertIn(
            "create or replace function "
            "quant.validate_intraday_forward_qa_handoff()", migration)
        self.assertIn("intraday_forward_qa_handoff_pass_guard", migration)
        self.assertIn("report.decision = 'pass'", migration)
        self.assertIn("report.hypothesis_status = 'supported'", migration)
        self.assertIn("do $semantic_audit$", migration)
        self.assertIn(
            "existing forward outcome revision lacks complete semantic "
            "identity", migration)
        self.assertIn(
            "left join quant.intraday_forward_confirmations confirmation",
            migration)
        self.assertIn(
            "left join research.experiment_outcome_revisions outcome",
            migration)
        self.assertIn(
            "existing forward publication violates semantic decision mapping",
            migration)
        self.assertIn(
            "existing qa handoff is not backed by a pass forward report",
            migration)

        self.assertIn(
            "create or replace view research.v_trial_family_status",
            migration)
        self.assertIn("left join lateral unnest", migration)
        self.assertIn("coalesce(outcome.lesson_codes, '{}'::text[])",
                      migration)
        self.assertIn("count(distinct outcome.outcome_id) as outcomes",
                      migration)

    def test_intraday_forward_qa_dispatch_contract(self) -> None:
        migration = next(
            sql
            for path, sql in self.files
            if path.name ==
            "20260818000300_intraday_forward_qa_dispatch.sql"
        ).lower()

        self.assertIn(
            "alter column next_attempt_at drop not null", migration)
        self.assertIn(
            "create table quant.intraday_forward_qa_outbox", migration)
        self.assertIn(
            "create table quant.intraday_forward_qa_delivery_state", migration)
        self.assertIn(
            "create table quant.intraday_forward_qa_dispatches", migration)
        self.assertRegex(
            migration,
            r"uq_intraday_forward_qa_dispatch_handoff\s+unique\s*"
            r"\(qa_handoff_id\)")
        self.assertIn(
            "create table audit.intraday_forward_reproduction_requests",
            migration)
        self.assertIn(
            "create table audit.intraday_forward_reproduction_work_items",
            migration)
        self.assertIn(
            "quant.intraday.forward.qa_requested.v1", migration)
        self.assertIn(
            "create or replace function "
            "quant.intraday_forward_qa_event_id(", migration)
        self.assertIn(
            "event_id = quant.intraday_forward_qa_event_id(qa_handoff_id)",
            migration)
        self.assertIn(
            "message_id = event_type || ':' || qa_handoff_id::text",
            migration)
        self.assertIn("payload_fingerprint", migration)
        self.assertIn("event_payload", migration)
        self.assertNotIn("jsonb_object_length", migration)
        self.assertIn("payload_fingerprint = encode(", migration)
        self.assertIn(
            "new.payload is distinct from expected.event_payload", migration)
        self.assertIn(
            "intraday_forward_qa_handoff_transactional_outbox", migration)
        self.assertIn("do $forward_qa_backfill$", migration)
        self.assertIn(
            "intraday_forward_qa_dispatch_semantic_guard", migration)
        self.assertIn(
            "reproduction_contract->'promotion_authority' = "
            "'false'::jsonb", migration)
        self.assertIn(
            "intraday_forward_reproduction_request_semantic_guard",
            migration)
        self.assertIn(
            "intraday_forward_qa_delivery_sent_guard", migration)
        self.assertIn(
            "intraday_forward_qa_domain_event_append_only", migration)
        self.assertIn(
            "expected.domain_status is distinct from 'processed'", migration)
        self.assertIn(
            "reproduction_contract->>'asset_scope' =", migration)
        self.assertIn("'krx_active_stock_only'", migration)
        self.assertIn(
            "reproduction_contract - array[", migration)
        self.assertIn(
            "intraday_forward_qa_dispatches_append_only", migration)
        self.assertIn(
            "alter table quant.intraday_forward_qa_dispatches enable row "
            "level security", migration)
        self.assertRegex(
            migration,
            r"grant select, insert on "
            r"quant\.intraday_forward_qa_dispatches\s+to service_role")
        self.assertRegex(
            migration,
            r"grant select, insert on audit\.domain_events\s+to service_role")
        self.assertIn("grant usage on schema audit to service_role", migration)
        self.assertIn(
            "create policy "
            "reference_instrument_symbols_svc_quant_stock_only_select",
            migration)
        self.assertIn(
            "drop policy if exists "
            "reference_instruments_svc_quant_stock_only_select", migration)
        self.assertIn(
            "create policy reference_instruments_svc_quant_stock_only_select",
            migration)
        self.assertIn("intraday_rung_stock_scope_guard", migration)
        self.assertIn(
            "instrument_id, provider, market, symbol, symbol_type, is_primary",
            migration)
        for stock_boundary in (
            "upper(instrument_type) = 'stock'",
            "upper(asset_class) = 'equity'",
            "upper(market) = 'krx'",
            "upper(status) = 'active'",
            "upper(instrument.instrument_type) = 'stock'",
            "upper(instrument.asset_class) = 'equity'",
            "upper(instrument.market) = 'krx'",
            "upper(instrument.status) = 'active'",
            "instrument.listed_from <= session.session_date",
            "instrument.listed_to >= session.session_date",
        ):
            self.assertIn(stock_boundary, migration)

    def test_intraday_forward_qa_least_privilege_contract(self) -> None:
        migration = next(
            sql
            for path, sql in self.files
            if path.name ==
            "20260818000400_intraday_forward_qa_least_privilege.sql"
        ).lower()

        self.assertIn("revoke insert, update, delete, truncate on", migration)
        self.assertIn("quant.intraday_forward_qa_outbox", migration)
        self.assertIn("quant.intraday_forward_qa_delivery_state", migration)
        self.assertIn("quant.intraday_forward_qa_dispatches", migration)
        self.assertIn("revoke all on", migration)
        self.assertIn("audit.intraday_forward_reproduction_requests", migration)
        self.assertIn("audit.intraday_forward_reproduction_work_items", migration)
        self.assertIn("do $forward_qa_least_privilege_audit$", migration)
        self.assertIn(
            "svc_quant retains a direct forward qa transport or audit write "
            "path", migration)
        self.assertIn(
            "forward qa relay or acceptance role lacks its required privilege",
            migration)

    def test_runtime_service_role_selection_contract(self) -> None:
        migration = next(
            sql
            for path, sql in self.files
            if path.name ==
            "20260818000500_runtime_service_role_selection.sql"
        ).lower()

        self.assertIn("pool_login name := session_user", migration)
        self.assertIn(
            "grant svc_quant to %i with set true, inherit false", migration)
        self.assertIn("membership.set_option", migration)
        self.assertIn("not membership.inherit_option", migration)
        self.assertIn("cannot explicitly reduce to service_role", migration)
        self.assertNotIn("to postgres with set true", migration)
        self.assertIn(
            "svc_quant retains a direct qa transport write path", migration)
        self.assertIn(
            "service_role lacks the qa relay privileges", migration)

    def test_qa_runtime_role_separation_contract(self) -> None:
        migration = next(
            sql
            for path, sql in self.files
            if path.name == "20260818000600_qa_runtime_role_separation.sql"
        ).lower()

        self.assertIn("create role svc_audit_api", migration)
        self.assertIn("create role svc_qa_worker", migration)
        self.assertIn("nologin nosuperuser nocreatedb nocreaterole", migration)
        self.assertIn("noinherit", migration)
        self.assertIn("nobypassrls", migration)
        self.assertIn("pool_login name := session_user", migration)
        self.assertIn(
            "grant svc_audit_api to %i with set true, inherit false",
            migration,
        )
        self.assertIn(
            "grant svc_qa_worker to %i with set true, inherit false",
            migration,
        )
        self.assertNotIn("to postgres with set true", migration)
        self.assertIn("alter table audit.domain_events enable row level security", migration)
        self.assertIn("svc_audit_api has a non-audit direct table grant", migration)
        self.assertIn("svc_qa_worker exceeds its append/relay boundary", migration)
        self.assertIn("a qa runtime role has destructive table privilege", migration)

    def test_intraday_forward_qa_reproducer_contract(self) -> None:
        migration = next(
            sql
            for path, sql in self.files
            if path.name ==
            "20260818000700_intraday_forward_qa_reproducer.sql"
        ).lower()

        self.assertIn("create role svc_qa_reproducer", migration)
        self.assertIn("nologin nosuperuser nocreatedb nocreaterole", migration)
        self.assertIn("noinherit", migration)
        self.assertIn("nobypassrls", migration)
        self.assertIn("pool_login name := session_user", migration)
        self.assertIn(
            "grant svc_qa_reproducer to %i with set true, inherit false",
            migration,
        )
        self.assertNotIn("to postgres with set true", migration)
        self.assertIn(
            "create table audit.intraday_forward_reproduction_results",
            migration,
        )
        self.assertRegex(
            migration,
            r"reproduction_request_id\s+uuid\s+not\s+null\s+unique",
        )
        self.assertIn(
            "uq_intraday_forward_reproduction_work_identity", migration)
        self.assertIn(
            "fk_intraday_forward_reproduction_result_work_request", migration)
        self.assertIn("verdict in ('pass', 'fail', 'inconclusive')", migration)
        self.assertIn("promotion_authority = false", migration)
        self.assertIn("result_fingerprint = encode(", migration)
        self.assertIn(
            "intraday_forward_reproduction_results_append_only", migration)
        self.assertIn(
            "alter table audit.intraday_forward_reproduction_results\n"
            "  force row level security",
            migration,
        )

        for function in (
            "claim_intraday_forward_reproduction_work",
            "heartbeat_intraday_forward_reproduction_work",
            "complete_intraday_forward_reproduction_work",
            "fail_intraday_forward_reproduction_work",
        ):
            self.assertIn(f"create or replace function audit.{function}(", migration)
        self.assertRegex(
            migration,
            r"claim_intraday_forward_reproduction_work\(\s*"
            r"p_worker text,\s*p_lease_seconds integer default 900\s*\)\s*"
            r"returns jsonb",
        )
        self.assertRegex(
            migration,
            r"heartbeat_intraday_forward_reproduction_work\([\s\S]*?\)\s*"
            r"returns boolean",
        )
        self.assertRegex(
            migration,
            r"complete_intraday_forward_reproduction_work\([\s\S]*?\)\s*"
            r"returns uuid",
        )
        self.assertRegex(
            migration,
            r"fail_intraday_forward_reproduction_work\([\s\S]*?\)\s*"
            r"returns text",
        )
        self.assertGreaterEqual(migration.count("security definer"), 4)
        self.assertGreaterEqual(
            migration.count(
                "set search_path = pg_catalog, audit"
            ),
            4,
        )
        self.assertIn("for update of work skip locked", migration)
        self.assertIn("work.status in ('ready', 'retry')", migration)
        self.assertIn("work.status = 'leased'", migration)
        self.assertIn("work.lease_token = p_lease_token", migration)
        self.assertIn("work.lease_expires_at > v_now", migration)
        self.assertIn("3600", migration)
        self.assertIn("1 << least(greatest(work.attempt_count - 1, 0), 7)", migration)
        self.assertIn("set status = 'completed'", migration)
        self.assertIn("v_verdict not in ('pass', 'fail', 'inconclusive')", migration)
        self.assertIn(
            "qa reproduction completion conflicts with immutable result",
            migration,
        )

        self.assertIn(
            "intraday-forward-qa-reproduction-input-v1", migration)
        for bundle_key in (
            "'work_item'", "'request'", "'experiment'", "'candidate'",
            "'forward_rung'", "'report_revision'", "'confirmation'",
            "'session_exposures'",
        ):
            self.assertIn(bundle_key, migration)
        for frozen_field in (
            "candidate_identity_fingerprint", "candidate_ast_fingerprint",
            "semantic_plan_fingerprint", "feature_spec_fingerprint",
            "label_spec_fingerprint", "model_spec_fingerprint",
            "planned_session_dates", "planned_instrument_ids",
            "session_set_fingerprint", "instrument_set_fingerprint",
            "rung_plan_fingerprint", "dataset_cutoff", "forward_test_index",
            "session_content_fingerprint", "quote_row_count",
            "trade_row_count", "confirmation_evidence_fingerprint",
            "intraday-forward-reproduction-runtime-v1",
            "intraday-forward-reproduction-source-set-v1",
            "frozen_config_fingerprint", "experiment_input_hash",
            "runtime_manifest_fingerprint", "source_fingerprint",
            "code_version", "cost_model_version", "evaluator_version",
        ):
            self.assertIn(frozen_field, migration)
        self.assertIn("'frozen_config' <> '{}'::jsonb", migration)
        self.assertIn(
            "coalesce(experiment.input_hash ~ '^[0-9a-f]{64}$', false)",
            migration,
        )
        self.assertIn("coalesce(btrim(experiment.code_version) <> '', false)", migration)
        self.assertIn("'source_manifest' <> '{}'::jsonb", migration)
        self.assertIn("'source_manifest'->'files' <> '{}'::jsonb", migration)
        self.assertIn("order by exposure.session_date", migration)
        self.assertIn("coalesce(upper(instrument.instrument_type), '') <> 'stock'", migration)
        self.assertIn("coalesce(upper(instrument.asset_class), '') <> 'equity'", migration)
        self.assertIn("coalesce(upper(instrument.market), '') <> 'krx'", migration)
        self.assertIn("coalesce(upper(instrument.status), '') <> 'active'", migration)

        self.assertIn(
            "grant select on audit.intraday_forward_reproduction_results",
            migration,
        )
        self.assertIn("grant execute on function", migration)
        self.assertIn("to svc_qa_reproducer", migration)
        self.assertIn("do $qa_reproducer_privilege_audit$", migration)
        self.assertIn(
            "svc_qa_reproducer has an unexpected direct table grant",
            migration,
        )
        self.assertIn(
            "qa reproducer or transport worker crosses its boundary",
            migration,
        )

    def test_intraday_qa_verdict_authority_contract(self) -> None:
        migration = next(
            sql
            for path, sql in self.files
            if path.name ==
            "20260818001000_intraday_qa_verdict_authority.sql"
        ).lower()

        # Authority is aggregated per hypothesis.  No single experiment may
        # win merely because its QA result was inserted last.
        self.assertRegex(
            migration,
            r"audit\.intraday_forward_qa_hypothesis_authority"
            r"\(p_hypothesis_id uuid\)\s*returns table \("
            r"[\s\S]*?language sql\s*security definer\s*"
            r"set search_path = pg_catalog, audit, quant",
        )
        self.assertIn("when count(*) filter (where verdict = 'fail') > 0",
                      migration)
        self.assertIn("where verdict is null or verdict = 'inconclusive'",
                      migration)
        self.assertIn(
            "when count(*) filter (where verdict = 'pass') = count(*)",
            migration,
        )

        # A PASS report immediately creates an INCONCLUSIVE obligation; the
        # immutable result later re-evaluates the same aggregate.  Both paths
        # serialize on the mutable hypothesis row and preserve ARCHIVED.
        self.assertIn(
            "create or replace function "
            "audit.apply_intraday_forward_qa_authority()",
            migration,
        )
        self.assertIn("for update", migration)
        self.assertIn("if v_current_status = 'archived'", migration)
        for trigger_table in (
            "after insert on audit.intraday_forward_reproduction_results",
            "after insert on quant.intraday_forward_report_revisions",
        ):
            self.assertIn(trigger_table, migration)
        self.assertGreaterEqual(
            migration.count(
                "execute function audit.apply_intraday_forward_qa_authority()"
            ),
            2,
        )

        # Old publishers cannot write optimistic support after creating the
        # pending obligation.  Runtime roles cannot call the definer helpers.
        self.assertIn(
            "create or replace function "
            "audit.guard_intraday_forward_qa_support()",
            migration,
        )
        self.assertIn("before update of status on quant.hypotheses", migration)
        self.assertIn(
            "hypothesis support is blocked by aggregate forward qa status",
            migration,
        )
        for role in ("public", "anon", "authenticated", "service_role",
                     "svc_quant", "svc_qa_worker", "svc_audit_api",
                     "svc_qa_reproducer"):
            self.assertIn(role, migration)

        # One deterministic backfill repairs both pending legacy publications
        # and pre-trigger results without last-row-wins UPDATE FROM behavior.
        self.assertIn("with governed_hypotheses as", migration)
        self.assertIn("cross join lateral", migration)
        self.assertIn("and hypothesis.status <> 'archived'", migration)
        self.assertIn(
            "and hypothesis.status is distinct from authority.status",
            migration,
        )

        # The current-outcome projection exposes a positive decision only for
        # QA PASS. FAIL is negative; both PENDING and INCONCLUSIVE are BLOCKED.
        self.assertIn(
            "create or replace view research.v_current_experiment_outcomes",
            migration,
        )
        self.assertRegex(
            migration,
            r"when revision\.decision = 'submit_to_qa' then\s*"
            r"case qa_result\.verdict\s*"
            r"when 'pass' then 'submit_to_qa'\s*"
            r"when 'fail' then 'reject'\s*"
            r"else 'blocked'\s*end",
        )
        self.assertIn("'qa_reproduction_inconclusive'", migration)
        self.assertIn("'qa_reproduction_pending'", migration)

        # Only a reproduced PASS exposes alpha-like metrics. FAIL remains a
        # negative structural verdict, while its unreproduced point estimates
        # stay available only in the immutable source report.
        self.assertRegex(
            migration,
            r"when qa_result\.verdict = 'pass'\s*"
            r"then base\.oos_summary \|\| coalesce\(\s*"
            r"revision\.oos_summary, '\{\}'::jsonb\)\s*"
            r"else \(base\.oos_summary \|\| coalesce\(\s*"
            r"revision\.oos_summary, '\{\}'::jsonb\)\)\s*"
            r"- 'mean_net_bps_per_opportunity'\s*"
            r"- 'mean_mid_markout_bps'\s*"
            r"- 'sharpe'\s*"
            r"- 'deflated_sharpe'",
        )
        self.assertIn("'status', coalesce(qa_result.verdict, 'pending')", migration)
        self.assertIn(
            "'qa_verified', coalesce(qa_result.verdict = 'pass', false)",
            migration,
        )
        self.assertIn("'promotion_authority', false", migration)

    def test_domain_schemas_and_table_counts(self) -> None:
        expected_counts = {
            "accounting": 19,
            # +1 (2026-08-18): immutable independent intraday forward
            # reproduction verdicts, one per accepted QA request.
            "audit": 25,
            # +1 (QA, 2026-08-09): eval_comparisons stores immutable Champion
            # comparison evidence across API/process restarts.
            # +2 (도현, 2026-08-06): outbox(Transactional Outbox — OMS 상태 변경과 같은
            # 트랜잭션에서 기록), outbox_consumed(소비자별 중복 제거). P0-2 / PLAT-03
            # +6 (2026-08-18): authenticated PAPER directive roots, consumed
            # proofs, execution/cancel legs, resource reservations, the
            # per-book priority barrier, and direct fill evidence.
            # +3 (2026-08-18): durable CEO/Hermes PAPER-order requests,
            # append-only interpretations, and transition audit events.
            # +7: 20260820000300_conditional_paper_rules.sql
            # +1: 20260824000600_compound_paper_order_bundles.sql — 복합
            # PAPER 요청은 기존 즉시주문/조건부규칙의 합성이며 두 번째
            # 주문 원장이 아니다.
            "execution": 31,
            "governance": 20,
            # +1 (재일, 2026-08-10): 공장 재편으로 실험 사전등록/결과 원장 확장
            # +1 (재일, 2026-08-16): 사전 데이터 타당성 점검을 trial에서 분리
            # +6 (2026-08-17): intraday candidate ancestry, immutable resource
            # rungs, durable pre-read access, post-read evidence, forward
            # confirmations, and full report manifests outside indexed JSONB.
            # +4 (2026-08-18): immutable forward report/QA request/dispatch
            # receipt plus the separate mutable fair scheduler.
            "quant": 26,
            "reference": 9,
            # +2 (재일, 2026-08-03): claim_evidence(주장↔근거 인용 링크),
            # document_revisions(뉴스 정정 이력 - 저장본은 PIT 상 최초 관측
            # 문장을 유지하므로 정정 사실은 이 테이블이 유일한 흔적이다)
            # +1 (재일, 2026-08-16): durable independent proposal review outcomes.
            # +1 (2026-08-18): append-only authoritative outcome revisions;
            # legacy outcomes remain one row per experiment.
            "research": 28,
            "risk": 19,
            "strategy": 9,
            "workforce": 25,
        }
        actual_counts = {
            schema: sum(1 for table_schema, _ in self.tables if table_schema == schema)
            for schema in expected_counts
        }
        self.assertEqual(actual_counts, expected_counts)
        self.assertNotIn("public", {schema for schema, _ in self.tables})

    def test_critical_end_to_end_entities_exist(self) -> None:
        required = {
            ("reference", "instruments"),
            ("research", "documents"),
            ("research", "document_versions"),
            ("research", "evidence_chunks"),
            ("governance", "cases"),
            ("governance", "case_events"),
            ("strategy", "versions"),
            ("strategy", "signals"),
            ("execution", "intent_groups"),
            ("execution", "order_intents"),
            ("risk", "risk_requests"),
            ("risk", "risk_decisions"),
            ("execution", "orders"),
            ("execution", "fills"),
            ("accounting", "journals"),
            ("accounting", "journal_lines"),
            ("accounting", "positions"),
            ("accounting", "nav_runs"),
            ("audit", "traces"),
            ("audit", "agent_runs"),
            ("audit", "run_log_events"),
            ("risk", "run_log_events"),
            ("workforce", "agent_profile_versions"),
        }
        self.assertTrue(required.issubset(self.tables), required - self.tables)

    def test_market_raw_data_is_not_stored_in_supabase(self) -> None:
        forbidden = {
            ("public", "market_ticks"),
            ("research", "market_ticks"),
            ("quant", "market_ticks"),
            ("public", "market_quotes"),
            ("research", "market_quotes"),
            ("quant", "market_quotes"),
        }
        self.assertTrue(self.tables.isdisjoint(forbidden))
        self.assertNotRegex(self.sql.lower(), r"references\s+market\.")

    def test_database_enforces_critical_controls(self) -> None:
        controls = [
            "validate_order_state_transition",
            "protect_posted_journal_lines",
            "protect_posted_journal",
            "validate_journal_posting",
            "reject_append_only_change",
            "case_events_append_only",
            "order_events_append_only",
            "fills_append_only",
            "tool_calls_append_only",
        ]
        for control in controls:
            with self.subTest(control=control):
                self.assertIn(control, self.sql)

        self.assertRegex(
            self.sql,
            r"(?is)filled_quantity\s+numeric.*?check\s*\(filled_quantity\s*<=\s*requested_quantity\)",
        )
        self.assertIn("base_debit numeric", self.sql)
        self.assertIn("base_credit numeric", self.sql)
        self.assertIn("imbalance <> 0", self.sql)

    def test_security_boundary_and_api_surface_exist(self) -> None:
        self.assertIn("enable row level security", self.sql.lower())
        self.assertIn("revoke all on all tables in schema", self.sql.lower())
        self.assertIn("grant usage on schema api to authenticated", self.sql.lower())

        required_views = {
            "investment_cases",
            "open_orders",
            "positions",
            "risk_status",
            "strategy_registry",
            "agent_registry",
            # 회계·포트폴리오 읽기 뷰 (20260804000500). `/ui/snapshot`의 회계 구간 원천이다.
            "portfolio_snapshot_latest",
            "position_holdings",
            "ledger_balances",
            "open_breaks",
        }
        actual_views = set(
            re.findall(
                r"(?im)^create\s+or\s+replace\s+view\s+api\.([a-z_][a-z0-9_]*)",
                self.sql,
            )
        )
        self.assertEqual(actual_views, required_views)
        self.assertIn("api.match_evidence_chunks", self.sql)
        self.assertIn("api.get_case_timeline", self.sql)

    def test_point_in_time_and_versioning_contracts_exist(self) -> None:
        for token in (
            "published_at",
            "observed_at",
            "content_hash",
            "schema_version",
            "strategy_version_id",
            "trace_id",
            "idempotency_key",
        ):
            with self.subTest(token=token):
                self.assertIn(token, self.sql)


class TimescaleSchemaContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.files = read_sql_files(TIMESCALE_MIGRATIONS)
        cls.sql = "\n".join(content for _, content in cls.files)
        cls.tables = created_tables(cls.sql)

    def test_market_data_plane_entities_exist(self) -> None:
        required = {
            ("market", "market_ticks"),
            ("market", "market_quotes"),
            ("market", "market_bars"),
            ("market", "microstructure_features"),
            ("market", "market_breadth"),
            ("market", "derivative_snapshots"),
            ("market", "data_quality_windows"),
            ("market", "feed_gaps"),
            ("market", "ingestion_watermarks"),
            ("market", "archive_exports"),
            ("market", "retention_registry"),
            # PIT 출처 각인 (002, 2026-08-14 에 supabase/migrations 에서 옮겨 옴).
            # `market` 스키마는 TimescaleDB 에만 있으므로 이 마이그레이션이
            # Supabase 목록에 있으면 `db push` 가 없는 스키마에서 멈춘다.
            ("market", "pit_provenance"),
        }
        self.assertEqual(self.tables, required)

    def test_hypertables_and_continuous_aggregate_exist(self) -> None:
        expected_hypertables = {
            "market.market_ticks",
            "market.market_quotes",
            "market.market_bars",
            "market.microstructure_features",
            "market.market_breadth",
            "market.derivative_snapshots",
            "market.data_quality_windows",
        }
        actual_hypertables = set(
            re.findall(r"create_hypertable\(\s*'([^']+)'", self.sql, re.IGNORECASE)
        )
        self.assertEqual(actual_hypertables, expected_hypertables)
        self.assertIn("with (timescaledb.continuous)", self.sql.lower())
        self.assertIn("add_continuous_aggregate_policy", self.sql)

    def test_archive_gate_precedes_retention_deletion(self) -> None:
        self.assertIn("exported boolean", self.sql)
        self.assertIn("verified boolean", self.sql)
        self.assertIn("manifest_signed boolean", self.sql)
        self.assertIn("deletion_enabled boolean not null default false", self.sql)
        self.assertNotIn("add_retention_policy", self.sql)

    def test_timescale_has_no_cross_database_foreign_keys(self) -> None:
        self.assertNotRegex(self.sql.lower(), r"references\s+(reference|governance|strategy|accounting)\.")
        self.assertIn("instrument_id uuid not null", self.sql)

    def test_compression_jobs_have_a_finite_runtime(self) -> None:
        migration = next(
            content
            for path, content in self.files
            if path.name == "006_compression_policy_runtime.sql"
        ).lower()
        self.assertIn("public.alter_job", migration)
        self.assertIn("max_runtime => interval '20 minutes'", migration)
        for hypertable in (
            "market_ticks",
            "market_quotes",
            "market_bars",
            "microstructure_features",
            "derivative_snapshots",
        ):
            with self.subTest(hypertable=hypertable):
                self.assertIn(f"'{hypertable}'", migration)


if __name__ == "__main__":
    unittest.main()
