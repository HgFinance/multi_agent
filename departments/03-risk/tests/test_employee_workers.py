"""Risk employee Graph contract tests (no network, no order side effects)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

RISK_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RISK_DIR))

import risk_employee_workers
from risk_employee_workers import WORKER_SPECS, risk_runner, run_employee_workers


def _llm(_system: str, _prompt: str) -> str:
    return '{"summary":"evidence checked","confidence":0.8,"evidence_refs":["tool"],"escalate":false}'


def test_llm_worker_is_langgraph_qwen_and_conditional():
    report = run_employee_workers(
        {"trading_state": "ENABLED", "assessment": {"verdict": "approve"}},
        llm=_llm,
    )
    assert report["runtime"]["executor"] == "LangGraph"
    assert report["runtime"]["model"] == "qwen3:1.7b"
    assert report["failed"] == []
    assert "compliance-policy-worker" in report["not_executed"]
    # risk-runner는 레지스트리 밖이지만 항상 실행되고 workers/executed에 나온다
    assert report["executed"] == ["risk-runner"]
    assert any(item["worker_id"] == "risk-runner" for item in report["workers"])
    assert all(item["tools"] for item in report["workers"])


def test_compliance_worker_runs_only_when_its_signal_exists():
    report = run_employee_workers(
        {
            "trading_state": "ENABLED",
            "assessment": {"verdict": "approve"},
            "compliance": {"grounded": True},
            "counterparty": {"status": "DEGRADED"},
        },
        llm=_llm,
    )
    assert report["failed"] == []
    assert {item["worker_id"] for item in report["workers"]} == {
        spec.worker_id for spec in WORKER_SPECS
    } | {"risk-runner"}


def test_compliance_worker_trace_contains_all_declared_tools():
    report = run_employee_workers(
        {
            "trading_state": "ENABLED",
            "p1_snapshot": {"status": "PASS"},
            "assessment": {"verdict": "approve"},
            "compliance": {"grounded": True},
            "counterparty": {"status": "DEGRADED"},
        },
        llm=_llm,
    )

    # risk-runner는 LangGraph Worker가 아니라 skill_results가 없다 - 별도로 다룬다
    llm_workers = [w for w in report["workers"] if w["worker_id"] != "risk-runner"]
    assert llm_workers
    for worker in llm_workers:
        tool_events = [
            event
            for event in worker["skill_results"]
            if event["skill_id"] == "context.internal_api.v1"
        ]
        assert len(tool_events) == 1
        assert tool_events[0]["tool_calls"] == worker["tools"]


def test_risk_runner_is_deterministic_and_derives_blockers_from_engine_output():
    report = risk_runner(
        {
            "trading_state": "ENABLED",
            "assessment": {
                "verdict": "reject",
                "check_results": [{"name": "concentration", "passed": False}],
            },
            "counterparty": {"status": "DEGRADED"},
        }
    )
    assert report["llm"] is False
    assert report["status"] == "COMPLETED"
    assert "summary" not in report["output"]
    assert report["output"]["decided_by"] == "deterministic"
    assert report["output"]["authoritative"] is False
    assert "risk_verdict_reject" in report["output"]["blockers"]
    assert "check_failed:concentration" in report["output"]["blockers"]
    assert report["output"]["escalate"] is True


def test_risk_runner_has_no_blockers_when_engine_approves():
    report = risk_runner({"assessment": {"verdict": "approve", "check_results": []}})
    assert report["output"]["blockers"] == []
    assert report["output"]["escalate"] is False


def _worker_output(report: dict) -> dict:
    worker = next(
        w for w in report["workers"] if w["worker_id"] == "compliance-policy-worker"
    )
    return worker["output"]


def _tool_output(report: dict) -> dict:
    worker = next(
        w for w in report["workers"] if w["worker_id"] == "compliance-policy-worker"
    )
    (event,) = [
        e for e in worker["skill_results"] if e["skill_id"] == "context.internal_api.v1"
    ]
    return event["output"]


def _fake_legal_answer_fn(query: str, as_of: str, mandate: str) -> dict:
    return {
        "answer": {
            "verdict": "breach",
            "cited_documents": ["자본시장법_제178조"],
            "rationale": "부정거래행위 소지.",
            "confidence": 0.75,
            "escalate": True,
        },
        "context_chars": 400,
        "pages_visited": ["자본시장법_제178조"],
    }


def _spy_on_query_legal_wiki(monkeypatch) -> list:
    """Patch legal_wiki_tool.query_legal_wiki to record calls but skip the real
    OpenAI-backed Arm C (arms.py) — the fake only replaces the answer_fn boundary,
    the same injection point risk_mandate_workers.py's own LEGAL_QUERY tests use."""

    calls: list = []
    real_query_legal_wiki = risk_employee_workers.query_legal_wiki

    def spy(request, *, answer_fn=None):
        calls.append(request)
        return real_query_legal_wiki(request, answer_fn=_fake_legal_answer_fn)

    monkeypatch.setattr(risk_employee_workers, "query_legal_wiki", spy)
    return calls


