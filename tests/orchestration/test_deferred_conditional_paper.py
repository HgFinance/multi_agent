from __future__ import annotations

from types import SimpleNamespace

from orchestration.adapters.ceo_supervisor import (
    CeoSupervisorService,
    ChildTaskState,
    SupervisorAction,
    SupervisorDecision,
    SupervisorState,
    _deferred_conditional_decision,
)
from orchestration.ceo_workflow_scope import build_scoped_task_body


def test_deferred_conditional_decision_waits_for_research_and_uses_existing_scope(
    monkeypatch,
) -> None:
    root_id = "t_deferred_root"
    order_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    raw = "research 분석 후 삼성전자 262,000원 초과 시 5주 매도 조건주문"
    record = SimpleNamespace(
        order_request_id=order_id,
        user_id="11111111-1111-4111-8111-111111111111",
        fund_id="22222222-2222-4222-8222-222222222222",
        book_id="33333333-3333-4333-8333-333333333333",
        client_request_id="discord:deferred-conditional-1",
        raw_instruction=raw,
        raw_instruction_sha256="a" * 64,
        ceo_root_task_id=root_id,
        trading_task_id=None,
    )

    class Repository:
        def get(self, value):
            assert value == order_id
            return record

    monkeypatch.setattr(
        "apps.api.user_order_workflow.user_order_repository",
        lambda: Repository(),
    )
    monkeypatch.setattr(
        "apps.api.ceo._conditional_rule_child_body",
        lambda **kwargs: "hgfinance.user-conditional-paper-rule.v1\n" + kwargs["query"],
    )

    root_body = "\n".join(
        (
            "workflow_mode=analysis",
            "origin=user-query",
            "deferred_conditional=true",
            f"deferred_conditional_order_request_id={order_id}",
            "deferred_conditional_required_profile=research-department",
            "deferred_conditional_policy=AFTER_RESEARCH_PRIMARY_COMPLETED",
        )
    )
    research_body = build_scoped_task_body(
        "research result",
        root_id,
        role="primary",
        workflow_mode="analysis",
    )
    research = ChildTaskState(
        task_id="t_research",
        profile="research-department",
        status="done",
        result="Research 분석 결과와 근거를 정리했습니다.",
        body=research_body,
    )
    state = SupervisorState(
        parent_task_id=root_id,
        children=(research,),
        selected_primary_profiles=("research-department",),
        has_mandate=False,
        root_is_user_query=True,
    )

    decision = _deferred_conditional_decision(state, root_body)

    assert decision is not None
    assert decision.action is SupervisorAction.CREATE_TASK
    assert decision.assignee == "trading-department"
    assert decision.initial_status == "blocked"
    assert decision.reason == "deferred_conditional_after_research"
    assert "삼성전자 262,000원 초과 시 5주 매도 조건주문" in (decision.body or "")
    assert f"deferred_conditional_order_request_id={order_id}" in (decision.body or "")


def test_deferred_conditional_does_not_release_without_usable_research() -> None:
    root_id = "t_deferred_root_empty"
    order_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    root_body = "\n".join(
        (
            "deferred_conditional=true",
            f"deferred_conditional_order_request_id={order_id}",
            "deferred_conditional_required_profile=research-department",
            "deferred_conditional_policy=AFTER_RESEARCH_PRIMARY_COMPLETED",
        )
    )
    research = ChildTaskState(
        task_id="t_research_empty",
        profile="research-department",
        status="done",
        result="",
        body=build_scoped_task_body(
            "research", root_id, role="primary", workflow_mode="analysis"
        ),
    )
    state = SupervisorState(
        parent_task_id=root_id,
        children=(research,),
        selected_primary_profiles=("research-department",),
        root_is_user_query=True,
    )

    assert _deferred_conditional_decision(state, root_body) is None


def test_deferred_trading_card_is_bound_before_unblock(monkeypatch) -> None:
    order_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"

    class Repository:
        def bind_trading_task(self, request_id, task_id):
            assert request_id == order_id
            assert task_id == "t_trading"

    class Client:
        def __init__(self):
            self.created = []
            self.unblocked = []

        def create_task(self, **kwargs):
            self.created.append(kwargs)
            return {"id": "t_trading"}

        def unblock_task(self, task_id):
            self.unblocked.append(task_id)

    monkeypatch.setattr(
        "apps.api.user_order_workflow.user_order_repository",
        lambda: Repository(),
    )
    client = Client()
    service = CeoSupervisorService(client)
    state = SupervisorState(parent_task_id="t_root", children=())
    # Use the public decision shape produced by the gate without reaching the
    # database a second time in this sequencing-focused test.
    decision = SupervisorDecision(
        SupervisorAction.CREATE_TASK,
        "t_root",
        assignee="trading-department",
        title="deferred Trading",
        body=(
            "hgfinance.user-conditional-paper-rule.v1\n"
            f"deferred_conditional_order_request_id={order_id}"
        ),
        reason="deferred_conditional_after_research",
        initial_status="blocked",
    )

    service._execute(decision, state)

    assert client.created[0]["initial_status"] == "blocked"
    assert client.unblocked == ["t_trading"]
