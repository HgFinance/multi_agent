"""Unit tests for the opt-in LLM CEO task planner and its fail-closed dispatcher."""

from __future__ import annotations

import json
import os
import subprocess
import unittest
from pathlib import Path
from unittest import mock

from orchestration.adapters.ceo_task_planner import (
    CeoTaskPlannerError,
    LlmCeoTaskPlanner,
    _parse_plan,
    build_task_plan,
)
from orchestration.skill_contract import CanonicalSkillError

VALID_DEPARTMENTS = ("research", "trading", "risk", "qa", "accounting", "ceo")


def _fallback(profile):
    return {
        "mode": "deterministic",
        "requested_departments": ["research"],
        "fallback_profile_query": profile.get("query"),
    }


class _FakePlanner:
    """Mirrors LlmCeoTaskPlanner's constructor(repo_root)/.plan(...) shape."""

    result: dict | None = None
    error: Exception | None = None
    received: dict | None = None

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = repo_root

    def plan(self, *, profile, mandate_policy, valid_departments):
        type(self).received = {
            "profile": profile,
            "mandate_policy": mandate_policy,
            "valid_departments": tuple(valid_departments),
        }
        if type(self).error is not None:
            raise type(self).error
        return type(self).result


class ParsePlanTest(unittest.TestCase):
    def test_valid_response_is_reordered_to_canonical_department_order(self) -> None:
        stdout = json.dumps(
            {
                "requested_departments": ["qa", "research"],
                "rewritten_query": "AAPL 포트폴리오 점검",
                "rationale": "리서치 후 QA 검증이 필요합니다.",
                "required_skills": ["financial-portfolio-assessment"],
            }
        )
        decision = _parse_plan(stdout, VALID_DEPARTMENTS)
        # allow-list 는 상한만 정하고 REQUIRED_DEPARTMENTS({qa, ceo})가 하한이다.
        # LLM 이 ceo 를 빼도 파싱 단계에서 되살아난다(ceo_task_planner.py 34~39행).
        self.assertEqual(decision["requested_departments"], ["research", "qa", "ceo"])
        self.assertEqual(decision["rationale"], "리서치 후 QA 검증이 필요합니다.")
        self.assertEqual(decision["required_skills"], ["financial-portfolio-assessment"])

    def test_out_of_allowlist_department_is_rejected(self) -> None:
        stdout = json.dumps(
            {
                "requested_departments": ["research", "not-a-real-department"],
                "rewritten_query": "q",
                "rationale": "r",
            }
        )
        with self.assertRaises(ValueError):
            _parse_plan(stdout, VALID_DEPARTMENTS)

    def test_unknown_required_skill_is_rejected_before_child_creation(self) -> None:
        stdout = json.dumps(
            {
                "requested_departments": ["research"],
                "rewritten_query": "q",
                "rationale": "r",
                "required_skills": ["unknown-skill"],
            }
        )
        with self.assertRaises(ValueError):
            _parse_plan(stdout, VALID_DEPARTMENTS)

    def test_required_skill_owner_must_be_selected(self) -> None:
        stdout = json.dumps(
            {
                "requested_departments": ["quant"],
                "rewritten_query": "q",
                "rationale": "r",
                "required_skills": ["methodology-scout"],
            }
        )
        with self.assertRaises(ValueError):
            _parse_plan(stdout, VALID_DEPARTMENTS)

    def test_empty_rationale_is_rejected(self) -> None:
        stdout = json.dumps({"requested_departments": ["research"], "rewritten_query": "q", "rationale": ""})
        with self.assertRaises(ValueError):
            _parse_plan(stdout, VALID_DEPARTMENTS)

    def test_missing_json_object_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            _parse_plan("not json at all", VALID_DEPARTMENTS)


class LlmCeoTaskPlannerTest(unittest.TestCase):
    def _planner(self) -> LlmCeoTaskPlanner:
        return LlmCeoTaskPlanner(Path.cwd(), executable="fake-hermes", timeout=5)

    def test_successful_plan_shape(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["fake-hermes"],
            returncode=0,
            stdout=json.dumps(
                {
                    "requested_departments": ["risk", "research"],
                    "rewritten_query": "AAPL 위험 점검",
                    "rationale": "리스크 우선 검토가 필요합니다.",
                }
            ),
            stderr="",
        )
        with mock.patch("orchestration.adapters.ceo_task_planner.subprocess.run", return_value=completed):
            result = self._planner().plan(
                profile={"query": "AAPL 위험 점검", "category": "risk"},
                mandate_policy={"risk_bounds": {"max_position_pct": 0.1}},
                valid_departments=VALID_DEPARTMENTS,
            )
        self.assertEqual(result["mode"], "llm_task_plan")
        # qa·ceo 감사/응답 의도는 REQUIRED_DEPARTMENTS 하한으로 되살아난다.
        self.assertEqual(result["requested_departments"], ["research", "risk", "qa", "ceo"])
        self.assertTrue(result["mandate_considered"])
        self.assertEqual(result["runtime"]["provider"], "openai-codex")

    def test_nonzero_exit_raises_planner_error(self) -> None:
        completed = subprocess.CompletedProcess(args=["fake-hermes"], returncode=1, stdout="", stderr="boom")
        with mock.patch("orchestration.adapters.ceo_task_planner.subprocess.run", return_value=completed), self.assertRaises(CeoTaskPlannerError):
            self._planner().plan(profile={"query": "q"}, mandate_policy=None, valid_departments=VALID_DEPARTMENTS)

    def test_missing_executable_raises_planner_error(self) -> None:
        with mock.patch(
            "orchestration.adapters.ceo_task_planner.subprocess.run",
            side_effect=FileNotFoundError(),
        ), self.assertRaises(CeoTaskPlannerError):
            self._planner().plan(profile={"query": "q"}, mandate_policy=None, valid_departments=VALID_DEPARTMENTS)

    def test_timeout_raises_planner_error(self) -> None:
        with mock.patch(
            "orchestration.adapters.ceo_task_planner.subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="fake-hermes", timeout=5),
        ), self.assertRaises(CeoTaskPlannerError):
            self._planner().plan(profile={"query": "q"}, mandate_policy=None, valid_departments=VALID_DEPARTMENTS)

    def test_invalid_json_raises_planner_error(self) -> None:
        completed = subprocess.CompletedProcess(args=["fake-hermes"], returncode=0, stdout="not json", stderr="")
        with mock.patch("orchestration.adapters.ceo_task_planner.subprocess.run", return_value=completed), self.assertRaises(CeoTaskPlannerError):
            self._planner().plan(profile={"query": "q"}, mandate_policy=None, valid_departments=VALID_DEPARTMENTS)


class BuildTaskPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        _FakePlanner.result = None
        _FakePlanner.error = None
        _FakePlanner.received = None

    def test_deterministic_mode_is_default_and_never_calls_planner(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("PORTFOLIO_CEO_TASK_PLANNER_MODE", None)
            plan = build_task_plan(
                {"query": "AAPL 리서치"},
                deterministic_fallback=_fallback,
                valid_departments=VALID_DEPARTMENTS,
                planner_cls=_FakePlanner,
            )
        self.assertEqual(plan["mode"], "deterministic")
        self.assertIsNone(_FakePlanner.received)

    def test_llm_mode_valid_response_is_used_unchanged(self) -> None:
        _FakePlanner.result = {
            "mode": "llm_task_plan",
            "requested_departments": ["research", "qa"],
            "planner_rationale": "needs research then qa",
        }
        with mock.patch.dict(os.environ, {"PORTFOLIO_CEO_TASK_PLANNER_MODE": "llm"}):
            plan = build_task_plan(
                {"query": "포트폴리오 점검", "mandate_policy": {"risk_bounds": {}}},
                deterministic_fallback=_fallback,
                valid_departments=VALID_DEPARTMENTS,
                planner_cls=_FakePlanner,
            )
        self.assertEqual(plan["mode"], "llm_task_plan")
        self.assertEqual(plan["requested_departments"], ["research", "qa"])
        self.assertEqual(_FakePlanner.received["mandate_policy"], {"risk_bounds": {}})
        self.assertEqual(_FakePlanner.received["valid_departments"], VALID_DEPARTMENTS)

    def test_llm_mode_planner_error_falls_back_to_deterministic(self) -> None:
        _FakePlanner.error = CeoTaskPlannerError("ceo_planner_timeout")
        with mock.patch.dict(os.environ, {"PORTFOLIO_CEO_TASK_PLANNER_MODE": "llm"}):
            plan = build_task_plan(
                {"query": "AAPL 리서치"},
                deterministic_fallback=_fallback,
                valid_departments=VALID_DEPARTMENTS,
                planner_cls=_FakePlanner,
            )
        self.assertEqual(plan["mode"], "deterministic")
        self.assertEqual(plan["planner_fallback_reason"], "ceo_planner_timeout")

    def test_llm_mode_unexpected_exception_falls_back_to_deterministic(self) -> None:
        _FakePlanner.error = RuntimeError("boom")
        with mock.patch.dict(os.environ, {"PORTFOLIO_CEO_TASK_PLANNER_MODE": "llm"}):
            plan = build_task_plan(
                {"query": "AAPL 리서치"},
                deterministic_fallback=_fallback,
                valid_departments=VALID_DEPARTMENTS,
                planner_cls=_FakePlanner,
            )
        self.assertEqual(plan["mode"], "deterministic")
        self.assertIn("ceo_planner_unexpected", plan["planner_fallback_reason"])

    def test_unresolvable_skill_is_not_silently_dropped_by_fallback(self) -> None:
        _FakePlanner.error = CanonicalSkillError("unknown skill")
        with mock.patch.dict(os.environ, {"PORTFOLIO_CEO_TASK_PLANNER_MODE": "llm"}), self.assertRaises(CanonicalSkillError):
            build_task_plan(
                {"query": "AAPL portfolio assessment"},
                deterministic_fallback=_fallback,
                valid_departments=VALID_DEPARTMENTS,
                planner_cls=_FakePlanner,
            )

    def test_missing_mandate_policy_is_passed_through_as_none(self) -> None:
        _FakePlanner.result = {
            "mode": "llm_task_plan",
            "requested_departments": ["research"],
            "planner_rationale": "no mandate available",
        }
        with mock.patch.dict(os.environ, {"PORTFOLIO_CEO_TASK_PLANNER_MODE": "llm"}):
            build_task_plan(
                {"query": "AAPL 리서치"},
                deterministic_fallback=_fallback,
                valid_departments=VALID_DEPARTMENTS,
                planner_cls=_FakePlanner,
            )
        self.assertIsNone(_FakePlanner.received["mandate_policy"])


if __name__ == "__main__":
    unittest.main()
