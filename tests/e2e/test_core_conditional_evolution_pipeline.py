"""One deterministic E2E for the two core HgFinance paths.

This test deliberately uses a temporary Evolution repository.  It proves the
full governed lifecycle without adding a second production implementation or
mutating the live registry:

    conditional rule -> trigger -> deterministic PAPER guard
      -> three independent QA observations -> candidate -> proposal
      -> QA approval -> canonical activation in an isolated repository

The broker/OMS/fill/ledger boundary is covered by the existing
``test_selected_strategy_trace`` and ``test_paper_loop`` suites.  Keeping that
boundary in one owner avoids a second, subtly different PAPER executor here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from apps.api import conditional_rule_orchestrator as orchestrator
from apps.api.conditional_rule_worker import ConditionalRuleWorker
from apps.api.conditional_rules import ConditionalRuleCandidate
from tests.api.test_conditional_rule_orchestrator import (
    USER_ID,
    _install_workflow,
)
from tests.conditional_rules.test_worker import FakeClient, FakeStore, inputs

from orchestration.conditional_rules import (
    ActiveRule,
    EvaluationContext,
    EvaluationFrame,
    ExecutionGuardInput,
    ConditionalRuleSpec,
    RuleState,
    evaluate_condition,
    guard_rule_execution,
)
from orchestration.evolution_skills import (
    PRODUCTION_GENERATION_MODEL,
    EvolutionSkillStore,
    Occurrence,
    build_resolution_report,
    detect_candidates,
    promote_proposal,
    validate_canonical_registry,
)


NOW = datetime(2026, 8, 20, 1, 0, tzinfo=timezone.utc)


def _conditional_rule() -> ConditionalRuleSpec:
    return ConditionalRuleSpec.model_validate(
        {
            "schema_version": "conditional-trade-rule.v1",
            "authority": {
                "user_id": "10000000-0000-0000-0000-000000000001",
                "fund_id": "20000000-0000-0000-0000-000000000001",
                "book_id": "30000000-0000-0000-0000-000000000001",
            },
            "instrument_id": "40000000-0000-0000-0000-000000000001",
            "symbol": "005930",
            "condition": {
                "type": "COMPARISON",
                "operator": "GT",
                "left": {"type": "MARKET", "field": "LAST_PRICE"},
                "right": {"type": "LITERAL", "value": "100", "unit": "PRICE"},
            },
            "action": {
                "side": "BUY",
                "sizing": {"type": "FIXED_SHARES", "value": "2"},
            },
            "evaluation": {"clock": "QUOTE"},
            "execution_mode": "PAPER",
            "repeat_policy": "ONCE",
            "expires_at": (NOW + timedelta(days=30)).isoformat(),
            "raw_instruction_sha256": "0" * 64,
        }
    )


def _notional_price_candidate() -> ConditionalRuleCandidate:
    return ConditionalRuleCandidate.model_validate(
        {
            "symbol": "삼성전자",
            "condition": {
                "type": "COMPARISON",
                "operator": "GT",
                "left": {"type": "MARKET", "field": "LAST_PRICE"},
                "right": {"type": "LITERAL", "value": "100", "unit": "PRICE"},
            },
            "action": {
                "side": "BUY",
                "sizing": {"type": "NOTIONAL_KRW", "value": "1000000"},
            },
            "evaluation": {"clock": "QUOTE"},
        }
    )


def _skill_body(slug: str) -> str:
    return (
        f"# {slug}\n\n"
        "## 왜 필요한가\n"
        "조건주문 관찰에서 반복된 검증 가능한 문제를 다음 실행에서 재현한다.\n\n"
        "## 작업 순서\n"
        "조건 발생 시각과 PAPER 가드 결과를 확인하고 정본 검증 명령을 실행한다.\n\n"
        "## 하지 않을 것\n"
        "관측하지 않은 결과를 만들거나 승인·위험 통제를 우회하지 않는다.\n"
    )


def _run_governed_evolution(
    tmp_path: Path, *, run_prefix: str, detail: str
) -> dict[str, object]:
    """Run the real candidate/proposal/approval/promotion state machine once."""

    store = EvolutionSkillStore(tmp_path / "evolution-state")
    occurrences = [
        Occurrence(
            kind="conditional-paper-trigger-review",
            detail=f"{detail} in core E2E run {number}",
            run_id=f"{run_prefix}-{number}",
            department="02-trading",
            source_type="qa-benchmark",
            source_artifact_id=f"feedback-{run_prefix}-{number}",
            benchmark_id="core-conditional-e2e-v1",
            improvement_type="SKILL_CREATE",
        )
        for number in range(1, 4)
    ]
    assert store.append_occurrences(occurrences) == 3

    candidates = detect_candidates(occurrences, department="02-trading")
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate.slug == "conditional-paper-trigger-review"
    assert candidate.count == 3

    proposal = store.create_proposal(
        candidate,
        lambda _prompt: _skill_body(candidate.slug),
        model_metadata={
            "model_version": PRODUCTION_GENERATION_MODEL,
            "base_model": PRODUCTION_GENERATION_MODEL,
            "adapter_id": None,
        },
    )
    assert proposal["status"] == "VALIDATED"

    approved = store.approve(
        proposal["proposal_id"],
        approved_by="discord:382384727245455360",
        qa_verdict="PASS",
        decision_ref="core-conditional-evolution-e2e-v1",
    )
    assert approved["status"] == "APPROVED"

    isolated_repo = tmp_path / "canonical-repo"
    registry = isolated_repo / "skills" / "evolution-registry.json"
    active = promote_proposal(
        store,
        proposal["proposal_id"],
        repository_root=isolated_repo,
        registry_path=registry,
    )
    assert active["status"] == "ACTIVE"
    assert active["regression_validation"]["ok"] is True
    assert (
        isolated_repo / "skills/evolved/conditional-paper-trigger-review/SKILL.md"
    ).is_file()
    assert validate_canonical_registry(isolated_repo, registry)["ok"] is True

    report = build_resolution_report(store, proposal["proposal_id"])
    assert report["outcome_evidence"]["status"] == "ACTIVE_PENDING_FEEDBACK"
    assert report["problem_evidence"]["source_run_ids"] == [
        f"{run_prefix}-1",
        f"{run_prefix}-2",
        f"{run_prefix}-3",
    ]
    return {
        "proposal": proposal,
        "approved": approved,
        "active": active,
        "report": report,
    }


def test_conditional_paper_trigger_feeds_governed_evolution_activation(
    tmp_path: Path,
) -> None:
    rule = _conditional_rule()
    before = EvaluationContext(
        current=EvaluationFrame(
            market={"LAST_PRICE": Decimal("99")},
            portfolio={},
            indicators={},
            observed_at=NOW,
        )
    )
    at_trigger = EvaluationContext(
        current=EvaluationFrame(
            market={"LAST_PRICE": Decimal("105")},
            portfolio={},
            indicators={},
            observed_at=NOW + timedelta(minutes=1),
        )
    )

    assert evaluate_condition(rule, before) is False
    assert evaluate_condition(rule, at_trigger) is True

    guard = guard_rule_execution(
        rule,
        ExecutionGuardInput(
            now=NOW + timedelta(minutes=1),
            rule_state=RuleState.ACTIVE,
            evaluated_rule_version=1,
            active_rule_version=1,
            membership_active=True,
            fund_active=True,
            book_active=True,
            market_session_available=True,
            market_open=True,
            data_complete=True,
            quote_fresh=True,
            current_price=Decimal("105"),
            available_cash=Decimal("1000000"),
            position_quantity=Decimal("0"),
            sellable_quantity=Decimal("0"),
            lot_size=Decimal("1"),
        ),
    )
    assert guard.allowed is True
    assert guard.code == "READY_FOR_PAPER_DIRECTIVE"
    assert guard.quantity == Decimal("2")

    # A candidate requires three distinct, benchmarked observations.  These
    # are isolated E2E observations, not fabricated live production history.
    _run_governed_evolution(
        tmp_path,
        run_prefix="core-conditional-e2e",
        detail="PAPER trigger guard observed",
    )


def test_user_query_reaches_conditional_paper_result_and_evolution_approval(
    monkeypatch, tmp_path: Path
) -> None:
    """Exercise the user-facing bridge, worker seam, and governed lifecycle."""

    raw = "삼성전자 현재가가 100원을 초과하면 100만원 시장가 매수해"
    orders, rules, tasks = _install_workflow(monkeypatch, raw_instruction=raw)
    admission = next(iter(orders._records.values()))

    # CEO accepted the exact user sentence and created the scoped CEO/Trading
    # cards before the interpreter result is admitted.
    assert admission.raw_instruction == raw
    assert admission.raw_instruction_sha256
    assert raw in tasks["t_root1"]["body"]
    assert raw in tasks["t_trade1"]["body"]

    activation = orchestrator.process_user_conditional_paper_rule(
        root_task_id="t_root1",
        trading_task_id="t_trade1",
        candidate=_notional_price_candidate(),
    )
    assert activation["binding"] is True
    assert activation["mode"] == "PAPER"
    assert activation["state"] == "ACTIVE"
    assert activation["summary"]["symbol"] == "005930"
    assert activation["summary"]["sizing_value"] == "1000000"

    waiting = orchestrator.get_user_conditional_paper_rule_status(
        root_task_id="t_root1", trading_task_id="t_trade1"
    )
    assert waiting["workflow_state"] == "WAITING_FOR_TRIGGER"
    stored = rules.get(activation["rule_id"], user_id=USER_ID)
    assert stored is not None

    # Reuse the existing worker test seam so this E2E cannot accidentally grow
    # a second PAPER executor.  The production ConditionalRuleWorker is real;
    # only market/Trading side effects are isolated and deterministic.
    active_rule = ActiveRule(
        rule_id=UUID(stored.rule_id),
        rule_version=stored.rule_version,
        row_version=1,
        spec_sha256=stored.spec_sha256,
        spec=stored.spec,
    )
    worker_store = FakeStore(active_rule)
    worker_client = FakeClient(
        inputs(
            price="105",
            position_quantity="0",
            sellable_quantity="0",
        )
    )
    counts = ConditionalRuleWorker(
        worker_store, worker_client, batch_size=1, max_workers=1
    ).process_once()
    assert counts["evaluated"] == 1
    assert counts["triggered"] == 1
    assert counts["submitted"] == 1
    assert counts["errors"] == 0
    assert worker_client.submit_calls == 1
    assert worker_store.execution_decisions == [
        (True, "READY_FOR_PAPER_DIRECTIVE", Decimal("9523"))
    ]
    directive_id = worker_store.submitted[0]

    # In production this projection is read from the execution join.  Mirror
    # that one durable projection in memory, then exercise the same user
    # status reader and its PAPER-only boundary.
    rules._records[stored.rule_id] = replace(
        stored,
        directive_id=str(directive_id),
        last_execution_state="SUBMITTED",
        last_guard_code="READY_FOR_PAPER_DIRECTIVE",
    )
    monkeypatch.setattr(
        orchestrator,
        "read_paper_directive_status_for_admitted_authority",
        lambda **_kwargs: {
            "directive_id": str(directive_id),
            "state": "SUBMITTED",
            "mode": "PAPER",
            "error_code": None,
            "legs": [
                {
                    "symbol": "005930",
                    "side": "BUY",
                    "order_type": "MARKET",
                    "requested_quantity": "9523",
                    "filled_quantity": "0",
                    "average_fill_price": None,
                    "broker_order_id": "ls-paper:e2e-conditional-1",
                }
            ],
        },
    )
    monkeypatch.setattr(
        orchestrator,
        "_workflow_state_from_directive",
        lambda _directive: "IN_PROGRESS",
    )
    submitted = orchestrator.get_user_conditional_paper_rule_status(
        root_task_id="t_root1", trading_task_id="t_trade1"
    )
    assert submitted["authority_verified"] is True
    assert submitted["mode"] == "PAPER"
    assert submitted["directive_id"] == str(directive_id)
    assert submitted["workflow_state"] == "IN_PROGRESS"

    evolution = _run_governed_evolution(
        tmp_path,
        run_prefix="user-query-conditional-e2e",
        detail="User query reached PAPER submission",
    )
    assert evolution["approved"]["status"] == "APPROVED"
    assert evolution["active"]["status"] == "ACTIVE"
    assert evolution["report"]["outcome_evidence"]["status"] == (
        "ACTIVE_PENDING_FEEDBACK"
    )
