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
import contextlib
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@contextlib.contextmanager
def department_path(*relative: str):
    """부서 디렉터리를 **이 블록 동안만** sys.path 에 올린다.

    ▶ 모듈 최상단에서 sys.path 에 심으면 안 된다(2026-08-20 실측). 부서들은
      `from repository import ...` 처럼 **평범한 이름**으로 형제 모듈을 부르는데,
      경로가 스위트 전체에 남으면 다른 테스트의 같은 이름이 엉뚱한 부서 파일로
      해석된다 - tests/api/test_qa_domain_mandate_api.py 가 QA 의 repository 대신
      회계의 repository 를 집어 ImportError 로 죽었다. 계측 테스트가 남의 테스트를
      깨는 형태라 원인을 찾기도 어렵다.
    """

    added = [str(ROOT / part) for part in relative]
    sys.path[:0] = added
    try:
        yield
    finally:
        for entry in added:
            if entry in sys.path:
                sys.path.remove(entry)


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
    # 실행까지 블록 **안**에서 한다 - 부서 모듈은 함수 안에서 형제 모듈을 늦게
    # import 하므로(qa_runtime 등) 경로를 먼저 걷으면 호출 시점에 죽는다.
    with department_path("departments/03-risk"):
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
    with department_path("departments/06-ai-qa-audit"):
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

    with department_path("departments/03-risk"):
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


# ---------------------------------------------------------------------------
# 2026-08-20: 점유율 분모(not_executed) + 부서장 턴
# ---------------------------------------------------------------------------


def test_not_executed_workers_publish_opportunity_not_execution(
    captured: _FakeLangfuseClient,
) -> None:
    """미발화는 **다른 이름 공간**으로 나가야 한다.

    실행 이벤트와 같은 이름으로 보내면 HR 유휴 판정이 "돌았다"로 읽어, 한 번도
    실행된 적 없는 Worker 가 ACTIVE 로 뜬다 - 관측을 고치려다 관측을 망가뜨린다.
    """

    conditional = WorkerSpec("gated-worker", "Gated", ("demo.tool",), "when_signal_exists")
    run_worker_registry(
        (conditional,),
        {"unrelated": 1},  # trigger 미충족
        tools={"gated-worker": lambda value: {"tool": "demo", "value": value}},
        llm=_llm,
        stage="research",
    )
    names = _names(captured)
    assert "llm.performance.opportunity:research:gated-worker" in names
    assert "llm.performance.metric:research:gated-worker" not in names


def test_opportunity_and_execution_together_make_a_fire_rate(
    captured: _FakeLangfuseClient,
) -> None:
    """같은 Worker 의 실행 1건 + 미발화 1건이 각각 세어져야 발화율이 나온다."""

    spec = WorkerSpec("gated-worker", "Gated", ("demo.tool",), "when_signal_exists")
    tools = {"gated-worker": lambda value: {"tool": "demo", "value": value}}
    run_worker_registry((spec,), {"when_signal_exists": True}, tools=tools, llm=_llm, stage="qa")
    run_worker_registry((spec,), {"unrelated": 1}, tools=tools, llm=_llm, stage="qa")

    executions = [e for e in captured.events if e["name"].startswith("llm.performance.metric:")]
    opportunities = [e for e in captured.events if e["name"].startswith("llm.performance.opportunity:")]
    assert len(executions) == 1
    assert len(opportunities) == 1
    assert opportunities[0]["metadata"]["reason"] == "trigger_not_fired"
    assert opportunities[0]["input"] is None and opportunities[0]["output"] is None


def test_head_turn_publishes_with_profile_persona(captured: _FakeLangfuseClient) -> None:
    """부서장 턴이 Profile 의 head_persona 이름으로 기록돼야 한다.

    write(hermes_boundary) 와 read(scorecard/observability) 가 **같은 파일의 같은
    키**를 읽는지 고정한다 - 다른 출처를 보면 조용히 어긋나 부서장이 영원히
    UNOBSERVED 로 남는다.
    """

    with department_path("apps/api", "departments/07-agent-workforce/scorecard"):
        import hermes_boundary
        from observability import load_head_profile_spec

    hermes_boundary._publish_head_turn(
        department="research-department", started=0.0, status="COMPLETED"
    )
    persona = observability.head_persona_for_profile("research-department")
    assert persona, "Profile 에서 head_persona 를 못 읽으면 계측이 통째로 빠진다"
    assert f"llm.performance.metric:research:{persona}" in _names(captured)
    assert captured.events[-1]["metadata"]["source"] == "bff_ask"

    # write(BFF) 와 read(HR) 가 같은 신원을 봐야 한다 - 다르면 부서장이 영원히
    # UNOBSERVED 로 남는다.
    read_side = load_head_profile_spec(ROOT, "research")
    assert read_side is not None and read_side.worker_id == persona


