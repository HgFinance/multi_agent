from __future__ import annotations

from types import SimpleNamespace

from apps.api import accounting, hermes_boundary


def test_accounting_alias_scopes_ceo_plan_to_accounting_hermes(monkeypatch) -> None:
    captured: dict[str, object] = {}

    def fake_ceo_query(req, owner_id=None, *, deterministic_routing_plan=None, **_kwargs):
        captured["request"] = req
        captured["owner_id"] = owner_id
        captured["plan"] = deterministic_routing_plan
        return {"task_id": "t_accounting", "status": "accepted"}

    monkeypatch.setattr("apps.api.ceo.ceo_query", fake_ceo_query)

    result = accounting._enqueue_accounting_via_ceo(
        hermes_boundary.AgentAsk(query="원장과 현금의 PAPER 상태를 검토해줘")
    )

    assert result["task_id"] == "t_accounting"
    plan = captured["plan"]
    assert plan["selected_primary_profiles"] == ("accounting-portfolio-department",)
    assert plan["requested_departments"] == ["accounting"]


def test_model_accounting_query_is_enqueued_on_canonical_ceo_path(monkeypatch) -> None:
    routing = SimpleNamespace(
        calls_model=True,
        as_dict=lambda: {"level": "L2"},
    )
    monkeypatch.setattr(accounting.hermes_boundary, "agent_ask_enabled", lambda: True)
    monkeypatch.setattr(accounting, "classify", lambda _query: routing)
    monkeypatch.setattr(accounting, "routing_note", lambda _routing: "L2")
    monkeypatch.setattr(
        accounting,
        "_enqueue_accounting_via_ceo",
        lambda req: {"task_id": "t_12345678", "status": "accepted"},
    )

    def forbidden_ask(**_kwargs):
        raise AssertionError("BFF must not invoke a department profile directly")

    monkeypatch.setattr(accounting.hermes_boundary, "ask", forbidden_ask)

    result = accounting.agent_ask(hermes_boundary.AgentAsk(query="사용자 질의"))

    assert result["task_id"] == "t_12345678"
    assert result["status"] == "accepted"
    assert result["result_url"] == "/ui/ceo/tasks/t_12345678/result"
    assert result["execution_path"] == "CEO_KANBAN_ACCOUNTING_HERMES"
    assert result["session_id"] is None
    assert result["authoritative"] is False
    assert result["routing"]["level"] == "L2"
