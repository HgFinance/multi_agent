"""Worker 실행 → HR 유휴 관측 이벤트 계측 계약 (2026-08-20).

## 왜 이 파일이 필요한가

2026-08-10 Langfuse 도입 당시 계측은 orchestration/workflows/portfolio_recommendation.py
한 곳에 있었고, 주석은 "이 한 지점이 6개 투자본부의 모든 Worker 실행을 통과한다"고
적었다. 2026-08-13 에 본부장이 자기 부서 Worker 를 직접 돌리는 MCP 간선이 생기면서
그 전제가 깨졌는데, **깨진 것이 아무 신호도 내지 않았다** - 조회는 정상이고 결과만
UNOBSERVED 라서 "그 워커는 원래 안 도나 보다"로 읽힌다. HR 이 그걸 보고 정리 판단을
하면 실제로 일하는 Worker 를 자르는 방향의 오판이 된다.

그래서 여기서 고정하는 것은 "이벤트가 나갔다" 하나가 아니라 **Worker 실행 경로가
셋이고 셋 다 계측된다**는 사실이다:
  (1) orchestration/workflows/portfolio_recommendation.py 자체 실행기
  (2) departments/employee_worker_runtime.py 공용 registry 실행기
  (3) Risk/QA 자체 실행기

## 네트워크는 타지 않는다

langfuse_enabled() 와 클라이언트를 가짜로 바꿔 create_event 호출만 가로챈다.
실제 전송 확인은 이 스위트의 일이 아니다(2026-08-20 실측으로 왕복은 따로 확인).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "departments"))

import orchestration.llm_observability as observability  # noqa: E402
from departments.employee_worker_runtime import (  # noqa: E402
    WorkerSpec,
    run_worker_registry,
)


class _FakeLangfuseClient:
    """create_event 인자를 그대로 붙잡는다. flush 호출 여부도 센다."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.flush_calls = 0

    def create_event(self, **kwargs: Any) -> None:
        self.events.append(kwargs)

    def flush(self) -> None:
        self.flush_calls += 1


@pytest.fixture()
def captured(monkeypatch: pytest.MonkeyPatch) -> _FakeLangfuseClient:
    client = _FakeLangfuseClient()
    monkeypatch.setattr(observability, "langfuse_enabled", lambda: True)
    monkeypatch.setattr(observability, "_safe_langfuse_client", lambda: client)
    return client


def _names(client: _FakeLangfuseClient) -> set[str]:
    return {event["name"] for event in client.events}


def _llm(_system: str, _prompt: str) -> str:
    return '{"summary":"ok","confidence":0.8,"evidence_refs":["tool"],"escalate":false}'


# ---------------------------------------------------------------------------
# (2) 공용 registry 실행기
# ---------------------------------------------------------------------------


def _demo_spec() -> WorkerSpec:
    return WorkerSpec("demo-worker", "Demo", ("demo.tool",), "always")


def test_shared_runtime_publishes_activity_when_stage_given(captured: _FakeLangfuseClient) -> None:
    spec = _demo_spec()
    report = run_worker_registry(
        (spec,),
        {"anything": 1},
        tools={"demo-worker": lambda value: {"tool": "demo", "value": value}},
        llm=_llm,
        stage="research",
    )
    assert report["workers"], "워커가 실행되지 않으면 이 테스트가 무의미하다"
    assert "llm.performance.metric:research:demo-worker" in _names(captured)


def test_shared_runtime_without_stage_publishes_nothing(captured: _FakeLangfuseClient) -> None:
    """stage 를 안 주는 기존 호출자의 동작은 그대로여야 한다(계측만 빠진다)."""

    run_worker_registry(
        (_demo_spec(),),
        {"anything": 1},
        tools={"demo-worker": lambda value: {"tool": "demo", "value": value}},
        llm=_llm,
    )
    assert captured.events == []


