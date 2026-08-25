from __future__ import annotations

import asyncio
import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from orchestration.langsmith_feedback import (
    FeedbackLedger,
    evaluate_observation,
    observation_from_run,
)
from orchestration.qa_discord_feedback import (
    QA_FEEDBACK_MARKER,
    artifact_id_from_text,
    format_qa_feedback_request,
    parse_qa_feedback_command,
)

_PATCH_PATH = Path(__file__).parents[2] / "deploy" / "hermes-discord" / "gateway_patch.py"
_QA_SOUL_PATH = Path(__file__).parents[2] / "departments" / "06-ai-qa-audit" / "hermes" / "SOUL.md"
_SPEC = importlib.util.spec_from_file_location("qa_feedback_gateway_patch", _PATCH_PATH)
assert _SPEC and _SPEC.loader
gateway_patch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gateway_patch)


ARTIFACT_ID = "feedback-0123456789abcdef0123456789abcdef"


def test_qa_card_and_commands_keep_only_redacted_contract() -> None:
    card = format_qa_feedback_request(
        artifact_id=ARTIFACT_ID,
        department="risk",
        decision="IMPROVEMENT_CANDIDATE",
        finding_codes=("LATENCY_ABOVE_THRESHOLD",),
        summaries=("end-to-end latency exceeded threshold",),
        metadata={
            "source_project": "First",
            "source_name": "hgfinance.user-query",
            "source_run_id": "run-redacted-1",
            "request_id": "request-redacted-1",
            "task_id": "t_hr_primary",
            "latency_ms": 154_910,
            "latency_threshold_ms": 60_000,
            "latency_scope": "end_to_end",
            "prompt": "must-not-appear",
        },
    )

    assert card.startswith(QA_FEEDBACK_MARKER)
    assert "## ① 자동 감지 · QA 검토 요청" in card
    assert "### 관측" in card
    assert "### 증거 키 · 원문 payload 제외" in card
    assert "### 관리자 결정" in card
    assert "보류:" in card and "`PENDING` 유지" in card
    assert "154.91s > 기준 60.00s (end_to_end)" in card
    assert "source_run_id: run-redacted-1" in card
    assert "trace_name: hgfinance.user-query" in card
    assert "department_task_id: t_hr_primary" in card
    assert "request_id: request-redacted-1" in card
    assert "must-not-appear" not in card
    approved = parse_qa_feedback_command(f"승인 {ARTIFACT_ID} 기준 충족")
    assert approved is not None
    assert approved.decision == "APPROVED"
    assert approved.artifact_id == ARTIFACT_ID
    assert approved.reason == "기준 충족"
    missing_reason = parse_qa_feedback_command(f"승인 {ARTIFACT_ID}")
    assert missing_reason is not None and missing_reason.reason == ""
    rejected = parse_qa_feedback_command("반려 재현 실패")
    assert rejected is not None and rejected.decision == "REJECTED"
    assert rejected.artifact_id is None
    assert artifact_id_from_text(card) == ARTIFACT_ID


def test_qa_hermes_profile_requires_distinct_structured_review() -> None:
    soul = _QA_SOUL_PATH.read_text(encoding="utf-8")

    assert "## ② QA Hermes 검토 결과" in soul
    assert "**검토 의견:**" in soul
    assert "**근거 충족도:**" in soul
    assert "### 아직 확인되지 않은 점" in soul
    assert "### 관리자 판단 가이드" in soul
    assert "never write `승인 완료` or `거부 완료`" in soul


def test_duration_is_used_when_trace_has_no_latency_metadata() -> None:
    class Run:
        id = "run-duration"
        name = "hgfinance.user-query"
        status = "success"
        start_time = datetime(2026, 8, 25, tzinfo=timezone.utc)
        end_time = start_time + timedelta(seconds=154.91)
        extra: dict[str, object] = {
            "metadata": {
                "request_id": "discord:1",
                "department": "ceo-terminal",
                "status": "completed",
                "trace_kind": "workflow_root",
                "latency_scope": "end_to_end",
            }
        }

    observation = observation_from_run(Run())
    assert observation.metadata["latency_ms"] == 154_910
    result = evaluate_observation(observation, latency_warn_ms=60_000)
    assert "LATENCY_ABOVE_THRESHOLD" in result.finding_codes
    assert result.metadata["latency_scope"] == "end_to_end"
    assert result.metadata["latency_threshold_ms"] == 60_000
    assert "end-to-end latency exceeded" in result.summaries[0]
    assert "worker latency" not in result.summaries[0]