def _forbid_query_legal_wiki(monkeypatch) -> None:
    def boom(request, *, answer_fn=None):
        raise AssertionError("query_legal_wiki must not be called for this query_mode")

    monkeypatch.setattr(risk_employee_workers, "query_legal_wiki", boom)


# ── 1. 구조화된 query_mode: route가 LLM을 부르지 않고, tool→worker_llm→validate까지 유지 ──
def test_structured_query_mode_skips_route_llm_and_flows_through_full_graph(
    monkeypatch,
):
    legal_calls = _spy_on_query_legal_wiki(monkeypatch)
    narration_calls: list[str] = []

    def counting_llm(system: str, prompt: str) -> str:
        if "routing classifier" in system:
            raise AssertionError("structured query_mode must skip LLM routing")
        narration_calls.append(prompt)
        return '{"summary":"policy evidence reviewed","confidence":0.9,"evidence_refs":["p"],"escalate":false}'

    report = run_employee_workers(
        {"compliance": {"grounded": True}, "query_mode": "RISK_POLICY_REVIEW"},
        llm=counting_llm,
    )

    worker = next(
        w for w in report["workers"] if w["worker_id"] == "compliance-policy-worker"
    )
    assert worker["status"] == "COMPLETED"  # tool -> worker_llm -> validate 모두 통과
    tool_events = [
        e for e in worker["skill_results"] if e["skill_id"] == "context.internal_api.v1"
    ]
    assert len(tool_events) == 1  # tool 노드가 정확히 한 번 실행됨
    assert len(narration_calls) == 1  # worker_llm 노드도 한 번 실행됨
    assert legal_calls == []  # RISK_POLICY_REVIEW는 legal_wiki_tool을 안 씀

    output = worker["output"]
    assert output["query_mode"] == "RISK_POLICY_REVIEW"
    assert output["routing_by_llm"] is False


# ── 2. 자연어 compliance.query만 있는 경우: 동일 Qwen 모델이 5-mode 중 하나로 분류 ──
def test_natural_language_query_is_classified_by_the_same_local_model(monkeypatch):
    _spy_on_query_legal_wiki(monkeypatch)
    route_prompts: list[str] = []
    narration_prompts: list[str] = []

    def dispatch_llm(system: str, prompt: str) -> str:
        if "routing classifier" in system:
            route_prompts.append(prompt)
            return '{"query_mode": "LEGAL_QUERY", "routing_rationale": "법령 질문"}'
        narration_prompts.append(prompt)
        return '{"summary":"법률 근거 확인됨","confidence":0.75,"evidence_refs":["자본시장법_제178조"],"escalate":true}'

    report = run_employee_workers(
        {"compliance": {"query": "부정거래행위 판단 기준이 뭐야"}},
        llm=dispatch_llm,  # route와 worker_llm 노드 둘 다 이 같은 콜백(=같은 모델)을 쓴다
    )

    assert len(route_prompts) == 1 and "부정거래행위" in route_prompts[0]
    assert len(narration_prompts) == 1  # 같은 콜러블이 narration도 수행

    output = _worker_output(report)
    assert output["query_mode"] == "LEGAL_QUERY"
    assert output["routing_by_llm"] is True
    assert output["routing_rationale"] == "법령 질문"  # 최종 output에 라우팅 사유가 남음


# ── 3. LEGAL_QUERY: legal_wiki_tool(Arm C 경로)이 실제 호출되고, 그 근거가 worker_llm에 전달 ──
def test_legal_query_mode_calls_legal_wiki_tool_and_feeds_evidence_to_worker_llm(
    monkeypatch,
):
    legal_calls = _spy_on_query_legal_wiki(monkeypatch)
    narration_prompts: list[str] = []

    def llm(system: str, prompt: str) -> str:
        if "routing classifier" in system:
            raise AssertionError("query_mode is structured; routing must be skipped")
        narration_prompts.append(prompt)
        return '{"summary":"부정거래행위 위반 소지 확인","confidence":0.75,"evidence_refs":["자본시장법_제178조"],"escalate":true}'

    report = run_employee_workers(
        {
            "compliance": {"query": "부정거래행위 판단 기준이 뭐야"},
            "query_mode": "LEGAL_QUERY",
        },
        llm=llm,
    )

    assert len(legal_calls) == 1  # legal_wiki_tool이 정확히 한 번 실제 호출됨
    assert legal_calls[0].query == "부정거래행위 판단 기준이 뭐야"

    tool_output = _tool_output(report)
    assert tool_output["legal"]["status"] == "OK"
    assert tool_output["legal"]["cited_documents"] == ["자본시장법_제178조"]

    # worker_llm(같은 모델)이 tool_output의 법률 근거를 실제로 프롬프트에서 받았는지 확인
    assert "자본시장법_제178조" in narration_prompts[0]
    assert "부정거래행위 소지" in narration_prompts[0]  # rationale도 전달됨

    output = _worker_output(report)
    assert output["evidence_refs"] == ["자본시장법_제178조"]
    assert output["escalate"] is True


