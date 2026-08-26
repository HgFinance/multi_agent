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
    SKILL_PROPOSAL_MARKER,
    artifact_id_from_text,
    format_qa_feedback_request,
    format_skill_proposal_request,
    parse_qa_feedback_command,
    proposal_id_from_text,
)

_PATCH_PATH = (
    Path(__file__).parents[2] / "deploy" / "hermes-discord" / "gateway_patch.py"
)
_QA_SOUL_PATH = (
    Path(__file__).parents[2] / "departments" / "06-ai-qa-audit" / "hermes" / "SOUL.md"
)
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
    approved = parse_qa_feedback_command(
        f"승인 {ARTIFACT_ID} 유형=SKILL_CREATE 기준 충족"
    )
    assert approved is not None
    assert approved.decision == "APPROVED"
    assert approved.artifact_id == ARTIFACT_ID
    assert approved.reason == "기준 충족"
    assert approved.improvement_type == "SKILL_CREATE"
    missing_reason = parse_qa_feedback_command(f"승인 {ARTIFACT_ID}")
    assert missing_reason is not None and missing_reason.reason == ""
    rejected = parse_qa_feedback_command("반려 재현 실패")
    assert rejected is not None and rejected.decision == "REJECTED"
    assert rejected.artifact_id is None
    assert artifact_id_from_text(card) == ARTIFACT_ID


def test_qa_card_separates_bottleneck_joint_owners_and_observation_point() -> None:
    card = format_qa_feedback_request(
        artifact_id=ARTIFACT_ID,
        department="trading-department",
        decision="IMPROVEMENT_CANDIDATE",
        finding_codes=("LATENCY_ABOVE_THRESHOLD",),
        summaries=("end-to-end latency exceeded threshold",),
        metadata={
            "primary_bottleneck_department": "trading-department",
            "primary_bottleneck_duration_ms": 61_000,
            "joint_improvement_targets": "ceo-workflow / observability",
            "observation_point": "ceo-ingress",
            "latency_attribution_status": "MEASURED",
            "latency_scope": "end_to_end",
            "latency_ms": 98_590,
        },
    )

    assert "**주요 병목:** `trading-department`" in card
    assert "**공동 개선 대상:** `ceo-workflow / observability`" in card
    assert "**관측 시작 지점:** `ceo-ingress` (원인 부서 아님)" in card
    assert "대상 부서:** `ceo-ingress`" not in card


def test_qa_hermes_profile_requires_distinct_structured_review() -> None:
    soul = _QA_SOUL_PATH.read_text(encoding="utf-8")

    assert "## ② QA Hermes 검토 결과" in soul
    assert "**검토 의견:**" in soul
    assert "**근거 충족도:**" in soul
    assert "### 아직 확인되지 않은 점" in soul
    assert "### 관리자 판단 가이드" in soul
    assert "never write `승인 완료` or `거부 완료`" in soul
    assert "`주요 병목`" in soul
    assert "`공동 개선 대상`" in soul
    assert "`관측 시작 지점`" in soul


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
            type(
                "Observation",
                (),
                {
                    "metadata": {
                        "request_id": "r1",
                        "stage": "risk",
                        "status": "error",
                        "latency_ms": 70_000,
                    },
                    "status": "error",
                    "source_run_id": "source-1",
                    "name": "worker.risk",
                    "department": "risk",
                    "workflow_role": "primary",
                },
            )()
        ),
    )
    assert ledger.claim_discord_delivery(artifact_id) is True
    ledger.finish_discord_delivery(artifact_id, delivered=False, error_code="timeout")
    assert ledger.claim_discord_delivery(artifact_id) is False


def test_gateway_routes_authorized_qa_decision_without_invoking_hermes(
    tmp_path,
) -> None:
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
        content = f"승인 {ARTIFACT_ID} 유형=SKILL_CREATE 재현 완료"
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
                "HERMES_PROFILE": "qa-department",
                "QA_DISCORD_CHANNEL_ID": "1541636723006775477",
                "QA_DISCORD_APPROVER_ROLE_IDS": "900",
            },
            clear=False,
        ),
        patch.object(
            gateway_patch,
            "submit_qa_feedback_decision",
            return_value=(200, {"status": "APPROVED"}),
        ) as submit,
    ):
        handled = asyncio.run(
            gateway_patch._maybe_handle_qa_feedback_message(Adapter(), message)
        )

    assert handled is True
    submit.assert_called_once()
    assert "## ✅ 관리자 결정 기록" in message.replies[0]
    assert "`APPROVED`" in message.replies[0]
    assert "offline benchmark PENDING" in message.replies[0]
    assert "자동 변경:** 없음" in message.replies[0]