def test_discord_delivery_claim_is_one_shot(tmp_path) -> None:
    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    assert ledger.enqueue("source-1", "First")
    assert ledger.claim() is not None
    artifact_id = ledger.complete(
        "source-1",
        "eval-1",
        evaluate_observation(
            type("Observation", (), {
                "metadata": {"request_id": "r1", "stage": "risk", "status": "error", "latency_ms": 70_000},
                "status": "error",
                "source_run_id": "source-1",
                "name": "worker.risk",
                "department": "risk",
                "workflow_role": "primary",
            })()
        ),
    )
    assert ledger.claim_discord_delivery(artifact_id) is True
    ledger.finish_discord_delivery(artifact_id, delivered=False, error_code="timeout")
    assert ledger.claim_discord_delivery(artifact_id) is False


def test_gateway_routes_authorized_qa_decision_without_invoking_hermes(tmp_path) -> None:
    class Role:
        id = "900"

    class Author:
        id = "42"
        bot = False
        roles: tuple[Role, ...] = (Role(),)

    class Channel:
        id = "1541636723006775477"
        parent_id = None

    class Guild:
        id = "guild-1"

    class Message:
        id = "message-1"
        content = f"승인 {ARTIFACT_ID} 재현 완료"
        author = Author()
        channel = Channel()
        guild = Guild()
        reference = None

        def __init__(self) -> None:
            self.replies: list[str] = []

        async def reply(self, content, mention_author=False):
            self.replies.append(content)

    class Adapter:
        _client = type("Client", (), {"user": object()})()

    message = Message()
    with patch.dict(
        "os.environ",
        {
            "HERMES_HOME": str(tmp_path),
            "HERMES_PROFILE": "qa-department",
            "QA_DISCORD_CHANNEL_ID": "1541636723006775477",
            "QA_DISCORD_APPROVER_ROLE_IDS": "900",
        },
        clear=False,
    ), patch.object(
        gateway_patch,
        "submit_qa_feedback_decision",
        return_value=(200, {"status": "APPROVED"}),
    ) as submit:
        handled = asyncio.run(gateway_patch._maybe_handle_qa_feedback_message(Adapter(), message))

    assert handled is True
    submit.assert_called_once()
    assert "## ✅ 관리자 결정 기록" in message.replies[0]
    assert "`APPROVED`" in message.replies[0]
    assert "offline benchmark PENDING" in message.replies[0]
    assert "자동 변경:** 없음" in message.replies[0]


def test_gateway_rejects_bare_approval_before_calling_ledger(tmp_path) -> None:
    class Author:
        id = "42"
        bot = False
        roles: tuple[object, ...] = ()

    class Channel:
        id = "1541636723006775477"
        parent_id = None

    class Guild:
        id = "guild-1"

    class Message:
        id = "message-no-reason"
        content = f"승인 {ARTIFACT_ID}"
        author = Author()
        channel = Channel()
        guild = Guild()
        reference = None

        def __init__(self) -> None:
            self.replies: list[str] = []

        async def reply(self, content, mention_author=False):
            self.replies.append(content)

    class Adapter:
        _client = type("Client", (), {"user": object()})()

    message = Message()
    with patch.dict(
        "os.environ",
        {
            "HERMES_HOME": str(tmp_path),
            "HERMES_PROFILE": "qa-department",
            "QA_DISCORD_CHANNEL_ID": "1541636723006775477",
            "QA_DISCORD_APPROVER_USER_IDS": "42",
        },
        clear=False,
    ), patch.object(gateway_patch, "submit_qa_feedback_decision") as submit:
        handled = asyncio.run(gateway_patch._maybe_handle_qa_feedback_message(Adapter(), message))

    assert handled is True
    submit.assert_not_called()
    assert "결정 사유가 필요합니다" in message.replies[0]