def test_publish_failure_does_not_break_worker_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    """계측이 죽어도 Worker 결과는 그대로 나와야 한다 - 관측이 로직을 못 바꾼다."""

    def _boom() -> Any:
        raise RuntimeError("langfuse exploded")

    monkeypatch.setattr(observability, "langfuse_enabled", lambda: True)
    monkeypatch.setattr(observability, "_safe_langfuse_client", _boom)
    report = run_worker_registry(
        (_demo_spec(),),
        {"anything": 1},
        tools={"demo-worker": lambda value: {"tool": "demo", "value": value}},
        llm=_llm,
        stage="research",
    )
    assert report["workers"][0]["status"] == "COMPLETED"


# ---------------------------------------------------------------------------
# (3) Risk / QA 자체 실행기
# ---------------------------------------------------------------------------


def test_risk_executor_publishes_activity(captured: _FakeLangfuseClient) -> None:
    sys.path.insert(0, str(ROOT / "departments" / "03-risk"))
    from risk_employee_workers import run_employee_workers as run_risk

    report = run_risk(
        {
            "trading_state": "ENABLED",
            "assessment": {"verdict": "approve"},
            "compliance": {"grounded": True},
            "counterparty": {"status": "DEGRADED"},
        },
        llm=_llm,
    )
    assert "compliance-policy-worker" in {w["worker_id"] for w in report["workers"]}
    assert "llm.performance.metric:risk:compliance-policy-worker" in _names(captured)


def test_qa_executor_publishes_activity(captured: _FakeLangfuseClient) -> None:
    sys.path.insert(0, str(ROOT / "departments" / "06-ai-qa-audit"))
    from qa_employee_workers import run_employee_workers as run_qa

    run_qa(
        {
            "assessment": {"decision": "FAIL", "claim_checks": [{"result": "UNSUPPORTED"}]},
            "model_risk": {"decision": "WARN"},
            "ops_assessment": {"status": "DEGRADED"},
            "incident": {"incident_id": "i1"},
        },
        llm=_llm,
    )
    assert "llm.performance.metric:qa:hallucination-critic-worker" in _names(captured)
    assert "llm.performance.metric:qa:incident-postmortem-worker" in _names(captured)


def test_deterministic_runners_are_not_reported_as_llm_workers(captured: _FakeLangfuseClient) -> None:
    """risk-runner/qa-runner 는 모델을 부르지 않는다 - 유휴 관측 대상이 아니다.

    이벤트로 나가면 HR 편제(LLM Worker 10명)와 리포트 인원이 어긋난다.
    """

    sys.path.insert(0, str(ROOT / "departments" / "03-risk"))
    from risk_employee_workers import run_employee_workers as run_risk

    run_risk({"trading_state": "ENABLED", "assessment": {"verdict": "approve"}}, llm=_llm)
    assert not any("runner" in name for name in _names(captured))


# ---------------------------------------------------------------------------
# 드리프트 가드
# ---------------------------------------------------------------------------

# 공용 registry 를 쓰는 부서 -> event name 이 쓰는 stage 값.
# (Risk/QA 는 자체 실행기라 여기 없다 - 위 두 테스트가 직접 실행으로 잡는다.)
SHARED_RUNTIME_ENTRY_POINTS = {
    "departments/00-ceo-office/employee_workers.py": "ceo",
    "departments/01-research/employee_workers.py": "research",
    "departments/04-quant-backtest/employee_workers.py": "quant",
    "departments/05-accounting-portfolio/employee_workers.py": "accounting",
}


