"""부서장 카드 계측이 조용히 빠지는 경우를 막는 회귀 테스트.

이 파일이 잡으려는 실패는 전부 **같은 모양**이다: 계측이 예외 없이 성공한 것처럼
보이는데 어디에도 안 찍히는 것. 그런 실패는 대시보드가 비어 있을 때에야 발견되고,
그때는 이미 그 기간의 데이터가 없다.
"""

from __future__ import annotations

import json
import unittest

from orchestration.canonical_profiles import (
    _DEPARTMENT_BY_CANONICAL_PROFILE,
)
from orchestration.head_span_builder import build_card_spans, card_span_id
from orchestration.langfuse_otlp import (
    ATTR_COST_DETAILS,
    ATTR_OBSERVATION_METADATA,
    ATTR_USAGE_DETAILS,
    RedactionError,
    build_span,
)
from orchestration.trace_identity import is_span_id, is_trace_id, trace_id_for
from scripts.head_card_trace import DEPARTMENT_BY_PROFILE


def _attributes(span: dict) -> dict:
    return {item["key"]: item["value"] for item in span["attributes"]}


def _metadata(span: dict) -> dict:
    packed = _attributes(span).get(ATTR_OBSERVATION_METADATA)
    return json.loads(packed["stringValue"]) if packed else {}


class DepartmentTableTest(unittest.TestCase):
    """부서 코드 표가 정본과 갈리면 그 부서장만 조용히 빠진다."""

    def test_matches_canonical_profiles(self) -> None:
        # head_card_trace 는 Hermes 이미지에서 도느라 canonical_profiles 를
        # import 하지 않고 값을 복사해 둔다. 복사본이 정본과 어긋나는 순간
        # 그 프로필의 span 은 "unknown_profile" 로 접혀 사라진다.
        self.assertEqual(
            DEPARTMENT_BY_PROFILE, dict(_DEPARTMENT_BY_CANONICAL_PROFILE)
        )

    def test_covers_every_department_head(self) -> None:
        for profile in (
            "ceo-agent",
            "research-department",
            "trading-department",
            "risk-management",
            "quant-backtest-department",
            "accounting-portfolio-department",
            "qa-department",
            "hr-department",
        ):
            self.assertIn(profile, DEPARTMENT_BY_PROFILE, profile)


class TraceShapeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = "t_aed63d47"
        self.spans = build_card_spans(
            root_id=self.root,
            task_id="t_6a665d49",
            department="research",
            head_persona="autonomous-quant-researcher",
            profile="research-department",
            status="COMPLETED",
            started_ms=1_787_795_186_000,
            ended_ms=1_787_795_237_600,
        )

    def test_department_card_hangs_under_the_root_card(self) -> None:
        # 카드마다 wrapper 프로세스가 따로 돌고 서로를 모른다. 그래도 트리가 되는
        # 것은 부모 id 가 루트 id 만으로 계산되기 때문이다 - 이게 깨지면 카드가
        # 전부 고아 trace 로 흩어진다.
        head = self.spans[0]
        self.assertEqual(head["traceId"], trace_id_for(self.root))
        self.assertEqual(
            head["parentSpanId"], card_span_id(root_id=self.root, task_id=self.root)
        )
        self.assertTrue(is_trace_id(head["traceId"]))
        self.assertTrue(is_span_id(head["spanId"]))

    def test_root_card_has_no_parent(self) -> None:
        root_spans = build_card_spans(
            root_id=self.root, task_id=self.root, department="ceo",
            head_persona="executive-orchestrator", profile="ceo-agent",
            status="COMPLETED", started_ms=1, ended_ms=2,
        )
        self.assertNotIn("parentSpanId", root_spans[0])

    def test_same_card_twice_produces_identical_ids(self) -> None:
        # 재시도·중복 관측이 span 을 두 배로 만들면 안 된다. 결정론 id 라
        # 같은 관측은 덮어쓴다.
        again = build_card_spans(
            root_id=self.root, task_id="t_6a665d49", department="research",
            head_persona="autonomous-quant-researcher", profile="research-department",
            status="COMPLETED", started_ms=1_787_795_186_000,
            ended_ms=1_787_795_237_600,
        )
        self.assertEqual(
            [s["spanId"] for s in self.spans], [s["spanId"] for s in again]
        )

    def test_unknown_root_publishes_nothing(self) -> None:
        self.assertEqual(
            build_card_spans(
                root_id="", task_id="t_1", department="research", head_persona="x",
                profile="research-department", status="COMPLETED",
                started_ms=1, ended_ms=2,
            ),
            [],
        )

    def test_missing_session_still_reports_the_head_turn(self) -> None:
        # 토큰을 못 붙였다고 턴 자체를 안 남기면, 그 카드는 "일이 없었다"로 읽힌다.
        self.assertEqual(len(self.spans), 1)
        self.assertEqual(_metadata(self.spans[0])["token_source"], "missing")


