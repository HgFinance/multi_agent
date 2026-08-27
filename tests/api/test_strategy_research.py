from __future__ import annotations

import json
from pathlib import Path

import pytest

from apps.api import strategy_research


@pytest.fixture(autouse=True)
def no_real_kanban_side_effects(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        strategy_research,
        "_ensure_tracking_root",
        lambda **_kwargs: (None, "UNAVAILABLE"),
    )


def test_strategy_research_api_enqueues_a_natural_language_goal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strategy_research, "_lab_root", lambda: tmp_path / "research")
    request = strategy_research.StrategyResearchAsk(
        query="코스피 단기 반전 전략을 연구하고 백테스트해줘",
    )

    accepted = strategy_research.strategy_research_ask(request, "user-a")
    status = strategy_research.strategy_research_status(accepted.request_id, "user-a")

    assert accepted.accepted is True
    assert accepted.status == "QUEUED"
    assert accepted.lab_id == accepted.request_id
    assert status.status == "QUEUED"
    assert status.goal == request.query


def test_strategy_research_status_is_scoped_to_the_requesting_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strategy_research, "_lab_root", lambda: tmp_path / "research")
    accepted = strategy_research.strategy_research_ask(
        strategy_research.StrategyResearchAsk(
            query="Find a robust cross-sectional alpha strategy",
            request_id="research-02",
        ),
        "user-a",
    )

    with pytest.raises(strategy_research.HTTPException) as error:
        strategy_research.strategy_research_status(accepted.request_id, "user-b")

    assert error.value.status_code == 403


def test_duplicate_submission_reports_the_current_lab_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(strategy_research, "_lab_root", lambda: tmp_path / "research")
    request = strategy_research.StrategyResearchAsk(query="Find a robust cross-sectional alpha strategy", request_id="research-03")
    first = strategy_research.strategy_research_ask(request, "user-a")
    intake = strategy_research.ResearchIntake(tmp_path / "research")
    intake.materialize(first.request_id, repo_root=tmp_path)

    replay = strategy_research.strategy_research_ask(request, "user-a")

    assert replay.duplicate is True
    assert replay.status == "RESEARCHING"


def test_strategy_tracking_root_is_persisted_when_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(strategy_research, "_lab_root", lambda: tmp_path / "research")
    def fake_tracking_root(**kwargs):
        kwargs["intake"].bind_kanban_root(kwargs["request_id"], "t_strategy_root")
        return "t_strategy_root", "CREATED"

    monkeypatch.setattr(strategy_research, "_ensure_tracking_root", fake_tracking_root)

    accepted = strategy_research.strategy_research_ask(
        strategy_research.StrategyResearchAsk(
            query="코스피 돌파 전략을 연구하고 백테스트해줘",
            request_id="research-root-01",
        ),
        "user-a",
    )

    assert accepted.kanban_root_task_id == "t_strategy_root"
    assert accepted.kanban_tracking_status == "CREATED"
    request = (tmp_path / "research" / "intake" / "research-root-01.json").read_text()
    assert "t_strategy_root" in request


def test_blocked_strategy_can_only_be_escalated_for_human_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(strategy_research, "_lab_root", lambda: tmp_path / "research")
    accepted = strategy_research.strategy_research_ask(
        strategy_research.StrategyResearchAsk(
            query="코스피 돌파 전략을 연구하고 백테스트해줘",
            request_id="research-promotion-01",
        ),
        "user-a",
    )
    intake = strategy_research.ResearchIntake(tmp_path / "research")
    intake.record_error(accepted.request_id, phase="DATA", error="PIT universe unavailable")

    promoted = strategy_research.strategy_research_promote(
        accepted.request_id,
        strategy_research.StrategyPromotionAsk(
            mode="live", confirm=True, override_blocked=True
        ),
        "user-a",
    )

    assert promoted.status == "REVIEW_REQUIRED"
    assert "강제 배포하지 않고" in promoted.message