@pytest.mark.parametrize(("relative_path", "stage"), sorted(SHARED_RUNTIME_ENTRY_POINTS.items()))
def test_department_entry_point_passes_its_stage(relative_path: str, stage: str) -> None:
    """부서가 stage= 를 빠뜨리면 그 부서만 조용히 계측에서 빠진다.

    실행이 실패하는 게 아니라 이벤트만 안 나가므로 어떤 테스트도 안 깨지고,
    HR 리포트에서 UNOBSERVED 로만 보인다 - 소스에서 직접 못 박는다.
    """

    tree = ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))
    stages: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else None
        if name != "run_worker_registry":
            continue
        for keyword in node.keywords:
            if keyword.arg == "stage" and isinstance(keyword.value, ast.Constant):
                stages.append(keyword.value.value)
    assert stages, f"{relative_path} 가 run_worker_registry 에 stage= 를 안 준다"
    assert set(stages) == {stage}


def test_investment_stages_cover_every_hr_observed_department() -> None:
    """계측이 쓰는 stage 이름과 HR 이 조회하는 stage 이름이 같아야 한다.

    두 이름 공간이 어긋나면 그 부서는 매 조회에서 0건이고, 조회 실패가 아니라
    UNOBSERVED 로 보인다.
    """

    sys.path.insert(0, str(ROOT / "departments" / "07-agent-workforce" / "scorecard"))
    from observability import INVESTMENT_DEPARTMENT_STAGE

    instrumented = set(SHARED_RUNTIME_ENTRY_POINTS.values()) | {"risk", "qa"}
    # trading 은 LLM Worker 0 명이라 실행기 자체가 없다(CLAUDE.md 편제표).
    expected = set(INVESTMENT_DEPARTMENT_STAGE.values()) - {"trading"}
    assert expected <= instrumented


# ---------------------------------------------------------------------------
# 성능·redaction 회귀 가드
# ---------------------------------------------------------------------------


def test_publish_does_not_flush_by_default(captured: _FakeLangfuseClient) -> None:
    """매 호출 flush 는 JP 리전 왕복 85ms 다(2026-08-20 실측).

    async fan-out 안에서 부르면 이벤트 루프를 막아 병렬 Worker 가 직렬화된다 -
    계측이 로직 성능을 바꾸는 순간이라 기본값이 되돌아가지 않게 고정한다.
    """

    observability.publish_worker_activity(stage="research", worker_id="w")
    assert captured.flush_calls == 0

    observability.publish_langfuse_metric({"worker_id": "w", "stage": "research"}, flush=True)
    assert captured.flush_calls == 1


def test_activity_event_never_carries_payload_text(captured: _FakeLangfuseClient) -> None:
    """input/output 은 항상 비어야 한다(.env.example 3-2절)."""

    observability.publish_worker_activity(
        stage="risk", worker_id="compliance-policy-worker", status="COMPLETED"
    )
    event = captured.events[-1]
    assert event["input"] is None
    assert event["output"] is None
    assert set(event["metadata"]) <= {
        "schema_version", "worker_id", "role", "stage", "model_name", "status",
        "attempts", "llm_calls", "retries", "prompt_tokens", "completion_tokens",
        "latency_ms", "eval_score", "error_count", "raw_payloads_sent", "trace_id",
    }


def test_activity_event_omits_fields_it_cannot_measure(captured: _FakeLangfuseClient) -> None:
    """모르는 값을 0/"" 으로 채우면 관측 사실로 읽힌다(2026-08-20).

    이 경로에는 begin_worker_metric() 컨텍스트가 없어 llm_calls·model_name·토큰수를
    셀 방법이 없다. `llm_calls: 0` 은 "모델을 안 불렀다"로 읽히므로 아예 빼야 한다.
    """

    observability.publish_worker_activity(
        stage="qa", worker_id="hallucination-critic-worker", status="DEGRADED",
        attempts=3, latency_ms=5311, error_count=1,
    )
    metadata = captured.events[-1]["metadata"]
    assert "llm_calls" not in metadata
    assert "model_name" not in metadata
    assert "prompt_tokens" not in metadata
    # 실제로 잰 값은 그대로 나가야 한다.
    assert metadata["status"] == "DEGRADED"
    assert metadata["attempts"] == 3
    assert metadata["latency_ms"] == 5311
    assert captured.events[-1]["level"] == "ERROR"