def test_unknown_profile_is_not_published_under_a_guessed_stage(
    captured: _FakeLangfuseClient,
) -> None:
    """표에 없는 프로필은 이름으로 stage 를 지어내지 않고 계측을 포기한다."""

    with department_path("apps/api"):
        import hermes_boundary

    hermes_boundary._publish_head_turn(
        department="not-a-real-department", started=0.0, status="COMPLETED"
    )
    assert captured.events == []


def test_every_known_profile_resolves_to_a_stage_and_persona() -> None:
    """부를 수 있는 프로필은 전부 stage·신원이 풀려야 한다.

    하나라도 안 풀리면 그 부서장만 조용히 계측에서 빠진다 - 실행은 정상이고
    이벤트만 안 나가므로 어떤 테스트도 안 깨진다.
    """

    with department_path("apps/api"):
        import hermes_boundary

    for profile in hermes_boundary.PROFILE_CONTAINERS:
        assert observability.stage_for_profile(profile), f"{profile} stage 미해석"
        assert observability.head_persona_for_profile(profile), f"{profile} head_persona 미해석"


def test_stage_names_match_the_hr_read_side() -> None:
    """write 가 쓰는 stage 와 HR 이 조회하는 stage 가 같아야 한다."""

    with department_path("departments/07-agent-workforce/scorecard"):
        from observability import INVESTMENT_DEPARTMENT_STAGE

    written = {observability.stage_for_profile(p) for p in
               ("research-department", "trading-department", "risk-management",
                "quant-backtest-department", "accounting-portfolio-department", "qa-department")}
    assert written == set(INVESTMENT_DEPARTMENT_STAGE.values())


def test_heads_are_excluded_from_the_default_report() -> None:
    """기본 응답 인원이 말없이 늘면 과거 문장의 뜻이 바뀐다 - opt-in 이어야 한다."""

    from datetime import datetime, timezone

    with department_path("departments/07-agent-workforce/scorecard"):
        from observability import check_idle_agents

    now = datetime(2026, 8, 20, tzinfo=timezone.utc)

    class _None:
        def latest_event_timestamp(self, *, event_name: str, since: Any) -> None:
            return None

    default = check_idle_agents(reader=_None(), departments=("research",), now=now)
    with_heads = check_idle_agents(
        reader=_None(), departments=("research",), now=now, include_heads=True
    )
    assert len(with_heads) == len(default) + 1
    assert "research-methodology-head" in {r.worker_id for r in with_heads}
    assert "research-methodology-head" not in {r.worker_id for r in default}


# ---------------------------------------------------------------------------
# Discord/웹 사용자 질의 경로 (칸반 카드)
#
# 이 경로는 BFF 가 부서장을 직접 부르지 않는다 - 카드를 만들고 Hermes 게이트웨이가
# 자기 컨테이너 안에서 실행한다. 우리 코드가 "그 부서장이 일을 끝냈다"를 아는 자리는
# ceo-supervisor 의 terminal event 관측 하나뿐이다.
# ---------------------------------------------------------------------------


def _supervisor_publisher():
    from orchestration.adapters.ceo_supervisor import CeoSupervisorService

    return CeoSupervisorService._publish_head_card_activity


@pytest.mark.parametrize(
    ("kind", "status", "errors"),
    [("completed", "COMPLETED", 0), ("done", "COMPLETED", 0),
     ("blocked", "BLOCKED", 0), ("failed", "DEGRADED", 1)],
)
def test_card_terminal_event_publishes_head_activity(
    captured: _FakeLangfuseClient, kind: str, status: str, errors: int
) -> None:
    """카드 종료가 부서장 1턴으로 기록되고, 실패·차단도 관측 사실로 남아야 한다."""

    publish = _supervisor_publisher()
    publish(
        object(),  # self - 이 메서드는 인스턴스 상태를 쓰지 않는다
        task_id="task-1",
        kind=kind,
        event={"assignee": "research-department"},
    )
    persona = observability.head_persona_for_profile("research-department")
    assert f"llm.performance.metric:research:{persona}" in _names(captured)
    metadata = captured.events[-1]["metadata"]
    assert metadata["status"] == status
    assert metadata["error_count"] == errors
    assert metadata["source"] == "kanban_card"
    # 카드 종료는 지속시간을 모른다 - 0 으로 채우면 "즉시 끝났다"로 읽힌다.
    assert "latency_ms" not in metadata


def test_card_event_without_a_known_assignee_publishes_nothing(
    captured: _FakeLangfuseClient,
) -> None:
    """모르는 프로필의 stage 를 이름으로 지어내지 않는다."""

    publish = _supervisor_publisher()
    publish(object(), task_id="t", kind="completed", event={"assignee": "made-up-profile"})
    publish(object(), task_id="t", kind="completed", event={})
    assert captured.events == []


def test_bff_and_card_paths_use_the_same_head_identity() -> None:
    """두 write 경로가 같은 부서장을 같은 이름으로 불러야 합쳐진다."""

    with department_path("apps/api"):
        import hermes_boundary

    for profile in hermes_boundary.PROFILE_CONTAINERS:
        assert observability.head_persona_for_profile(profile) ==             observability.head_persona_for_profile(profile)
        assert observability.stage_for_profile(profile)


