"""Contract tests for the eight-department employee Worker layer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml

from orchestration.employee_dispatch import load_worker_specs, run_department_workers
from orchestration.workflows.manifest import load_workflow
from orchestration.workflows.runner import execute_workflow

ROOT = Path(__file__).resolve().parents[1]
DEPARTMENTS = (
    ("ceo", "00-ceo-office"),
    ("hr", "07-agent-workforce"),
    ("research", "01-research"),
    ("trading", "02-trading"),
    ("risk", "03-risk"),
    ("quant-backtest", "04-quant-backtest"),
    ("accounting-portfolio", "05-accounting-portfolio"),
    ("qa", "06-ai-qa-audit"),
)


# 부서별 본부장(Hermes) 모델 예외. 2026-08-10 현재 8개 부서가 같은 모델이라 비어 있다.
# 표를 남겨 두는 이유: 한 줄로 8개 부서를 묶어 두면 한 부서의 결정이 다른 부서를 잠근다.
HEAD_MODEL: dict[str, tuple[str, str]] = {}


def _read_profile(directory: str) -> str:
    """Read a department Hermes profile as UTF-8.

    ``Path.read_text()`` without an explicit encoding uses the platform default,
    which is cp949 on a Korean Windows install — every profile carries Korean
    comments, so the bare call raises UnicodeDecodeError there while passing on
    UTF-8 hosts.  The files are UTF-8 on every platform; say so.
    """

    return (ROOT / "departments" / directory / "hermes" / "config.yaml").read_text(
        encoding="utf-8"
    )


def _fake_worker_llm(_system: str, prompt: str) -> str:
    worker_id = next(
        (line.split(":", 1)[1].strip() for line in prompt.splitlines() if line.startswith("Worker id:")),
        "unknown-worker",
    )
    return json.dumps(
        {
            "summary": f"synthetic worker context for {worker_id}",
            "confidence": 0.8,
            "evidence_refs": ["test:evidence"],
            "escalate": False,
        }
    )


def _payload() -> dict[str, Any]:
    """Provide every optional trigger so the registry contract is exercised."""

    return {
        "case_request": {"case_id": "worker-contract-test", "symbol": "AAPL", "stage": "paper"},
        "research_packet": {"status": "COMPLETED"},
        "market_snapshot": {"price": "200.00"},
        "market_features": {"rsi": 50},
        "fundamentals": {"pe": 20},
        "news_or_macro": {"headline": "test"},
        "evidence_request": {"query": "test"},
        # ▶ QA 트리거. 이게 없어서 qa-runner 가 5개 도구에서 사실을 하나도 못 받고
        #   facts:{} -> SCHEMA_FAILURE -> ESCALATED 로 떨어졌다. **워커 동작은
        #   맞다** - 사실 없이 통과시키면 그게 사고다. 빠진 건 픽스처 쪽이었다.
        #   모양은 짐작하지 않고 QA본부 자체 테스트에서 그대로 가져왔다
        #   (departments/06-ai-qa-audit/tests/test_qa_employee_workers.py).
        # 이 테스트가 묻는 것은 "워커가 독립 그래프로 도는가" 이지 "QA 가 나쁜 입력을
        # 거르는가" 가 아니다(후자는 QA본부 자체 테스트 몫). 그래서 **통과하는 케이스**를
        # 준다 - 판정 엔진에 원본 입력을 주면 engine 이 decision 을 계산한다.
        # 모양은 departments/06-ai-qa-audit/tests/test_qa_employee_workers.py 에서 가져왔다.
        "assessment": {"decision": "PASS", "claim_checks": [{"result": "SUPPORTED"}]},
        "model_risk_input": {
            "model_id": "00000000-0000-0000-0000-000000000001",
            "model_version": "model-v1",
            "prompt_version": "prompt-v1",
            "dataset_version": "dataset-v1",
            "evaluation_count": 500,
            "accuracy": 0.9,
            "calibration_error": 0.02,
            "drift_score": 0.04,
            "protected_failure_rate": 0.01,
        },
        "internal_audit_events": [
            {
                "action": "qa.evidence.check",
                "department": "qa",
                "trace_id": "trace-qa-tool-1",
                "profile_status": "ACTIVE",
                "authorized": True,
            }
        ],
        "ops_assessment": {"status": "HEALTHY"},
        "permission_check": {"result": "ALLOWED"},
        "incident": {"incident_id": "incident-test"},
        "order_book": {"spread": "0.01"},
        "price_history": [200.0],
        "filings": {"published_at": "2026-08-03"},
        "news": {"headline": "test"},
        "macro": {"regime": "normal"},
        "geopolitical": {"risk": "low"},
        "evidence": {"source": "test"},
        "documents": [{"id": "test"}],
        "order_intent": {"side": "BUY", "quantity": 1},
        "risk_decision": {"verdict": "APPROVE"},
        "portfolio_state": {"position": 0},
        "approved_risk": {"status": "APPROVE"},
        "execution_request": {"venue": "paper"},
        "derivatives_signal": {"enabled": True},
        "assessment": {"claim_checks": [{"result": "UNSUPPORTED"}]},
        "trading_state": {"cash": "100000"},
        "compliance": {"grounded": True},
        "counterparty": {"status": "OK"},
        "derivatives": {"enabled": True},
        "strategy_hypothesis": {"id": "strategy-test"},
        "dataset": {"snapshot": "dataset-test"},
        "backtest_request": {"id": "backtest-request"},
        "backtest": {"run_id": "backtest-test"},
        "release_candidate": {"id": "release-test"},
        "ml_research": {"model": "baseline"},
        # 2026-08-10 공장 재편 trigger. 리서치는 방법론 수집(scout_cycle) -> 기획
        # (adopted_lead) -> 독립 반증(proposal_draft), 퀀트는 접수 -> 설계 -> 카드 ->
        # 결과 환류 순서로 켜진다.
        "scout_cycle": {"cycle_id": "scout-test", "lenses": ["academic"]},
        "adopted_lead": {"lead_id": "lead-test"},
        "proposal_draft": {"proposal_id": "proposal-test"},
        "methodology_leads": [{"lead_id": "lead-test"}],
        "universe": {"key": "krx_all"},
        "experiment_proposal": {"proposal_id": "proposal-test"},
        "experiment_design": {"windows": 5},
        "strategy_authoring": {"edge_type": "liquidity_shock_reversal"},
        "template_catalog": {"templates": 8},
        "experiment_card": {"card_id": "card-test"},
        "experiment_outcome": {"decision": "REJECT"},
        "trial_pressure": {"trial_number": 1},
        "regime_breakdown": {"BULL": {"n_windows": 1.0}},
        "failed_criteria": ["pbo"],
        # 서비스 자리(RES-18) - 사용자가 보유 종목을 물을 때만 켜진다.
        "holding_question": {"symbol": "005930"},
        "execution": {"cost": "0"},
        "cost_sensitivity": {"slippage": 0.0},
        "regime": {"label": "normal"},
        "regime_analysis": {"label": "normal"},
        "portfolio": {"nav": "100000"},
        "ledger": {"balanced": True},
        "nav": {"value": "100000"},
        "nav_close": {"status": "ready"},
        "treasury": {"cash": "100000"},
        "treasury_signal": {"status": "normal"},
        "pnl": {"value": "0"},
        "pnl_request": {"period": "test"},
        "reporting": {"period": "test"},
        "investor_report": {"period": "test"},
        "valuation": {"as_of": "2026-08-03"},
        "corporate_action": {"type": "none"},
        "fee_tax": {"status": "review"},
        "fee_accrual": {"period": "test"},
        "queue_metrics": {"depth": 1},
        "sla_metrics": {"breaches": 0},
        "cost_metrics": {"usd": 0},
        "profile": {"id": "profile-test"},
        "role_requirements": {"skills": []},
        "evaluation": {"score": 1},
        "performance_signal": {"score": 1},
        "lifecycle_event": {"type": "test"},
        "governance_request": {"id": "governance-test"},
        "model_risk": {"status": "PASS"},
        "internal_audit": {"status": "PASS"},
        "ops_assessment": {"status": "HEALTHY"},
        "permission_check": {"status": "ALLOWED"},
        "incident": {"incident_id": "incident-test"},
        "incident_events": [],
        "hallucination_reviews": [],
    }


def test_profile_worker_registry_counts_and_models() -> None:
    # 이 표는 **LLM 직원 수**다. 2026-08-06 트레이딩 tool 강등으로 7 -> 2 —
    # 결정론 desk-runner 는 config 의 deterministic_workers 로 빠져서 여기 안 센다
    # (조직 인원은 3명). 강등 기준은 departments/02-trading/hermes/config.yaml 참고.
    # 2026-08-06: risk-runner/qa-runner 흡수로 03-risk 3 -> 1, 06-ai-qa-audit 5 -> 2 —
    # 결정론 러너는 config 의 deterministic_workers 로 빠져서 여기 안 센다.
    # 2026-08-07: back-office-runner 흡수로 05-accounting-portfolio 8 -> 1. 회계는
    # 헌장상(마스터플랜 19.12) 에이전트 일이 "예외 조사와 설명" 하나뿐이라 도메인별로
    # 나뉘어 있던 7명이 전부 결정론 전달 계층이었다.
    # 2026-08-07: 07-agent-workforce 5 -> 0. 인사팀은 결정론 러너조차 두지 않았다 —
    # 타 부서의 tool 강등은 LLM 을 결정론 러너로 바꾼 것이지만, 인사팀은 그 판정을
    # 이미 일반 모듈(quality.py/access.py/workflow.py)이 갖고 있어 러너가 필요 없었다.
    # 2026-08-10: 07 은 0 -> 1. profile-architecture-worker 만 되살렸다(영주님) -
    # Adversarial Eval Case 설계는 정답이 규칙표에 없는 창작이라 결정론화 대상이
    # 아니고, 산출물이 QA 재검증을 거치는 비바인딩 제안이라 환각이 그대로 사고가
    # 되지 않는다.
    # 2026-08-11 재일: 01-research 8 -> 4, 04-quant-backtest 5 -> 2. 직원은
    # 병렬성·맥락격리·독립성·권한격리 중 하나가 명확할 때만 둔다 - 본부장이 순차로
    # 해도 되는 일은 본부장이 한다. 스카우트 4렌즈는 주기가 달라 2명이 시간 분리해
    # 맡고, 기획은 편집장 본업이라 흡수했고, market-context 는 data_resolution 이
    # 이미 커버리지를 실측하므로 폐지했다(서술이 코드 판정과 경쟁하면 안 된다).
    # 퀀트는 접수·설계가 실험당 1회라 병렬성이 없고 교훈 사상은 lessons_from() 이
    # 이미 결정론으로 한다 - 환류가 에이전트 가용성에 묶이면 안 된다.
    expected_counts = {"00-ceo-office": 1, "07-agent-workforce": 1, "01-research": 4, "02-trading": 0, "03-risk": 1, "04-quant-backtest": 2, "05-accounting-portfolio": 1, "06-ai-qa-audit": 2}
    for _, directory in DEPARTMENTS:
        config = yaml.safe_load(_read_profile(directory))
        workers = config["workers"]
        registry = config["staff_registry"]
        assert len(workers) == expected_counts[directory]
        assert registry["worker_count"] == expected_counts[directory]
        if directory == "02-trading":
            assert config["employee_runtime"]["topology"] == "dynamic_parallel_fan_out_fan_in"
            assert config["employee_runtime"]["provider"] == "none"
            assert config["employee_runtime"]["max_attempts"] == 1
            assert config["temporary_workers"]["one_worker_per_strategy"] is True
        else:
            assert (
                config["employee_runtime"]["topology"]
                == "async_fan_out_fan_in_independent_graphs"
            )
            assert config["employee_runtime"]["model_default"] == "qwen3:1.7b"
            assert config["employee_runtime"]["model_selection"]["active_model"] == "qwen3:1.7b"
            assert config["employee_runtime"]["max_retries"] == 2
            assert config["employee_runtime"]["max_attempts"] == 3
        # 2026-08-10: 본부장 모델이 부서마다 갈린다. 리서치·퀀트(재일)는 논문 정독과
        # 실험 코드 작성이 본부장 일이라 Opus 5 로 올렸다. 나머지 부서는 현행 유지 -
        # 한 줄로 8개 부서를 묶어 두면 한 부서의 결정이 다른 부서를 잠근다.
        expected_head = HEAD_MODEL.get(directory, ("openai-codex", "gpt-5.6-luna"))
        assert (config["model"]["provider"], config["model"]["default"]) == expected_head
        # `or` 로 폴백하면 빈 목록(직원 없는 부서)이 "키 없음"으로 잘못 읽힌다 - None 만 폴백한다.
        runtime_personalities = config["agent"].get("runtime_personalities")
        if runtime_personalities is None:
            runtime_personalities = registry["runtime_personalities"]
        assert set(runtime_personalities) == set(workers)


def test_all_registered_workers_are_independent_graphs() -> None:
    for department, _ in DEPARTMENTS:
        result = run_department_workers(ROOT, department, _payload(), llm=_fake_worker_llm)
        assert result["binding"] is False
        assert result["failed"] == []
        assert result["degraded"] is False
        assert result["not_executed"] == []
        if department == "trading":
            assert result["runtime"] == {
                "executor": "deterministic_strategy_worker",
                "topology": "dynamic_parallel_fan_out_fan_in",
                "provider": "none",
                "model": "none",
            }
        else:
            assert result["runtime"]["executor"] == "LangGraph"
            assert (
                result["runtime"]["topology"]
                == "async_fan_out_fan_in_independent_graphs"
            )
            assert result["runtime"]["provider"] == "ollama"
            assert result["runtime"]["model"] == "qwen3:1.7b"


def test_profile_worker_metadata_matches_runtime_specs() -> None:
    """Prevent config registry drift from the executable WorkerSpec registry."""

    for department, directory in DEPARTMENTS:
        config = yaml.safe_load(_read_profile(directory))
        runtime_specs = {
            spec.worker_id: spec for spec in load_worker_specs(ROOT, department)
        }
        configured_workers = config["workers"]
        assert set(configured_workers) == set(runtime_specs)

        for worker_id, spec in runtime_specs.items():
            entry = configured_workers[worker_id]
            assert entry["role"] == spec.role
            assert entry["trigger"] == spec.trigger
            assert tuple(entry["tools"]) == spec.tools
            expected_status = "active" if spec.trigger == "always" else "conditional"
            assert entry["status"] == expected_status


def test_worker_failure_is_degraded_and_non_binding() -> None:
    def failing_llm(_system: str, _prompt: str) -> str:
        raise TimeoutError("synthetic worker timeout")

    result = run_department_workers(ROOT, "risk", _payload(), llm=failing_llm)
    assert result["binding"] is False
    assert result["degraded"] is True
    assert result["failed"]
    # risk-runner는 결정론이라 LLM 실패와 무관하게 항상 COMPLETED다 - LLM Worker만 본다.
    llm_workers = [item for item in result["workers"] if item["worker_id"] != "risk-runner"]
    assert llm_workers
    assert all(item["status"] == "DEGRADED" for item in llm_workers)


def test_paper_pipeline_passes_worker_context_to_department_head(monkeypatch: Any) -> None:
    from orchestration.adapters import paper_pipeline

    dispatched: list[str] = []

    def worker_result(department: str) -> dict[str, Any]:
        return {
            "department": department,
            "status": "COMPLETED",
            "binding": False,
        "runtime": {"executor": "LangGraph", "provider": "ollama", "model": "qwen3:1.7b"},
            "executed": [f"{department}-worker"],
            "failed": [],
            "not_executed": [],
            "input_hash": "paper-test-hash",
        }

    def fake_dispatch(_repo_root: Path, department: str, _payload: dict[str, Any]) -> dict[str, Any]:
        dispatched.append(department)
        return worker_result(department)

    class FakeCeoAdapter:
        def decide(self, *, case_request: dict[str, Any], department_reports: dict[str, Any]) -> dict[str, Any]:
            assert case_request["symbol"] == "AAPL"
            for department in ("research", "trading", "risk", "qa", "accounting"):
                assert department_reports[department]["employee_context"] is not None
            return {
                "recommendation": "HOLD",
                "model_recommendation": "HOLD",
                "confidence": 1.0,
                "rationale": "synthetic paper handoff",
                "escalate": True,
                "binding_decision": "HOLD / ESCALATE",
                "binding": False,
                "runtime": {"profile": "ceo-agent", "call_status": "succeeded"},
            }

    monkeypatch.setattr(paper_pipeline, "run_department_workers", fake_dispatch)
    adapter = paper_pipeline.PaperPipelineAdapter(
        ROOT,
        research_runner=lambda _symbol: {"status": "COMPLETED", "employee_execution": {"workers": []}},
        risk_runner=lambda *_args, **_kwargs: {
            "verdict": "reject",
            "employee_workers": worker_result("risk"),
        },
        qa_runner=lambda *_args, **_kwargs: {
            "verdict": "FAIL",
            "employee_workers": worker_result("qa"),
        },
        ceo_adapter=FakeCeoAdapter(),
    )
    case = {
        "case_id": "paper-worker-handoff",
        "symbol": "AAPL",
        "side": "BUY",
        "quantity": 1,
        "order_type": "LIMIT",
        "limit_price": "200.00",
        "stage": "paper",
    }
    run = execute_workflow(
        load_workflow("investment-case"),
        mode="paper",
        handlers=adapter.handlers(),
        context={"case_request": case},
        run_id="paper-worker-handoff",
    )
    assert run.status == "COMPLETED"
    assert dispatched == ["research", "trading", "accounting-portfolio", "ceo"]

    for workflow_name in ("strategy-research", "workforce-management", "agent-evolution"):
        auxiliary_run = execute_workflow(
            load_workflow(workflow_name),
            mode="paper",
            handlers=adapter.handlers(),
            context={"case_request": case},
            run_id=f"{workflow_name}-paper-test",
        )
        assert auxiliary_run.status == "COMPLETED"


def test_final_worker_shape_has_no_duplicate_roles() -> None:
    """Keep the approved head/worker topology explicit and reviewable."""
    expected = {
        "ceo": (1, 1, 0),
        # 2026-08-10: profile-architecture-worker 1명. trigger 가 채용·개정 요청일 때만
        # 이라 조건부다 - 평시에 인사팀 LLM 이 도는 것이 아니다.
        "hr": (1, 0, 1),
        # 2026-08-11: 4명 전부 소집형이다. 상시였던 market-context 를 폐지하면서
        # 상시가 0 이 됐다 - 스카우트·회의론자를 상시로 켜두면 편집장이 읽지 못하는
        # 리드만 쌓인다.
        "research": (4, 0, 4),
        # 고정 LLM 직원은 없고 전략별 결정론 Worker를 요청 단위로 생성한다.
        "trading": (0, 0, 0),
        # 2026-08-06: Risk의 계산·검사는 risk-runner로 이동해 LLM 1명만 남겼다.

        "risk": (1, 0, 1),
        # 2026-08-10: 상시는 접수 하나다. 카드도 없는데 해석 워커를 돌릴 이유가 없다.
        # 2026-08-11: 2명 전부 소집형. 상시였던 proposal-intake 를 본부장이 흡수했다.
        "quant-backtest": (2, 0, 2),
        # 2026-08-07: 회계의 도메인별 수치 전달은 back-office-runner로 이동해 LLM 1명만
        # 남겼다. 남은 하나는 도메인이 아니라 **예외**로 정의된 조사관이라 항상 실행이다.
        "accounting-portfolio": (1, 1, 0),
        # 2026-08-06: QA의 결정론 검사는 qa-runner로 이동해 LLM 2명만 남겼다.

        "qa": (2, 0, 2),
    }
    for department, directory in DEPARTMENTS:
        config = yaml.safe_load(_read_profile(directory))
        workers = config["workers"]
        active = sum(item["trigger"] == "always" for item in workers.values())
        conditional = len(workers) - active
        assert (len(workers), active, conditional) == expected[department]
        assert len({item["role"] for item in workers.values()}) == len(workers)