def test_status_exposes_one_final_report_after_all_experiments(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "research"
    monkeypatch.setattr(strategy_research, "_lab_root", lambda: root)
    accepted = strategy_research.strategy_research_ask(
        strategy_research.StrategyResearchAsk(
            query="코스피 돌파 전략을 연구하고 백테스트해줘",
            request_id="research-final-report-01",
        ),
        "user-a",
    )
    intake = strategy_research.ResearchIntake(root)
    lab_path = intake.materialize(accepted.request_id, repo_root=tmp_path)
    for plan_id in ("plan-1", "plan-2"):
        (lab_path / "plans" / f"{plan_id}.json").write_text(
            json.dumps({"plan_id": plan_id}), encoding="utf-8"
        )
        (lab_path / "results" / f"{plan_id}.json").write_text(
            json.dumps({"plan_id": plan_id, "status": "FAILED", "failure_reason": "검증 실패"}),
            encoding="utf-8",
        )
    (lab_path / "events.jsonl").write_text(
        "\n".join(
            json.dumps(
                {"event_type": "DECISION", "payload": {"plan_id": plan_id, "decision": "PAUSE"}}
            )
            for plan_id in ("plan-1", "plan-2")
        )
        + "\n",
        encoding="utf-8",
    )
    state = json.loads((lab_path / ".state.json").read_text(encoding="utf-8"))
    state.update({"active_plan_id": None, "last_action": "AWAITING_NEW_DATA"})
    (lab_path / ".state.json").write_text(json.dumps(state), encoding="utf-8")

    status = strategy_research.strategy_research_status(accepted.request_id, "user-a")

    assert status.latest_report is not None
    assert "전략 Hermes 백테스트 완료 · 최종 보고서" in status.latest_report
    assert "실험 2건 완료" in status.latest_report


def _completed_strategy_lab(root: Path, monkeypatch: pytest.MonkeyPatch, request_id: str) -> tuple[str, strategy_research.ResearchIntake]:
    monkeypatch.setattr(strategy_research, "_lab_root", lambda: root)
    accepted = strategy_research.strategy_research_ask(
        strategy_research.StrategyResearchAsk(
            query="하이닉스 3분봉 정배열에서 2% 상승 매도 전략을 연구하고 백테스트해줘",
            request_id=request_id,
        ),
        "user-a",
    )
    intake = strategy_research.ResearchIntake(root)
    lab_path = intake.materialize(accepted.request_id, repo_root=root)
    (lab_path / "plans" / "plan-1.json").write_text(
        json.dumps(
            {
                "plan_id": "plan-1",
                "signature": "sma_alignment_v1|3m|sma=5,20,60|target=2pct|next_open_plus_one_bar|t8412|adjusted|cost=10bps/side",
            }
        ),
        encoding="utf-8",
    )
    (lab_path / "results" / "plan-1.json").write_text(
        json.dumps(
            {
                "plan_id": "plan-1",
                "status": "COMPLETED",
                "metrics": {"by_symbol": {"000660": {"trades": 3}}},
            }
        ),
        encoding="utf-8",
    )
    return accepted.request_id, intake


def test_human_deployment_request_is_exactly_scoped_and_not_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id, intake = _completed_strategy_lab(tmp_path / "research", monkeypatch, "research-deploy-01")

    deployment = strategy_research.request_strategy_deployment(
        request_id,
        strategy_research.StrategyDeploymentAsk(
            mode="paper",
            symbols=["000660"],
            confirm=True,
            reason="검증 보고서를 확인했고 하이닉스 PAPER 배포를 요청합니다.",
        ),
        "user-a",
    )

    assert deployment.status == "REVIEW_REQUIRED"
    assert deployment.symbols == ["000660"]
    assert deployment.result_hash
    assert deployment.status != "ACTIVE"
    manifest = json.loads(
        (intake.lab_path(request_id) / "deployments" / f"{deployment.deployment_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["runtime_config"]["orders_enabled"] is False
    assert manifest["runtime_config"]["symbols"] == ["000660"]

    replay = strategy_research.request_strategy_deployment(
        request_id,
        strategy_research.StrategyDeploymentAsk(
            mode="paper",
            symbols=["000660"],
            confirm=True,
            reason="같은 요청 재시도",
        ),
        "user-a",
    )
    assert replay.deployment_id == deployment.deployment_id

    status = strategy_research.strategy_research_status(request_id, "user-a")
    assert status.deployment_count == 1
    assert status.deployments[0]["deployment_id"] == deployment.deployment_id


def _candidate_strategy_lab(
    root: Path, monkeypatch: pytest.MonkeyPatch, request_id: str
) -> tuple[str, strategy_research.ResearchIntake]:
    request_id, intake = _completed_strategy_lab(root, monkeypatch, request_id)
    lab_path = intake.lab_path(request_id)
    state = json.loads((lab_path / ".state.json").read_text(encoding="utf-8"))
    state.update({"status": "CANDIDATE", "candidate_available": True, "last_action": "CANDIDATE_READY"})
    (lab_path / ".state.json").write_text(json.dumps(state), encoding="utf-8")
    (lab_path / "candidate.json").write_text(
        json.dumps({"schema": "autonomous-strategy-candidate.v1", "plan_id": "plan-1"}),
        encoding="utf-8",
    )
    (lab_path / "events.jsonl").write_text(
        json.dumps({"event_type": "DECISION", "payload": {"plan_id": "plan-1", "decision": "CANDIDATE"}})
        + "\n",
        encoding="utf-8",
    )
    return request_id, intake


def test_candidate_deployment_waits_for_explicit_approval_then_starts_paper_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id, intake = _candidate_strategy_lab(
        tmp_path / "research", monkeypatch, "research-deploy-approval-01"
    )
    requested = strategy_research.request_strategy_deployment(
        request_id,
        strategy_research.StrategyDeploymentAsk(
            mode="paper",
            symbols=["000660"],
            confirm=True,
            reason="백테스트 요약을 먼저 확인할 배포 요청",
        ),
        "user-a",
    )

    assert requested.status == "AWAITING_APPROVAL"
    assert requested.approval_required is True
    assert requested.runtime_status == "NOT_STARTED"
    assert requested.execution_status == "NOT_STARTED"
    assert requested.backtest_summary["symbols"] == ["000660"]

    runtime_calls: list[tuple[str, str]] = []

    def fake_runtime(record: dict[str, object], *, bundle_path: Path) -> dict[str, object]:
        runtime_calls.append((str(record["deployment_id"]), bundle_path.name))
        return {
            "runtime_status": "RUNNING",
            "container_name": "strategy-paper-test",
            "container_id": "container-test",
        }

    monkeypatch.setattr(strategy_research, "_start_strategy_paper_container", fake_runtime)
    approved = strategy_research.approve_strategy_deployment(
        request_id,
        requested.deployment_id,
        strategy_research.StrategyDeploymentApprovalAsk(
            confirm=True, reason="백테스트 요약을 확인했고 PAPER 신호 실행을 승인합니다."
        ),
        "user-a",
    )

    assert approved.status == "ACTIVE"
    assert approved.approved_by == "user-a"
    assert approved.bundle_hash and len(approved.bundle_hash) == 64
    assert approved.runtime_status == "RUNNING"
    assert approved.execution_status == "SIGNAL_ONLY"
    assert runtime_calls == [(requested.deployment_id, f"{requested.deployment_id}.json")]
    bundle_path = intake.lab_path(request_id) / "deployments" / "bundles" / f"{requested.deployment_id}.json"
    bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
    assert bundle["strategy"] == {
        "kind": "SMA_ALIGNMENT",
        "timeframe": "3M",
        "fast": 5,
        "mid": 20,
        "slow": 60,
        "entry": "CLOSE_GT_SMA5_GT_SMA20_GT_SMA60",
        "take_profit_pct": "0.02",
        "entry_execution": "NEXT_BAR_OPEN",
        "exit_execution": "NEXT_BAR_OPEN",
    }
    assert bundle["execution"]["orders_enabled"] is False

    replay = strategy_research.approve_strategy_deployment(
        request_id,
        requested.deployment_id,
        strategy_research.StrategyDeploymentApprovalAsk(
            confirm=True, reason="동일 승인 재시도"
        ),
        "user-a",
    )
    assert replay.status == "ACTIVE"
    assert len(runtime_calls) == 1


def _review_required_completed_strategy_lab(
    root: Path, monkeypatch: pytest.MonkeyPatch, request_id: str
) -> tuple[str, strategy_research.ResearchIntake]:
    request_id, intake = _completed_strategy_lab(root, monkeypatch, request_id)
    lab_path = intake.lab_path(request_id)
    state = json.loads((lab_path / ".state.json").read_text(encoding="utf-8"))
    state.update(
        {
            "status": "COMPLETED",
            "candidate_available": False,
            "last_action": "COMPLETED_WITH_REVIEW_REQUIRED_RELEASE",
        }
    )
    (lab_path / ".state.json").write_text(json.dumps(state), encoding="utf-8")
    (lab_path / "events.jsonl").write_text(
        json.dumps(
            {
                "event_type": "DECISION",
                "payload": {"plan_id": "plan-1", "decision": "PIVOT"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return request_id, intake


def test_top_level_human_can_override_review_required_for_signal_only_paper_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id, intake = _review_required_completed_strategy_lab(
        tmp_path / "research", monkeypatch, "research-deploy-override-01"
    )
    requested = strategy_research.request_strategy_deployment(
        request_id,
        strategy_research.StrategyDeploymentAsk(
            mode="paper",
            symbols=["000660"],
            confirm=True,
            reason="PIVOT 결과를 확인하고 최상위 예외 승인을 요청합니다.",
        ),
        "user-a",
    )
    assert requested.status == "REVIEW_REQUIRED"

    monkeypatch.setenv("STRATEGY_TOP_LEVEL_APPROVER_USER_IDS", "user-admin")
    runtime_calls: list[str] = []

    def fake_runtime(record: dict[str, object], *, bundle_path: Path) -> dict[str, object]:
        runtime_calls.append(str(record["deployment_id"]))
        return {
            "runtime_status": "RUNNING",
            "container_name": "strategy-paper-override-test",
            "container_id": "container-override-test",
        }

    monkeypatch.setattr(strategy_research, "_start_strategy_paper_container", fake_runtime)
    approved = strategy_research.approve_strategy_deployment(
        request_id,
        requested.deployment_id,
        strategy_research.StrategyDeploymentApprovalAsk(
            confirm=True,
            override_review_required=True,
            reason="최상위 승인자가 PIVOT 릴리스 게이트 예외를 승인합니다.",
        ),
        "user-admin",
    )

    assert approved.status == "ACTIVE"
    assert approved.override_review_required is True
    assert approved.approved_by == "user-admin"
    assert approved.execution_status == "SIGNAL_ONLY"
    assert runtime_calls == [requested.deployment_id]

    manifest = json.loads(
        (intake.lab_path(request_id) / "deployments" / f"{requested.deployment_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["approval_type"] == "HUMAN_TOP_LEVEL_OVERRIDE"
    assert manifest["override_review_required"] is True
    assert manifest["approval_audit"][0]["approved_by"] == "user-admin"
    assert manifest["approval_audit"][0]["override_review_required"] is True
    bundle = json.loads(
        (intake.lab_path(request_id) / "deployments" / "bundles" / f"{requested.deployment_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert bundle["approval_type"] == "HUMAN_TOP_LEVEL_OVERRIDE"
    assert bundle["execution"]["orders_enabled"] is False
    assert bundle["execution"]["signal_only"] is True


def test_review_required_exception_is_fail_closed_for_non_top_level_human(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id, intake = _review_required_completed_strategy_lab(
        tmp_path / "research", monkeypatch, "research-deploy-override-02"
    )
    requested = strategy_research.request_strategy_deployment(
        request_id,
        strategy_research.StrategyDeploymentAsk(
            mode="paper", symbols=["000660"], confirm=True, reason="예외 승인 테스트"
        ),
        "user-a",
    )
    monkeypatch.setenv("STRATEGY_TOP_LEVEL_APPROVER_USER_IDS", "different-user")
    runtime_calls: list[str] = []
    monkeypatch.setattr(
        strategy_research,
        "_start_strategy_paper_container",
        lambda record, *, bundle_path: runtime_calls.append(str(record["deployment_id"])) or {},
    )

    with pytest.raises(strategy_research.HTTPException) as error:
        strategy_research.approve_strategy_deployment(
            request_id,
            requested.deployment_id,
            strategy_research.StrategyDeploymentApprovalAsk(
                confirm=True,
                override_review_required=True,
                reason="최상위 승인자 아닌 사용자의 예외 승인 시도",
            ),
            "user-a",
        )

    assert error.value.status_code == 403
    assert error.value.detail == "strategy_deployment_top_level_approver_required"
    assert runtime_calls == []
    record = json.loads(
        (intake.lab_path(request_id) / "deployments" / f"{requested.deployment_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert record["status"] == "REVIEW_REQUIRED"
    assert record.get("approval_audit") in (None, [])


def test_deployment_command_never_counts_as_approval_and_approval_text_is_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id, _intake = _candidate_strategy_lab(
        tmp_path / "research", monkeypatch, "research-deploy-approval-02"
    )
    requested = strategy_research.request_strategy_deployment_from_text(
        query="하이닉스 전략 배포해줘",
        actor_id="user-a",
        source="discord",
    )

    assert requested.request_id == request_id
    assert requested.status == "AWAITING_APPROVAL"
    assert strategy_research.looks_like_strategy_deployment_approval(
        f"전략 배포 승인해줘 {requested.deployment_id}"
    )
    assert not strategy_research.looks_like_strategy_deployment_approval("전략 배포해줘")


def test_natural_language_top_level_exception_approval_resolves_review_required_deployment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id, _intake = _review_required_completed_strategy_lab(
        tmp_path / "research", monkeypatch, "research-deploy-override-03"
    )
    requested = strategy_research.request_strategy_deployment(
        request_id,
        strategy_research.StrategyDeploymentAsk(
            mode="paper", symbols=["000660"], confirm=True, reason="자연어 예외 승인 테스트"
        ),
        "user-a",
    )
    assert strategy_research.looks_like_strategy_deployment_override(
        f"하이닉스 전략 배포 예외 승인해줘 {requested.deployment_id}"
    )
    assert not strategy_research.looks_like_strategy_deployment_override(
        f"전략 배포 승인해줘 {requested.deployment_id}"
    )
    monkeypatch.setenv("STRATEGY_TOP_LEVEL_APPROVER_USER_IDS", "user-a")
    monkeypatch.setattr(
        strategy_research,
        "_start_strategy_paper_container",
        lambda _record, *, bundle_path: {
            "runtime_status": "RUNNING",
            "container_name": "strategy-paper-natural-language-test",
        },
    )

    approved = strategy_research.approve_strategy_deployment_from_text(
        query=f"하이닉스 전략 배포 예외 승인해줘 {requested.deployment_id}",
        actor_id="user-a",
    )

    assert approved.status == "ACTIVE"
    assert approved.override_review_required is True



def test_approved_paper_deployment_can_stop_start_and_be_retired_without_erasing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id, intake = _candidate_strategy_lab(
        tmp_path / "research", monkeypatch, "research-deploy-lifecycle-01"
    )
    requested = strategy_research.request_strategy_deployment(
        request_id,
        strategy_research.StrategyDeploymentAsk(
            mode="paper", symbols=["000660"], confirm=True, reason="수명주기 테스트"
        ),
        "user-a",
    )
    monkeypatch.setattr(
        strategy_research,
        "_start_strategy_paper_container",
        lambda _record, *, bundle_path: {"runtime_status": "RUNNING", "container_name": "strategy-paper-test"},
    )
    active = strategy_research.approve_strategy_deployment(
        request_id,
        requested.deployment_id,
        strategy_research.StrategyDeploymentApprovalAsk(
            confirm=True, reason="PAPER 승인"
        ),
        "user-a",
    )
    assert active.status == "ACTIVE"
    monkeypatch.setattr(
        strategy_research,
        "_strategy_runtime_snapshot",
        lambda *, deployment_id: {
            "deployment_id": deployment_id,
            "container_name": "strategy-paper-test",
            "container": {"found": True, "running": True},
            "execution_status": "SIGNAL_ONLY",
        },
    )
    live_status = strategy_research.strategy_research_deployment_status(
        request_id, requested.deployment_id, "user-a"
    )
    assert live_status.runtime_status == "RUNNING"
    assert live_status.runtime_detail["container"]["running"] is True

    runtime_calls: list[tuple[str, dict[str, object]]] = []

    def fake_runtime_command(*, path: str, payload: dict[str, object], timeout_seconds: float = 20.0) -> dict[str, object]:
        del timeout_seconds
        runtime_calls.append((path, payload))
        if path.endswith("/power"):
            return {"runtime_status": "STOPPED" if payload["action"] == "stop" else "RUNNING", "container_name": "strategy-paper-test"}
        return {"runtime_status": "REMOVED"}

    monkeypatch.setattr(strategy_research, "_strategy_runtime_command", fake_runtime_command)
    paused = strategy_research.power_strategy_deployment(
        request_id,
        requested.deployment_id,
        strategy_research.StrategyDeploymentPowerAsk(action="stop", reason="컨테이너 중지 테스트"),
        "user-a",
    )
    assert paused.status == "PAUSED"
    restarted = strategy_research.power_strategy_deployment(
        request_id,
        requested.deployment_id,
        strategy_research.StrategyDeploymentPowerAsk(action="start", reason="컨테이너 시작 테스트"),
        "user-a",
    )
    assert restarted.status == "ACTIVE"
    removed = strategy_research.remove_strategy_deployment(
        request_id,
        requested.deployment_id,
        strategy_research.StrategyDeploymentRemoveAsk(confirm=True, reason="전략 제거 테스트"),
        "user-a",
    )
    assert removed.status == "REMOVED"
    assert removed.execution_status == "DISABLED"
    assert (intake.lab_path(request_id) / "results" / "plan-1.json").exists()
    assert (intake.lab_path(request_id) / "deployments" / "bundles" / f"{requested.deployment_id}.json").exists()
    assert [call[0].rsplit("/", 1)[-1] for call in runtime_calls] == ["power", "power", "remove"]


def test_human_deployment_rejects_live_and_unverified_universe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id, _intake = _completed_strategy_lab(tmp_path / "research", monkeypatch, "research-deploy-02")

    live = strategy_research.request_strategy_deployment(
        request_id,
        strategy_research.StrategyDeploymentAsk(
            mode="live",
            symbols=["000660"],
            confirm=True,
            reason="실수 방지 경계 테스트",
        ),
        "user-a",
    )
    assert live.status == "BLOCKED"

    unknown = strategy_research.request_strategy_deployment(
        request_id,
        strategy_research.StrategyDeploymentAsk(
            mode="paper",
            symbols=["005930"],
            confirm=True,
            reason="테스트 유니버스 밖 종목",
        ),
        "user-a",
    )
    assert unknown.status == "BLOCKED"


def test_natural_language_human_deployment_resolves_one_completed_lab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _request_id, _intake = _completed_strategy_lab(tmp_path / "research", monkeypatch, "research-deploy-03")

    assert strategy_research.looks_like_strategy_deployment("하이닉스 전략 배포해줘")
    deployment = strategy_research.request_strategy_deployment_from_text(
        query="하이닉스 전략 배포해줘",
        actor_id="user-a",
        source="web",
    )

    assert deployment.mode == "paper"
    assert deployment.symbols == ["000660"]
    assert deployment.status == "REVIEW_REQUIRED"


def test_top_level_override_can_retry_a_failed_runtime_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    request_id, intake = _review_required_completed_strategy_lab(
        tmp_path / "research", monkeypatch, "research-deploy-runtime-retry"
    )
    requested = strategy_research.request_strategy_deployment(
        request_id,
        strategy_research.StrategyDeploymentAsk(
            mode="paper", symbols=["000660"], confirm=True, reason="런타임 재시도 경계 테스트"
        ),
        "user-a",
    )
    monkeypatch.setenv("STRATEGY_TOP_LEVEL_APPROVER_USER_IDS", "user-admin")
    calls = 0

    def flaky_runtime(record: dict[str, object], *, bundle_path: Path) -> dict[str, object]:
        nonlocal calls
        del record, bundle_path
        calls += 1
        if calls == 1:
            raise strategy_research.HTTPException(status_code=503, detail="temporary_runtime_failure")
        return {
            "runtime_status": "RUNNING",
            "container_name": "strategy-paper-retry-test",
            "container_id": "container-retry-test",
        }

    monkeypatch.setattr(strategy_research, "_start_strategy_paper_container", flaky_runtime)
    first = strategy_research.approve_strategy_deployment(
        request_id,
        requested.deployment_id,
        strategy_research.StrategyDeploymentApprovalAsk(
            confirm=True,
            override_review_required=True,
            reason="최상위 승인으로 PAPER 배포를 시도합니다.",
        ),
        "user-admin",
    )
    assert first.status == "FAILED"
    assert first.runtime_status == "START_FAILED"

    retry = strategy_research.approve_strategy_deployment(
        request_id,
        requested.deployment_id,
        strategy_research.StrategyDeploymentApprovalAsk(
            confirm=True,
            override_review_required=True,
            reason="런타임 오류를 해소하고 최상위 승인 PAPER 배포를 재시도합니다.",
        ),
        "user-admin",
    )
    assert retry.status == "ACTIVE"
    assert retry.override_review_required is True
    assert calls == 2
    manifest = json.loads(
        (intake.lab_path(request_id) / "deployments" / f"{requested.deployment_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert len(manifest["approval_audit"]) == 2
