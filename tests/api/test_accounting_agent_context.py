from __future__ import annotations

from types import SimpleNamespace

from apps.api import accounting, hermes_boundary


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
