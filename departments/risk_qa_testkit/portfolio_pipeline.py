"""Synthetic portfolio-recommendation flow through the Risk -> QA boundary.

이 모듈은 제품 목적을 검증하기 위한 TEST adapter다. 적합도 계산은 포트폴리오
도메인의 결정론적 계약이 소유하고, 기존 Risk/QA Worker Graph는 같은 읽기 전용
컨텍스트를 받아 독립 검증한다. 이 흐름도 주문·DB 쓰기·Production을 활성화하지
않는다.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from departments.risk_qa_testkit.pipeline import run_risk_qa_pipeline

ROOT = Path(__file__).resolve().parents[2]
_SUITABILITY_MODULE_NAME = "portfolio_suitability_testkit"


def _suitability_module() -> Any:
    if _SUITABILITY_MODULE_NAME in sys.modules:
        return sys.modules[_SUITABILITY_MODULE_NAME]
    path = ROOT / "departments/05-accounting-portfolio/portfolio/suitability.py"
    spec = importlib.util.spec_from_file_location(_SUITABILITY_MODULE_NAME, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"portfolio suitability contract unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[_SUITABILITY_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


def run_portfolio_recommendation_pipeline(
    profile: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    *,
    worker_runtime: str = "deterministic",
    worker_llm: Any = None,
    head_llm: Any = None,
) -> dict[str, Any]:
    """Run suitability matching, then Risk/QA TEST graphs with read-only context."""

    suitability = _suitability_module()
    result = suitability.recommend_portfolios(profile, candidates)
    suitability_context = {
        "status": result.status.value,
        "calculation_version": result.calculation_version,
        "input_hash": result.input_hash,
        "effective_risk_band": result.effective_risk_band.value,
        "recommendation_ids": [item.portfolio_id for item in result.recommendations],
        "exclusion_ids": [item.portfolio_id for item in result.exclusions],
        "binding": False,
    }

    # The existing TEST packet is canonical Research -> Risk -> QA evidence. Add
    # only a compact, non-sensitive suitability context to both department inputs.
    from departments.risk_qa_testkit.pipeline import make_test_packet

    packet = make_test_packet()
    packet.risk_input["portfolio_suitability"] = suitability_context
    packet.qa_input["portfolio_suitability"] = suitability_context
    if result.status == suitability.SuitabilityStatus.NO_MATCH:
        # Risk must see a safe rejection for an empty candidate set; this does not
        # change the production RiskEngine and remains non-binding TEST data.
        packet.risk_input["assessment"] = {
            "verdict": "reject",
            "source": "portfolio-suitability-test-gate",
        }

    risk_qa = run_risk_qa_pipeline(
        "test",
        packet=packet,
        worker_runtime=worker_runtime,
        worker_llm=worker_llm,
        head_llm=head_llm,
    )
    matched = result.status == suitability.SuitabilityStatus.MATCHED
    completed = risk_qa["pipeline_status"] == "COMPLETED"
    return {
        "purpose": "portfolio_recommendation",
        "pipeline_status": "COMPLETED" if completed else "DEGRADED",
        "safe_action": "NO_ACTION" if matched and completed else "HOLD",
        "manual_review_required": True,
        "suitability": result.model_dump(mode="json"),
        "suitability_context": suitability_context,
        "risk_qa": risk_qa,
        "stages": [
            {"stage": "investor_profile_validation", "status": "COMPLETED"},
            {
                "stage": "portfolio_suitability_match",
                "status": "COMPLETED",
                "decision": result.status.value,
            },
            {
                "stage": "risk_department_graph",
                "status": risk_qa["pipeline_status"],
                "binding": False,
            },
            {
                "stage": "qa_department_graph",
                "status": risk_qa["pipeline_status"],
                "binding": False,
            },
            {
                "stage": "portfolio_recommendation_result",
                "status": "COMPLETED" if matched else "NO_MATCH",
            },
        ],
    }


__all__ = ["run_portfolio_recommendation_pipeline"]
