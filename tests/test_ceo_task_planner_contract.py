"""CEO task planner 계약 테스트.

`PORTFOLIO_CEO_TASK_PLANNER_MODE=llm` 은 opt-in 이라 평소 CI 에서 안 돈다. 그래서
켜는 순간 드러나는 결함이 조용히 쌓인다 - 2026-08-10 에 실제로 두 건 있었다.

  1. LLM 경로 반환값이 BFF 의 PortfolioTaskPlan(extra="forbid") 계약을 위반해
     라우팅을 켜면 응답이 422 가 됐다.
  2. allow-list 가 **상한**만 검사해 LLM 이 ["ceo"] 만 요청해도 통과했다 -
     qa 를 건너뛰면 인용·환각 검증 없이 자문이 사용자에게 나간다.

두 경로(성공/실패) 모두 결정론 경로와 **같은 응답 모양**을 유지하는지 고정한다.
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from apps.api.portfolio_schemas import PortfolioTaskPlan  # noqa: E402
from orchestration.adapters.ceo_task_planner import (  # noqa: E402
    REQUIRED_DEPARTMENTS,
    CeoTaskPlannerError,
    LlmCeoTaskPlanner,
    _parse_plan,
    build_task_plan,
)
from orchestration.workflows.portfolio_recommendation import (  # noqa: E402
    DEPARTMENTS,
    build_ceo_task_plan,
)

_PROFILE = {"category": "MARKET_RESEARCH", "query": "삼전 지금 사도 돼?"}


class _StubPlanner(LlmCeoTaskPlanner):
    """플랜을 그대로 돌려주는 대역. hermes 바이너리·자격증명 없이 경로만 검증한다."""

    plan_result: dict[str, Any] = {}

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:  # noqa: D107
        pass

    def plan(self, **_kwargs: Any) -> dict[str, Any]:
        return dict(self.plan_result)


class _FailingPlanner(LlmCeoTaskPlanner):
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:  # noqa: D107
        pass

    def plan(self, **_kwargs: Any) -> dict[str, Any]:
        raise CeoTaskPlannerError("ceo_planner_exit_1")


class CeoTaskPlannerContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self._saved = os.environ.get("PORTFOLIO_CEO_TASK_PLANNER_MODE")

    def tearDown(self) -> None:
        if self._saved is None:
            os.environ.pop("PORTFOLIO_CEO_TASK_PLANNER_MODE", None)
        else:
            os.environ["PORTFOLIO_CEO_TASK_PLANNER_MODE"] = self._saved

    def _build(self, planner_cls: type[LlmCeoTaskPlanner]) -> dict[str, Any]:
        return build_task_plan(
            _PROFILE,
            deterministic_fallback=build_ceo_task_plan,
            valid_departments=DEPARTMENTS,
            planner_cls=planner_cls,
        )

    def test_deterministic_mode_adds_no_planner_fields(self) -> None:
        """기본 모드는 LLM 필드를 붙이지 않는다 - 응답 모양이 그대로여야 한다."""

        os.environ["PORTFOLIO_CEO_TASK_PLANNER_MODE"] = "deterministic"
        plan = self._build(_FailingPlanner)
        PortfolioTaskPlan.model_validate(plan)
        self.assertNotIn("planner_fallback_reason", plan)
        self.assertEqual(plan["routing_basis"], "bounded_query_intent_router")

    def test_llm_success_satisfies_bff_contract(self) -> None:
        """LLM 성공 경로가 PortfolioTaskPlan(extra=forbid)을 통과해야 한다."""

        os.environ["PORTFOLIO_CEO_TASK_PLANNER_MODE"] = "llm"
        _StubPlanner.plan_result = {
            "mode": "llm_task_plan",
            "category": "MARKET_RESEARCH",
            "original_query": _PROFILE["query"],
            "rewritten_query": "삼성전자 매수 적정성 검토",
            "requested_departments": ["research", "qa", "ceo"],
            "matched_terms": {},
            "routing_basis": "ceo_llm_task_planner",
            "mandate_considered": False,
            "planner_rationale": "단순 종목 질의",
            "runtime": {"profile": "ceo-agent", "provider": "openai-codex", "model": "gpt-5.6-luna"},
        }
        plan = self._build(_StubPlanner)
        PortfolioTaskPlan.model_validate(plan)
        self.assertEqual(plan["routing_basis"], "ceo_llm_task_planner")
        self.assertEqual(plan["requested_departments"], ["research", "qa", "ceo"])

    def test_llm_success_keeps_caller_only_envelope_fields(self) -> None:
        """planner 가 모르는 필드(workflow 등)가 기본값으로 덮이지 않아야 한다.

        ceo_task_planner 는 workflows 를 import 하지 않아 CATEGORY_WORKFLOWS 를
        계산할 수 없다. 결정론 계획을 봉투 기반으로 쓰는 이유가 이것이다.
        """

        os.environ["PORTFOLIO_CEO_TASK_PLANNER_MODE"] = "llm"
        strategy_profile = {"category": "STRATEGY_PROPOSAL", "query": "모멘텀 전략 어때?"}
        _StubPlanner.plan_result = {
            "mode": "llm_task_plan",
            "category": "STRATEGY_PROPOSAL",
            "original_query": strategy_profile["query"],
            "rewritten_query": "모멘텀 전략 타당성 검토",
            "requested_departments": ["research", "quant", "qa", "ceo"],
            "matched_terms": {},
            "routing_basis": "ceo_llm_task_planner",
            "mandate_considered": False,
            "planner_rationale": "전략 평가 요청",
            "runtime": {"profile": "ceo-agent", "provider": "openai-codex", "model": "gpt-5.6-luna"},
        }
        plan = build_task_plan(
            strategy_profile,
            deterministic_fallback=build_ceo_task_plan,
            valid_departments=DEPARTMENTS,
            planner_cls=_StubPlanner,
        )
        PortfolioTaskPlan.model_validate(plan)
        self.assertEqual(plan["workflow"], "strategy-research")
        self.assertTrue(plan["category_recognized"])

    def test_llm_failure_falls_back_and_records_reason(self) -> None:
        """실패는 요청을 막지 않고 결정론으로 되돌아가되 이유를 남긴다."""

        os.environ["PORTFOLIO_CEO_TASK_PLANNER_MODE"] = "llm"
        plan = self._build(_FailingPlanner)
        PortfolioTaskPlan.model_validate(plan)
        self.assertEqual(plan["planner_fallback_reason"], "ceo_planner_exit_1")
        self.assertEqual(plan["routing_basis"], "bounded_query_intent_router")

    def test_allow_list_rejects_department_outside_ceiling(self) -> None:
        with self.assertRaises(ValueError):
            _parse_plan(
                '{"requested_departments":["hr"],"rewritten_query":"q","rationale":"r"}',
                DEPARTMENTS,
            )

    def test_required_departments_are_restored_when_llm_omits_them(self) -> None:
        """qa 를 빠뜨린 계획은 거부가 아니라 보강한다 - 검증 없는 자문을 막는다."""

        for requested in ('["ceo"]', '["research","ceo"]', '["research"]'):
            with self.subTest(requested=requested):
                parsed = _parse_plan(
                    '{"requested_departments":%s,"rewritten_query":"q","rationale":"r"}' % requested,
                    DEPARTMENTS,
                )
                self.assertTrue(
                    REQUIRED_DEPARTMENTS.issubset(parsed["requested_departments"]),
                    parsed["requested_departments"],
                )

    def test_department_order_follows_caller_canonical_sequence(self) -> None:
        parsed = _parse_plan(
            '{"requested_departments":["ceo","qa","research"],"rewritten_query":"q","rationale":"r"}',
            DEPARTMENTS,
        )
        self.assertEqual(parsed["requested_departments"], ["research", "qa", "ceo"])

    def test_empty_rationale_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _parse_plan(
                '{"requested_departments":["qa","ceo"],"rewritten_query":"q","rationale":"  "}',
                DEPARTMENTS,
            )


if __name__ == "__main__":
    unittest.main()