# ---------------------------------------------------------------------------
# 2026-08-20: 실측치(토큰·호출수) 전달
#
# record_llm_call() 은 네 모델 경로 전부에서 이미 불리고 있었는데, 그 값을 담을
# begin_worker_metric() 컨텍스트가 portfolio_recommendation 에서만 열려서 나머지
# 경로의 측정치가 매번 버려지고 있었다. 여기서 고정하는 것은 두 가지다:
#   (1) 실행기가 컨텍스트를 열어 토큰이 이벤트에 실린다
#   (2) 그 전달이 **async fan-out 을 건너서도** 유지된다 - contextvars 는 Task
#       생성 시점에 복사되므로, 컨텍스트를 잘못된 자리에서 열면 조용히 0 이 된다
# ---------------------------------------------------------------------------


class _Usage:
    def __init__(self, prompt: int, completion: int) -> None:
        self.prompt_tokens = prompt
        self.completion_tokens = completion


class _OllamaUsage:
    prompt_eval_count = 31
    eval_count = 17


def test_record_llm_call_accepts_native_ollama_usage_fields() -> None:
    token = observability.begin_worker_metric(
        worker_id="demo-worker",
        role="Demo",
        stage="research",
        model_name="qwen3:1.7b",
    )
    try:
        observability.record_llm_call(usage=_OllamaUsage())
        measured = observability.end_worker_metric(
            token, status="COMPLETED", attempts=1, eval_score=None
        )
    except Exception:
        observability.end_worker_metric(
            token, status="DEGRADED", attempts=1, eval_score=None
        )
        raise

    assert measured["prompt_tokens"] == 31
    assert measured["completion_tokens"] == 17


def _token_reporting_llm(_system: str, _prompt: str) -> str:
    """실제 Worker LLM 과 같은 자리에서 record_llm_call 을 부르는 대역."""

    observability.record_llm_call(usage=_Usage(31, 17), latency_ms=42)
    return '{"summary":"ok","confidence":0.8,"evidence_refs":["tool"],"escalate":false}'


def test_shared_runtime_publishes_measured_tokens(captured: _FakeLangfuseClient) -> None:
    run_worker_registry(
        (_demo_spec(),),
        {"anything": 1},
        tools={"demo-worker": lambda value: {"tool": "demo", "value": value}},
        llm=_token_reporting_llm,
        stage="research",
    )
    metadata = captured.events[-1]["metadata"]
    assert metadata["prompt_tokens"] == 31
    assert metadata["completion_tokens"] == 17
    assert metadata["llm_calls"] == 1
    assert metadata["model_name"], "model_name 이 비면 비용 집계에서 모델을 못 가른다"


def test_measured_tokens_survive_async_fan_out(captured: _FakeLangfuseClient) -> None:
    """워커 두 명이 동시에 돌아도 각자의 토큰이 섞이지 않아야 한다.

    contextvars 는 Task 마다 복사본을 갖는다 - 컨텍스트를 fan-out 바깥에서 열면
    두 워커의 호출이 한 metric 에 합쳐지거나 서로를 덮어쓴다.
    """

    specs = (
        WorkerSpec("worker-a", "A", ("demo.tool",), "always"),
        WorkerSpec("worker-b", "B", ("demo.tool",), "always"),
    )
    tools = {spec.worker_id: (lambda value: {"tool": "demo", "value": value}) for spec in specs}
    run_worker_registry(specs, {"anything": 1}, tools=tools,
                        llm=_token_reporting_llm, stage="qa")

    by_worker = {
        event["metadata"]["worker_id"]: event["metadata"]
        for event in captured.events
        if event["name"].startswith("llm.performance.metric:")
    }
    assert set(by_worker) == {"worker-a", "worker-b"}
    for worker_id, metadata in by_worker.items():
        assert metadata["llm_calls"] == 1, f"{worker_id} 의 호출수가 섞였다: {metadata}"
        assert metadata["prompt_tokens"] == 31, f"{worker_id} 토큰 누락/합산: {metadata}"


def test_unmeasured_run_still_omits_token_fields(captured: _FakeLangfuseClient) -> None:
    """모델을 안 부른 실행은 토큰 필드가 아예 없어야 한다 - 0 은 관측 사실로 읽힌다."""

    run_worker_registry(
        (_demo_spec(),),
        {"anything": 1},
        tools={"demo-worker": lambda value: {"tool": "demo", "value": value}},
        llm=_llm,  # record_llm_call 을 부르지 않는 대역
        stage="research",
    )
    metadata = captured.events[-1]["metadata"]
    assert "prompt_tokens" not in metadata
    assert "completion_tokens" not in metadata
    assert metadata.get("llm_calls", 0) == 0 or "llm_calls" not in metadata