def test_non_qa_gateway_never_turns_feedback_approval_into_ceo_workflow(
    tmp_path,
) -> None:
    class Author:
        id = "42"
        bot = False

    class Channel:
        id = "1541636723006775477"
        parent_id = None

    class Message:
        id = "message-owned-by-qa"
        content = f"승인 {ARTIFACT_ID} 근본 원인 해결 필요"
        author = Author()
        channel = Channel()
        guild = type("Guild", (), {"id": "guild-1"})()
        reference = None

    class Adapter:
        _client = type("Client", (), {"user": object()})()

    with (
        patch.dict(
            "os.environ",
            {
                "HERMES_HOME": str(tmp_path),
                "HERMES_PROFILE": "ceo-agent",
                "QA_DISCORD_CHANNEL_ID": "1541636723006775477",
            },
            clear=False,
        ),
        patch.object(gateway_patch, "_claim_inbound") as claim,
        patch.object(gateway_patch, "submit_qa_feedback_decision") as submit,
    ):
        handled = asyncio.run(
            gateway_patch._maybe_handle_qa_feedback_message(Adapter(), Message())
        )

    assert handled is True
    claim.assert_not_called()
    submit.assert_not_called()


def test_skill_proposal_card_and_reply_bind_second_approval_to_hashes(
    tmp_path,
) -> None:
    proposal_id = "risk-timeout-v1-0123456789ab"
    card = format_skill_proposal_request(
        proposal_id=proposal_id,
        slug="risk-timeout",
        version=1,
        owner_profile="risk-management",
        content_hash="a" * 64,
        provenance_hash="b" * 64,
        diff_hash="c" * 64,
        source_artifact_ids=(ARTIFACT_ID,),
        benchmark_ids=("offline-v1",),
        validation={"stages": {"structure_and_provenance": "PASS"}},
    )
    assert card.startswith(SKILL_PROPOSAL_MARKER)
    assert proposal_id_from_text(card) == proposal_id
    assert "a" * 64 in card and "b" * 64 in card and "c" * 64 in card

    class Author:
        id = "42"
        bot = False
        roles: tuple[object, ...] = ()

    class Channel:
        id = "1541636723006775477"
        parent_id = None

    class Guild:
        id = "guild-1"

    class Reference:
        message_id = "proposal-card"
        resolved = type("Resolved", (), {"content": card})()

    class Message:
        id = "proposal-decision"
        content = "승인 회귀 검증 확인"
        author = Author()
        channel = Channel()
        guild = Guild()
        reference = Reference()

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
                "HERMES_PROFILE": "qa-department",
                "QA_DISCORD_CHANNEL_ID": "1541636723006775477",
                "QA_DISCORD_APPROVER_USER_IDS": "42",
            },
            clear=False,
        ),
        patch.object(
            gateway_patch,
            "submit_skill_proposal_decision",
            return_value=(
                200,
                {
                    "status": "APPROVED",
                    "content_hash": "a" * 64,
                    "next_step": "CONTROL_PLANE_PROMOTION",
                },
            ),
        ) as submit,
    ):
        handled = asyncio.run(
            gateway_patch._maybe_handle_qa_feedback_message(Adapter(), message)
        )

    assert handled is True
    submit.assert_called_once()
    assert proposal_id in message.replies[0]
    assert "CONTROL_PLANE_PROMOTION" in message.replies[0]

    direct = Message()
    direct.id = "proposal-direct-decision"
    direct.content = f"승인 {proposal_id} 검토 생략"
    direct.reference = None
    with (
        patch.dict(
            "os.environ",
            {
                "HERMES_HOME": str(tmp_path),
                "HERMES_PROFILE": "qa-department",
                "QA_DISCORD_CHANNEL_ID": "1541636723006775477",
                "QA_DISCORD_APPROVER_USER_IDS": "42",
            },
            clear=False,
        ),
        patch.object(gateway_patch, "submit_skill_proposal_decision") as direct_submit,
    ):
        assert (
            asyncio.run(
                gateway_patch._maybe_handle_qa_feedback_message(Adapter(), direct)
            )
            is True
        )
    direct_submit.assert_not_called()
    assert "Reply해야" in direct.replies[0]


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
    with (
        patch.dict(
            "os.environ",
            {
                "HERMES_HOME": str(tmp_path),
                "HERMES_PROFILE": "qa-department",
                "QA_DISCORD_CHANNEL_ID": "1541636723006775477",
                "QA_DISCORD_APPROVER_USER_IDS": "42",
            },
            clear=False,
        ),
        patch.object(gateway_patch, "submit_qa_feedback_decision") as submit,
    ):
        handled = asyncio.run(
            gateway_patch._maybe_handle_qa_feedback_message(Adapter(), message)
        )

    assert handled is True
    submit.assert_not_called()
    assert "1차 승인에는" in message.replies[0]
