from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from orchestration.hr_langfuse_feedback import (
    build_hr_langfuse_evaluation,
    publish_hr_langfuse_review,
)
from orchestration.langsmith_feedback import FeedbackLedger
from orchestration.qa_discord_feedback import (
    HR_LANGFUSE_FEEDBACK_MARKER,
    format_hr_langfuse_feedback_request,
    parse_qa_feedback_command,
)

_PATCH_PATH = Path(__file__).parents[2] / "deploy" / "hermes-discord" / "gateway_patch.py"
_SPEC = importlib.util.spec_from_file_location("hr_langfuse_gateway_patch", _PATCH_PATH)
assert _SPEC and _SPEC.loader
gateway_patch = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gateway_patch)

ARTIFACT_ID = "feedback-0123456789abcdef0123456789abcdef"


def _observability() -> SimpleNamespace:
    return SimpleNamespace(
        window_start=SimpleNamespace(isoformat=lambda: "2026-08-27T11:00:00+00:00"),
        window_end=SimpleNamespace(isoformat=lambda: "2026-08-27T12:00:00+00:00"),
        idle_agents=(SimpleNamespace(status="ACTIVE"),),
        capacity=(
            SimpleNamespace(
                status="MEASURED",
                department="research",
                duration_p95_ms=70_000,
                arrivals=2,
                error_rate=0.0,
                retry_rate=0.1,
            ),
        ),
        llm_usage=(SimpleNamespace(status="MEASURED"),),
        worker_usage=(SimpleNamespace(status="MEASURED", llm_calls=3),),
        trigger_rates=(SimpleNamespace(status="ACTIVE"),),
        langfuse_queries=2,
    )


def test_hr_langfuse_card_is_korean_bounded_and_redacted() -> None:
    result = build_hr_langfuse_evaluation(_observability())
    card = format_hr_langfuse_feedback_request(
        artifact_id=ARTIFACT_ID,
        decision=result.decision,
        finding_codes=result.finding_codes,
        summaries=result.summaries,
        metadata={**result.metadata, "prompt": "must-not-appear"},
    )

    assert card.startswith(HR_LANGFUSE_FEEDBACK_MARKER)
    assert "HR · Langfuse 관측 요약 및 관리자 결정 요청" in card
    assert "인사 부서" in card
    assert "주요 병목: **리서치 부서 (70.00초)**" in card
    assert "근거 좌표 · 원문 제외" in card
    assert "원문 입력·출력 전송: 없음" in card
    assert "must-not-appear" not in card
    assert len(card) <= 1900

    rejected = parse_qa_feedback_command(f"미승인 {ARTIFACT_ID} 재현되지 않음")
    assert rejected is not None
    assert rejected.decision == "REJECTED"
    assert rejected.artifact_id == ARTIFACT_ID
    assert rejected.reason == "재현되지 않음"


def test_hr_langfuse_publisher_uses_shared_ledger_and_one_discord_attempt() -> None:
    class Ledger:
        def __init__(self) -> None:
            self.completed = []
            self.finished = []

        def complete(self, source_run_id, eval_run_id, result):
            self.completed.append((source_run_id, eval_run_id, result))
            return ARTIFACT_ID

        def claim_discord_delivery(self, artifact_id):
            return artifact_id == ARTIFACT_ID

        def finish_discord_delivery(self, artifact_id, **kwargs):
            self.finished.append((artifact_id, kwargs))

    ledger = Ledger()
    with (
        patch.dict(
            "os.environ",
            {
                "HR_LANGFUSE_REVIEW_MODE": "active",
                "HR_LANGFUSE_CHANNEL_ID": "1542405626531942432",
                "DISCORD_BOT_TOKEN_HR": "configured-but-not-printed",
            },
            clear=False,
        ),
        patch(
            "orchestration.hr_langfuse_feedback.post_hr_langfuse_discord_message",
            return_value="discord-message-1",
        ) as post,
        patch(
            "orchestration.hr_langfuse_feedback.verify_discord_message_delivery",
            return_value=True,
        ) as readback,
    ):
        status = publish_hr_langfuse_review(_observability(), ledger=ledger)

    assert status == "DELIVERED"
    assert len(ledger.completed) == 1
    assert ledger.finished[0][1]["delivered"] is True
    post.assert_called_once()
    assert post.call_args.kwargs["channel_id"] == "1542405626531942432"
    readback.assert_called_once()


def test_unavailable_langfuse_signal_is_approvable_as_an_actionable_finding(
    tmp_path,
) -> None:
    observed = _observability()
    observed.capacity = (SimpleNamespace(status="UNAVAILABLE", reason="reader"),)
    result = build_hr_langfuse_evaluation(observed)
    assert "LANGFUSE_OBSERVABILITY_UNAVAILABLE" in result.finding_codes

    ledger = FeedbackLedger(str(tmp_path / "feedback.sqlite3"))
    artifact_id = ledger.complete("hr-source-1", "hr-eval-1", result)
    assert ledger.approve(
        artifact_id,
        "APPROVED",
        "hr-admin",
        "재현 가능한 관측 장애",
        improvement_type="RUNTIME_CONFIG",
    )


def test_hr_gateway_routes_authorized_rejection_to_shared_ledger_without_hermes(
    tmp_path,
) -> None:
    class Author:
        id = "42"
        bot = False
        roles = ()

    class Channel:
        id = "1542405626531942432"
        parent_id = None

    class Guild:
        id = "guild-1"
        owner_id = "owner-1"

    class Message:
        id = "hr-decision-1"
        content = f"미승인 {ARTIFACT_ID} 재현 근거 부족"
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
    with (
        patch.dict(
            "os.environ",
            {
                "HERMES_HOME": str(tmp_path),
                "HERMES_PROFILE": "hr-department",
                "HR_LANGFUSE_CHANNEL_ID": "1542405626531942432",
                "HR_LANGFUSE_APPROVER_USER_IDS": "42",
            },
            clear=False,
        ),
        patch.object(
            gateway_patch,
            "submit_qa_feedback_decision",
            return_value=(200, {"status": "REJECTED"}),
        ) as submit,
    ):
        handled = asyncio.run(
            gateway_patch._maybe_handle_hr_langfuse_message(Adapter(), message)
        )

    assert handled is True
    submit.assert_called_once()
    assert "## ⛔ HR Langfuse 관리자 결정" in message.replies[0]
    assert "`REJECTED`" in message.replies[0]
    assert "자동 변경:** 없음" in message.replies[0]