# ── 4. MIXED_REVIEW: legal_wiki_tool 호출 + 기존 evidence가 함께 worker_llm에 전달 ──
def test_mixed_review_combines_legal_tool_and_existing_evidence_for_worker_llm(
    monkeypatch,
):
    legal_calls = _spy_on_query_legal_wiki(monkeypatch)
    narration_prompts: list[str] = []

    def llm(system: str, prompt: str) -> str:
        narration_prompts.append(prompt)
        return '{"summary":"내부정책·법률 근거 모두 검토","confidence":0.6,"evidence_refs":["internal-policy-3","자본시장법_제178조"],"escalate":true}'

    report = run_employee_workers(
        {
            "compliance": {
                "query": "임직원 매매명세 통지 주기",
                "internal_policy_note": "사전 신고 필요, internal-policy-3 참조",
            },
            "query_mode": "MIXED_REVIEW",
        },
        llm=llm,
    )

    assert len(legal_calls) == 1  # MIXED_REVIEW도 legal_wiki_tool을 호출한다

    tool_output = _tool_output(report)
    assert tool_output["legal"]["cited_documents"] == ["자본시장법_제178조"]
    assert tool_output["compliance"]["internal_policy_note"] == (
        "사전 신고 필요, internal-policy-3 참조"
    )

    # 두 근거가 같은 narration 프롬프트에 함께 들어갔는지 확인
    assert "자본시장법_제178조" in narration_prompts[0]
    assert "internal-policy-3" in narration_prompts[0]


# ── 5. 나머지 mode: legal_wiki_tool을 부르지 않고 기존 evidence-passthrough만 쓴다 ──
@pytest.mark.parametrize("mode", ["MANDATE_REVIEW", "RISK_POLICY_REVIEW", "NOT_APPLICABLE"])
def test_non_legal_modes_never_call_legal_wiki_tool(monkeypatch, mode):
    _forbid_query_legal_wiki(monkeypatch)

    def llm(system: str, prompt: str) -> str:
        return '{"summary":"evidence reviewed","confidence":0.8,"evidence_refs":[],"escalate":false}'

    report = run_employee_workers(
        {
            "compliance": {"query": "이 질문은 법률 검색을 유발하면 안 됨", "grounded": True},
            "query_mode": mode,
        },
        llm=llm,
    )

    tool_output = _tool_output(report)
    assert "legal" not in tool_output
    assert tool_output["compliance"]["grounded"] is True  # 기존 evidence-passthrough 유지

    output = _worker_output(report)
    assert output["query_mode"] == mode


# ── 6. 라우팅 분류 실패/파싱 실패: MIXED_REVIEW로 fail-open(범위를 좁히지 않음) ──
def test_compliance_worker_routing_parse_failure_defaults_to_mixed_review(monkeypatch):
    legal_calls = _spy_on_query_legal_wiki(monkeypatch)

    def broken_router_llm(system: str, prompt: str) -> str:
        if "routing classifier" in system:
            return "not json at all"
        return '{"summary":"fallback narration","confidence":0.5,"evidence_refs":[],"escalate":true}'

    report = run_employee_workers(
        {"compliance": {"query": "임직원 매매 규정 확인"}},
        llm=broken_router_llm,
    )

    output = _worker_output(report)
    assert output["query_mode"] == "MIXED_REVIEW"
    assert output["routing_by_llm"] is True
    assert output["routing_rationale"] == "routing_parse_failed_defaulted_to_mixed_review"
    assert len(legal_calls) == 1  # fail-open이 실제로 법률 검색까지 이어지는지 (근거 없음 != 무혐의)


# ── 7. validate: summary/evidence_refs/escalate/query_mode/routing_rationale 보존 ──
def test_validate_preserves_all_routing_and_narration_fields(monkeypatch):
    _spy_on_query_legal_wiki(monkeypatch)

    def llm(system: str, prompt: str) -> str:
        if "routing classifier" in system:
            return '{"query_mode": "MIXED_REVIEW", "routing_rationale": "정책+법률 모두 필요"}'
        return '{"summary":"최종 요약","confidence":0.7,"evidence_refs":["ref-a","ref-b"],"escalate":true}'

    report = run_employee_workers(
        {"compliance": {"query": "임직원 매매 규정 확인"}},
        llm=llm,
    )

    output = _worker_output(report)
    assert output["summary"] == "최종 요약"
    assert output["evidence_refs"] == ["ref-a", "ref-b"]
    assert output["escalate"] is True
    assert output["query_mode"] == "MIXED_REVIEW"
    assert output["routing_rationale"] == "정책+법률 모두 필요"


def test_compliance_policy_worker_graph_topology_is_route_tool_worker_llm_validate():
    spec = next(s for s in WORKER_SPECS if s.worker_id == "compliance-policy-worker")
    compiled = risk_employee_workers.build_worker_graph(
        spec, risk_employee_workers._compliance_tool
    )
    graph = compiled.get_graph()

    assert set(graph.nodes) == {
        "__start__",
        "route",
        "tool",
        "worker_llm",
        "validate",
        "__end__",
    }
    edges = {(edge.source, edge.target) for edge in graph.edges}
    assert edges == {
        ("__start__", "route"),
        ("route", "tool"),
        ("tool", "worker_llm"),
        ("worker_llm", "validate"),
        ("validate", "__end__"),
    }