class RedactionTest(unittest.TestCase):
    """원문은 어떤 경로로도 나갈 수 없어야 한다."""

    def test_forbidden_metadata_raises(self) -> None:
        for leaked in ("input", "output", "prompt", "task_body", "tool_result"):
            with self.subTest(leaked=leaked):
                with self.assertRaises(RedactionError):
                    build_span(
                        trace_id=trace_id_for("t_1"),
                        span_id=card_span_id(root_id="t_1", task_id="t_1"),
                        name="x", start_ms=0, end_ms=1,
                        metadata={leaked: "민감한 본문"},
                    )

    def test_card_spans_never_carry_free_text(self) -> None:
        for span in self.build():
            attributes = _attributes(span)
            for key in attributes:
                self.assertNotIn("input", key.rsplit(".", 1)[-1])
                self.assertNotIn("output", key.rsplit(".", 1)[-1])
            self.assertFalse(_metadata(span).get("raw_payloads_sent", False))

    @staticmethod
    def build() -> list[dict]:
        return build_card_spans(
            root_id="t_aed63d47", task_id="t_6a665d49", department="research",
            head_persona="x", profile="research-department", status="COMPLETED",
            started_ms=1, ended_ms=2,
        )


class UsageAndCostTest(unittest.TestCase):
    def _usage(self):
        from orchestration.hermes_session_usage import TurnSegment, TurnUsage

        return TurnUsage(
            session_id="20260827_014614_a2ee73", source="kanban",
            model_name="gpt-5.6-luna", started_ms=1_787_795_186_365,
            ended_ms=1_787_795_237_554, end_reason="", message_count=21,
            tool_call_count=11, api_call_count=12, input_tokens=64449,
            output_tokens=1590, cache_read_tokens=180_000, cache_write_tokens=0,
            reasoning_tokens=900, billing_provider="openai-codex",
            billing_mode="subscription_included", cost_status="included",
            cost_source="none", system_prompt_hash="3919c914",
            parent_session_id="",
            segments=(
                TurnSegment("model", "model.generate", 1_787_795_186_400, 1_787_795_190_000, 0),
                TurnSegment("tool", "tool.research.evidence.search", 1_787_795_190_000, 1_787_795_191_200, 1),
            ),
        )

    def _spans(self):
        return build_card_spans(
            root_id="t_aed63d47", task_id="t_6a665d49", department="research",
            head_persona="x", profile="research-department", status="COMPLETED",
            started_ms=1_787_795_186_000, ended_ms=1_787_795_237_600,
            usage=self._usage(), session_confidence="window",
        )

    def test_tokens_are_reported_once_at_session_scope(self) -> None:
        # state.db 의 messages.token_count 는 전부 NULL 이다. 구간별 토큰을
        # 지어내면 그 숫자를 인용한 비용·용량 판단이 조용히 틀린다.
        spans = self._spans()
        carriers = [s for s in spans if ATTR_USAGE_DETAILS in _attributes(s)]
        self.assertEqual(len(carriers), 1)
        usage = json.loads(_attributes(carriers[0])[ATTR_USAGE_DETAILS]["stringValue"])
        self.assertEqual(usage["input"], 64449)
        self.assertEqual(usage["output"], 1590)
        self.assertEqual(usage["total"], 64449 + 1590 + 180_000 + 900)

    def test_cost_is_absent_until_a_rate_exists(self) -> None:
        # gpt-5.6-luna 는 구독이라 토큰당 청구서가 없다. 단가가 정해지기 전 비용은
        # 0 이 아니라 '없음'이어야 한다 - 0 은 "공짜로 돈다"로 읽힌다.
        for span in self._spans():
            self.assertNotIn(ATTR_COST_DETAILS, _attributes(span))

    def test_segments_preserve_order_and_bounds(self) -> None:
        segments = [s for s in self._spans() if s["name"].startswith(("model.", "tool."))]
        self.assertEqual(
            [s["name"] for s in segments],
            ["model.generate", "tool.research.evidence.search"],
        )
        for span in segments:
            self.assertLessEqual(
                int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"])
            )


if __name__ == "__main__":
    unittest.main()
