#!/usr/bin/env python3
"""HITL 1·2단계 — Mandate 변경 Human-in-the-Loop 오케스트레이션.

소유: 영주 (CEO Office)
근거: docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md 5.1절
      ("사용자 요청 -> CEO Hermes 구조화 -> Draft Version -> Risk 검토 -> QA 검토 ->
       사용자 승인 -> Active -> Version Event 발행"),
      docs/02-engineering/GOVERNANCE_WORKFORCE_DOMAIN_API_SPEC.md 2.4절
      ("Mandate 변경 Workflow(LangGraph)는 두 노드 사이를 VersionResult로 넘긴다")

## 왜 LangGraph interrupt()를 안 쓰는가

설치된 checkpointer는 `langgraph.checkpoint.memory.InMemorySaver` 하나뿐이다
(`langgraph-checkpoint-postgres` 미설치, 2026-08-04 실측). 메모리 checkpoint는 프로세스가
재시작하면 사라진다 - 사람이 몇 시간 뒤에 승인하는 흐름에서 이건 "승인 요청이 증발"로
직결된다.

그래서 이 모듈은 **오래 켜져 있는 그래프 실행 하나로 승인 대기를 표현하지 않는다.** 대신:
  - `governance.approvals`(PENDING/APPROVED/REJECTED/EXPIRED/REVOKED)를 승인 대기의
    **유일한 진실**로 삼는다. checkpoint는 이 모듈에 아예 없다.
  - `submit()`은 Draft Version을 만들고 Risk·QA 승인을 **동시에** 요청한 뒤 **정상 종료**한다.
    "그래프가 멈췄다"가 아니라 "이번 실행이 할 일을 다 하고 끝났다"이다.
  - `advance()`는 승인이 결정될 때마다(Risk/QA 담당자가 API로 decide 호출) **별도로,
    다시** 호출돼 다음 단계로 넘긴다 - 재개(resume)가 아니라 매번 새로 판단하는 짧은 호출이다.
  - 컨테이너가 재시작해도 PENDING 승인은 DB에 그대로 있다 - `advance()`를 다시 부르면
    그 자리에서 이어진다(UC-7, 2026-08-04 논의).

`interrupt()` + Postgres checkpointer 도입은 이후 단계(4단계)로 미룬다 - 지금 없어도 위
설계로 모든 UC(승인/거절/역제안/만료/Risk 거부/재시작)가 동작한다(CLAUDE.md 개발원칙 8:
새 라이브러리는 기존 스택으로 못 푸는 문제와 함께 도입한다 - 지금은 그 문제가 없다).

## Case 상태와 세부 단계를 섞지 않는다

`governance.cases`는 굵은 Projection이다(OPEN/ACKNOWLEDGED/RESOLVED/CANCELLED,
GOV-02 case_root.py). "Risk 검토 중"/"QA 검토 중"/"사용자 승인 대기 중" 같은 세부 단계를
매번 case_events로 강제로 쑤셔넣지 않는다 - `transition()`은 실제 상태가 바뀔 때만 부른다
(같은 status로의 재전이는 case_root.py의 상태 머신 자체가 거부한다). 세부 단계는
`governance.approvals` 3개 행(RISK/QA/USER)의 decision을 그때그때 조회해서 판단한다 -
그 자체로 이미 감사 가능한 기록이라 중복 로그가 필요 없다.

## Risk/QA 검토를 어디에 태우는가

새 `risk-api`/`audit-api` 엔드포인트를 만들지 않는다. 대신 GOV-02가 이미 만든
`governance.approvals`(required_role=RISK/QA)를 그대로 쓴다 - Risk/QA 소관 담당자가
같은 `POST /governance/v1/approvals/{id}/decide`로 결정하고, `approval.py`의
`_ROLE_DECIDERS`가 이미 "CEO는 RISK/QA 승인을 결정할 수 없다"를 강제한다(불변식 2).
Risk/QA가 "무엇을 검사하는지"의 도메인 로직(강제 가능성 계산, 모순 탐지)은 그 부서
소관이라 여기서 지어내지 않는다 - 여기는 승인 요청을 만들고 결과를 읽을 뿐이다.

## 왜 TIGHTEN/NEUTRAL은 이 파이프라인을 건너뛰는가

`lifecycle.py`의 `MandateActivationService.activate()`는 TIGHTEN/NEUTRAL을 이미
승인 없이 즉시 적용한다(검증된 기존 동작, 2026-08-04 Mandate 쓰기 경로 검증에서 확인).
이 모듈은 그 경로를 그대로 재사용한다(`needs_review`가 False면 Case도 승인도 안 만들고
바로 activate) - 이미 테스트된 "즉시 적용"을 새 Case/승인 오버헤드로 느리게 만들지 않는다.

불변식:
  1. **승인 대기의 진실은 DB다, 그래프 메모리가 아니다.** (위 설명 참고)
  2. **`advance()`는 매 호출마다 PENDING 승인 3개(RISK/QA/USER)를 다시 읽는다.**
     캐시하지 않는다 - 그래야 재시작·재시도가 안전하다.
  3. **Risk/QA 중 하나라도 REJECTED/EXPIRED/REVOKED면 사용자 승인 단계로 가지 않는다.**
     둘 다 APPROVED여야만 USER 승인 요청을 만든다(CLAUDE.md "Risk/QA의 거부를 CEO가
     우회·해제할 수 없다").
  4. **만료는 `advance()` 호출 시점에 지연 평가한다.** 별도 스케줄러가 없어서다
     (2026-08-04 실측: 이 저장소에 주기 실행 인프라가 없다) - `advance()`가 불릴 때마다
     `approval.expire()`로 PENDING+기한초과 승인을 EXPIRED로 먼저 정리한 뒤 판단한다.
  5. **종료된 Case는 다시 advance하지 않는다.** RESOLVED/CANCELLED Case에 advance()를
     부르면 `CaseAlreadyResolvedError`다 - 조용히 무시하지 않는다.

자체 점검: python departments/00-ceo-office/src/mandate/change_workflow.py
"""
from __future__ import annotations

import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

_THIS_DIR = Path(__file__).resolve().parent
_SRC_DIR = _THIS_DIR.parent
for _p in (_SRC_DIR / "approval", _SRC_DIR / "case"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from approval import (  # noqa: E402
    ApprovalDecision,
    ApprovalRepository,
    ObjectType,
    RequiredRole,
)
from approval import (  # noqa: E402
    expire as expire_approval,
)
from approval import (  # noqa: E402
    request_approval as build_approval_request,
)
from case_root import (  # noqa: E402
    CaseRepository,
    CaseStatus,
    build_display_id,
)
from case_root import (  # noqa: E402
    open_case,
)
from case_root import (  # noqa: E402
    transition as transition_case,
)
from lifecycle import MandateActivationService, UserApproval  # noqa: E402
from policy import MandatePolicy  # noqa: E402
from service import (  # noqa: E402
    ChangeDirection,
    MandateVersionRepository,
    MandateVersionService,
    requires_user_reapproval,
)


class ChangeStage(str, Enum):
    FAST_APPLIED = "FAST_APPLIED"                  # TIGHTEN/NEUTRAL, Case 없이 즉시 적용
    AWAITING_REVIEW = "AWAITING_REVIEW"             # Risk/QA 결정 대기
    REVIEW_REJECTED = "REVIEW_REJECTED"             # Risk 또는 QA가 거절 - Case 종료
    AWAITING_USER_APPROVAL = "AWAITING_USER_APPROVAL"  # Risk+QA 통과, 사용자 결정 대기
    USER_REJECTED = "USER_REJECTED"                 # 사용자 거절 - Case 종료, 이전 Version 유지
    ACTIVATED = "ACTIVATED"                         # 사용자 승인 -> 활성화 완료


class CaseAlreadyResolvedError(Exception):
    """RESOLVED/CANCELLED Case에 advance()를 다시 불렀다 (불변식 5)."""


class NotAMandateChangeCaseError(Exception):
    """case_type이 MANDATE_CHANGE가 아닌 Case에 advance()를 불렀다."""


class ReviewApprovalMissingError(Exception):
    """submit()이 만들었어야 할 RISK/QA 승인 행을 찾지 못했다 - 데이터 정합성 문제."""


@dataclass(frozen=True)
class ChangeRequestResult:
    stage: ChangeStage
    mandate_id: str
    version: int
    direction: ChangeDirection
    case_id: str | None
    detail: str


class MandateChangeWorkflow:
    """§5.1 7단계를 오케스트레이션한다. 판정 로직은 갖지 않는다 - 전부 기존 서비스에 위임."""

    def __init__(
        self,
        *,
        version_repo: MandateVersionRepository,
        version_service: MandateVersionService,
        activation_service: MandateActivationService,
        approval_repo: ApprovalRepository,
        case_repo: CaseRepository,
    ) -> None:
        self._version_repo = version_repo
        self._version_service = version_service
        self._activation_service = activation_service
        self._approval_repo = approval_repo
        self._case_repo = case_repo

    # --- 1. 제출 ------------------------------------------------------------

    def submit(
        self,
        *,
        mandate_id: str,
        fund_id: str,
        policy: MandatePolicy,
        objective_text: str,
        objective: dict,
        effective_from: datetime,
        created_by: str,
        trace_id: str,
        now: datetime,
        previous_policy: MandatePolicy | None = None,
        priority: int = 50,
        review_expires_at: datetime | None = None,
        user_approval_ttl_seconds: int = 24 * 60 * 60,
        version_created_by: str | None = None,
    ) -> ChangeRequestResult:
        """`created_by`(Case 감사 표지, 자유 텍스트)와 `version_created_by`
        (mandate_versions.created_by, governance.user_profiles를 가리키는 uuid FK,
        nullable)는 컬럼 타입이 달라 같은 인자를 공유할 수 없다(governance_workforce.sql:
        cases.created_by는 `text not null`, mandate_versions.created_by는
        `uuid references governance.user_profiles(user_id)`다). 호출자가 실제 사용자
        uuid를 모르면 version_created_by를 생략한다 - nullable이라 None이 안전하다."""
        if user_approval_ttl_seconds < 1:
            raise ValueError("user_approval_ttl_seconds는 1 이상이어야 한다")

        version_result = self._version_service.propose_version(
            mandate_id=mandate_id, policy=policy, objective_text=objective_text,
            objective=objective, effective_from=effective_from,
            previous_policy=previous_policy, created_by=version_created_by,
        )
        current_version, _status = self._version_repo.get_mandate_current(mandate_id)
        is_initial = current_version == 0
        needs_review = is_initial or requires_user_reapproval(version_result.direction)

        if not needs_review:
            # TIGHTEN/NEUTRAL - 기존 activate()의 즉시 적용 경로를 그대로 쓴다(설계 근거는
            # 모듈 docstring "왜 TIGHTEN/NEUTRAL은 건너뛰는가" 참고). Case/승인 없음.
            self._activation_service.activate(
                mandate_id=mandate_id, version=version_result.row.version,
                direction=version_result.direction, at=now,
            )
            return ChangeRequestResult(
                stage=ChangeStage.FAST_APPLIED, mandate_id=mandate_id,
                version=version_result.row.version, direction=version_result.direction,
                case_id=None, detail="TIGHTEN/NEUTRAL - 승인 없이 즉시 적용",
            )

        version_id = self._version_repo.get_mandate_version_id(
            mandate_id, version_result.row.version
        )
        if version_id is None:
            raise ReviewApprovalMissingError(
                f"방금 만든 Version을 찾지 못했다 (mandate_id={mandate_id}, "
                f"version={version_result.row.version})"
            )

        display_id = build_display_id(
            "MANDATE_CHANGE", created_at=now,
            sequence=self._case_repo.next_display_sequence("MANDATE_CHANGE", now),
        )
        case, open_event = open_case(
            case_id=str(uuid.uuid4()), fund_id=fund_id, display_id=display_id,
            case_type="MANDATE_CHANGE", priority=priority, owner_department="ceo-agent",
            trace_id=trace_id, created_by=created_by, created_at=now,
            idempotency_key=str(uuid.uuid4()), reason=objective_text,
            payload={
                "mandate_id": mandate_id, "version": version_result.row.version,
                "mandate_version_id": version_id, "direction": version_result.direction.value,
                "is_initial": is_initial,
                # Risk/QA 완료 시점부터 사용자에게 주는 승인 기한이다. absolute timestamp를
                # submit 시점에 고정하면 검토가 길어질수록 사용자 승인 시간이 사라진다.
                "user_approval_ttl_seconds": user_approval_ttl_seconds,
            },
        )
        self._case_repo.save_new(case, open_event)

        for role in (RequiredRole.RISK, RequiredRole.QA):
            approval = build_approval_request(
                approval_id=str(uuid.uuid4()), fund_id=fund_id,
                object_type=ObjectType.MANDATE_VERSION, object_id=version_id,
                required_role=role, created_at=now,
                reason=f"Mandate 변경 {role.value} 검토 - {objective_text}",
                expires_at=review_expires_at,
            )
            self._approval_repo.save(approval)

        acknowledged, ack_event = transition_case(
            case, to_status=CaseStatus.ACKNOWLEDGED, actor=created_by, at=now,
            next_sequence=self._case_repo.next_sequence(case.case_id),
            idempotency_key=str(uuid.uuid4()), reason="Risk/QA 검토 요청",
        )
        self._case_repo.apply_transition(acknowledged, ack_event)

        return ChangeRequestResult(
            stage=ChangeStage.AWAITING_REVIEW, mandate_id=mandate_id,
            version=version_result.row.version, direction=version_result.direction,
            case_id=case.case_id, detail="Risk/QA 검토 대기 (동시 요청)",
        )

    # --- 2. 재개 --------------------------------------------------------------

    def advance(self, case_id: str, *, at: datetime) -> ChangeRequestResult:
        """다음 단계로 판단해 넘긴다. 상태 변화가 없으면 조회만 하고 아무것도 안 쓴다."""
        case = self._case_repo.get(case_id)
        if case is None:
            raise NotAMandateChangeCaseError(f"case_id={case_id} 없음")
        if case.case_type != "MANDATE_CHANGE":
            raise NotAMandateChangeCaseError(
                f"case_id={case_id}는 MANDATE_CHANGE가 아니라 {case.case_type}다"
            )
        if case.is_terminal:
            raise CaseAlreadyResolvedError(
                f"case_id={case_id}는 이미 {case.status.value}다 - 다시 advance할 수 없다"
            )

        timeline = self._case_repo.timeline(case_id)
        opened_payload = timeline[0].payload or {}
        mandate_id = opened_payload["mandate_id"]
        version = opened_payload["version"]
        version_id = opened_payload["mandate_version_id"]
        direction = ChangeDirection(opened_payload["direction"])
        user_approval_ttl_seconds = opened_payload.get("user_approval_ttl_seconds", 24 * 60 * 60)

        user_approval = self._find_current(ObjectType.MANDATE_VERSION, version_id, RequiredRole.USER, at)
        if user_approval is not None:
            return self._advance_user_stage(
                case=case, user_approval=user_approval, mandate_id=mandate_id,
                version=version, direction=direction, at=at,
            )

        risk = self._find_current(ObjectType.MANDATE_VERSION, version_id, RequiredRole.RISK, at)
        qa = self._find_current(ObjectType.MANDATE_VERSION, version_id, RequiredRole.QA, at)
        if risk is None or qa is None:
            raise ReviewApprovalMissingError(
                f"case_id={case_id}에 RISK/QA 승인 행이 없다 - submit()이 만들었어야 한다"
            )

        _terminal = (ApprovalDecision.REJECTED, ApprovalDecision.EXPIRED, ApprovalDecision.REVOKED)
        if risk.decision in _terminal or qa.decision in _terminal:
            resolved, event = transition_case(
                case, to_status=CaseStatus.RESOLVED, actor="governance-api", at=at,
                next_sequence=self._case_repo.next_sequence(case_id),
                idempotency_key=str(uuid.uuid4()),
                reason="Risk 또는 QA 검토 거절/만료 - 사용자 승인 단계로 가지 않음",
                payload={"outcome": "review_rejected", "risk_decision": risk.decision.value,
                        "qa_decision": qa.decision.value},
            )
            self._case_repo.apply_transition(resolved, event)
            return ChangeRequestResult(
                stage=ChangeStage.REVIEW_REJECTED, mandate_id=mandate_id, version=version,
                direction=direction, case_id=case_id,
                detail=f"Risk={risk.decision.value}, QA={qa.decision.value}",
            )

        if risk.decision is ApprovalDecision.APPROVED and qa.decision is ApprovalDecision.APPROVED:
            user_request = build_approval_request(
                approval_id=str(uuid.uuid4()), fund_id=case.fund_id,
                object_type=ObjectType.MANDATE_VERSION, object_id=version_id,
                required_role=RequiredRole.USER, created_at=at,
                reason="Mandate 변경 사용자 승인 - Risk/QA 검토 통과",
                expires_at=at + timedelta(seconds=user_approval_ttl_seconds),
            )
            self._approval_repo.save(user_request)
            return ChangeRequestResult(
                stage=ChangeStage.AWAITING_USER_APPROVAL, mandate_id=mandate_id, version=version,
                direction=direction, case_id=case_id, detail="Risk/QA 통과 - 사용자 승인 대기",
            )

        return ChangeRequestResult(
            stage=ChangeStage.AWAITING_REVIEW, mandate_id=mandate_id, version=version,
            direction=direction, case_id=case_id,
            detail=f"Risk={risk.decision.value}, QA={qa.decision.value}",
        )

    def _advance_user_stage(
        self, *, case, user_approval, mandate_id: str, version: int,
        direction: ChangeDirection, at: datetime,
    ) -> ChangeRequestResult:
        if user_approval.decision is ApprovalDecision.APPROVED:
            self._activation_service.activate(
                mandate_id=mandate_id, version=version, direction=direction, at=at,
                approval=UserApproval(
                    approved_by=user_approval.actor_user_id or "",
                    trace_id=case.trace_id, reason=user_approval.reason or "사용자 승인",
                ),
            )
            resolved, event = transition_case(
                case, to_status=CaseStatus.RESOLVED, actor="governance-api", at=at,
                next_sequence=self._case_repo.next_sequence(case.case_id),
                idempotency_key=str(uuid.uuid4()), reason="사용자 승인 - 활성화 완료",
                payload={"outcome": "activated", "approved_by": user_approval.actor_user_id},
            )
            self._case_repo.apply_transition(resolved, event)
            return ChangeRequestResult(
                stage=ChangeStage.ACTIVATED, mandate_id=mandate_id, version=version,
                direction=direction, case_id=case.case_id, detail="사용자 승인 -> 활성화 완료",
            )

        _terminal = (ApprovalDecision.REJECTED, ApprovalDecision.EXPIRED, ApprovalDecision.REVOKED)
        if user_approval.decision in _terminal:
            resolved, event = transition_case(
                case, to_status=CaseStatus.RESOLVED, actor="governance-api", at=at,
                next_sequence=self._case_repo.next_sequence(case.case_id),
                idempotency_key=str(uuid.uuid4()),
                reason=f"사용자 {user_approval.decision.value} - 이전 Version 유지",
                payload={"outcome": "user_rejected", "user_decision": user_approval.decision.value},
            )
            self._case_repo.apply_transition(resolved, event)
            return ChangeRequestResult(
                stage=ChangeStage.USER_REJECTED, mandate_id=mandate_id, version=version,
                direction=direction, case_id=case.case_id,
                detail=f"사용자 {user_approval.decision.value}",
            )

        return ChangeRequestResult(
            stage=ChangeStage.AWAITING_USER_APPROVAL, mandate_id=mandate_id, version=version,
            direction=direction, case_id=case.case_id, detail="사용자 결정 대기",
        )

    def _find_current(self, object_type, object_id: str, role: RequiredRole, at: datetime):
        """조회 + 지연 만료 평가 (불변식 4). PENDING+기한초과면 EXPIRED로 저장하고 그 값을 돌려준다."""
        approval = self._approval_repo.find(object_type, object_id, role)
        if approval is None:
            return None
        expired = expire_approval(approval, at)
        if expired is not None:
            self._approval_repo.save(expired)
            return expired
        return approval


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/00-ceo-office/src/mandate/change_workflow.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta, timezone
    from decimal import Decimal

    from approval import InMemoryApprovalRepository
    from approval import decide as decide_approval
    from case_root import InMemoryCaseRepository
    from lifecycle import MandateActivationService
    from policy import ApprovalRules, PaperOrderMode, RiskBounds, UniversePolicy
    from service import InMemoryMandateVersionRepository, MandateVersionService

    def _policy(max_instrument_weight: str) -> MandatePolicy:
        # RiskBounds는 instrument <= sector <= gross_exposure를 강제한다(policy.py 상호
        # 모순 검증) - sector/gross를 instrument보다 항상 넉넉하게 잡아 이 테스트가 검증하려는
        # 것(TIGHTEN/LOOSEN 판정, Risk/QA/사용자 승인 흐름)과 무관한 값에서 안 걸리게 한다.
        w = Decimal(max_instrument_weight)
        sector = max(Decimal("0.3"), w + Decimal("0.1"))
        gross = max(Decimal("1.0"), sector + Decimal("0.1"))
        return MandatePolicy(
            allowed_assets=["A005930"], forbidden_assets=[],
            risk_bounds=RiskBounds(
                base_capital="100000000", currency="KRW",
                max_instrument_weight=max_instrument_weight, max_sector_weight=str(sector),
                max_gross_exposure=str(gross), max_concurrent_positions=10, max_daily_loss="0.03",
            ),
            universe_policy=UniversePolicy(
                allowed_markets=["KRX"], trading_start="09:00", trading_end="15:30",
            ),
            approval_rules=ApprovalRules(paper_order_mode=PaperOrderMode.USER_APPROVAL),
        )

    def _workflow() -> tuple[MandateChangeWorkflow, InMemoryApprovalRepository, InMemoryCaseRepository]:
        version_repo = InMemoryMandateVersionRepository()
        version_repo.set_fund_base_currency("m1", "KRW")
        approval_repo = InMemoryApprovalRepository()
        case_repo = InMemoryCaseRepository()
        wf = MandateChangeWorkflow(
            version_repo=version_repo, version_service=MandateVersionService(version_repo),
            activation_service=MandateActivationService(version_repo),
            approval_repo=approval_repo, case_repo=case_repo,
        )
        return wf, approval_repo, case_repo

    # mandate_versions DDL이 effective_to > effective_from을 강제한다(check 제약) - 활성화가
    # 이전 Version을 그 시각에 종료시키므로, 모든 단계에 같은 시각을 재사용하면 안 된다.
    # 매 호출마다 증가하는 시계를 쓴다(postgres_repository.py 자체 점검에서 겪은 것과 같은 함정).
    _clock = [datetime(2026, 8, 4, tzinfo=timezone.utc)]

    def _tick() -> datetime:
        _clock[0] += timedelta(hours=1)
        return _clock[0]

    # 1) UC-2 TIGHTEN(최초가 아니고 완화) - Case/승인 없이 즉시 적용. 최초 활성화 먼저 필요.
    wf, approvals, cases = _workflow()
    r0 = wf.submit(mandate_id="m1", fund_id="f1", policy=_policy("0.2"),
                   objective_text="최초", objective={}, effective_from=_tick(), created_by="u",
                   trace_id="t0", now=_clock[0])
    assert r0.stage is ChangeStage.AWAITING_REVIEW, r0  # 최초 활성화는 항상 검토 필요
    # Risk/QA 승인 -> 사용자 승인 -> 활성화까지 진행시켜 "최초 활성화 완료" 상태를 만든다.
    v1_id = wf._version_repo.get_mandate_version_id("m1", 1)
    for role, dept in ((RequiredRole.RISK, "risk-management"), (RequiredRole.QA, "qa-department")):
        a = approvals.find(ObjectType.MANDATE_VERSION, v1_id, role)
        approvals.save(decide_approval(a, decision=ApprovalDecision.APPROVED,
                                       actor_department=dept, at=_tick()))
    r1 = wf.advance(r0.case_id, at=_tick())
    assert r1.stage is ChangeStage.AWAITING_USER_APPROVAL, r1
    user_a = approvals.find(ObjectType.MANDATE_VERSION, v1_id, RequiredRole.USER)
    approvals.save(decide_approval(user_a, decision=ApprovalDecision.APPROVED, at=_tick(),
                                   actor_user_id="user-1", reason="최초 승인"))
    r2 = wf.advance(r0.case_id, at=_tick())
    assert r2.stage is ChangeStage.ACTIVATED, r2
    assert cases.get(r0.case_id).status is CaseStatus.RESOLVED

    # 2) UC-2 TIGHTEN - 이제 활성 v1이 있으니 v2(더 좁은 한도)는 즉시 적용.
    r3 = wf.submit(mandate_id="m1", fund_id="f1", policy=_policy("0.1"),
                   objective_text="한도 축소", objective={}, effective_from=_tick(), created_by="u",
                   trace_id="t1", now=_clock[0], previous_policy=_policy("0.2"))
    assert r3.stage is ChangeStage.FAST_APPLIED and r3.case_id is None, r3
    assert wf._version_repo.get_mandate_current("m1") == (2, "ACTIVE")
    print("ok - UC-2(TIGHTEN 즉시 적용) + 최초 활성화 경로 통과")

    # 3) UC-1 LOOSEN - Risk/QA 동시 요청, 둘 다 승인 -> 사용자 승인 -> 활성화.
    r4 = wf.submit(mandate_id="m1", fund_id="f1", policy=_policy("0.3"),
                   objective_text="한도 확대", objective={}, effective_from=_tick(), created_by="u",
                   trace_id="t2", now=_clock[0], previous_policy=_policy("0.1"))
    assert r4.stage is ChangeStage.AWAITING_REVIEW and r4.direction is ChangeDirection.LOOSEN, r4
    v3_id = wf._version_repo.get_mandate_version_id("m1", 3)
    risk_a = approvals.find(ObjectType.MANDATE_VERSION, v3_id, RequiredRole.RISK)
    qa_a = approvals.find(ObjectType.MANDATE_VERSION, v3_id, RequiredRole.QA)
    assert risk_a is not None and qa_a is not None and risk_a.approval_id != qa_a.approval_id
    approvals.save(decide_approval(risk_a, decision=ApprovalDecision.APPROVED,
                                   actor_department="risk-management", at=_tick()))
    approvals.save(decide_approval(qa_a, decision=ApprovalDecision.APPROVED,
                                   actor_department="qa-department", at=_clock[0]))
    r5 = wf.advance(r4.case_id, at=_tick())
    assert r5.stage is ChangeStage.AWAITING_USER_APPROVAL, r5
    user_a2 = approvals.find(ObjectType.MANDATE_VERSION, v3_id, RequiredRole.USER)
    approvals.save(decide_approval(user_a2, decision=ApprovalDecision.APPROVED, at=_tick(),
                                   actor_user_id="user-1"))
    r6 = wf.advance(r4.case_id, at=_tick())
    assert r6.stage is ChangeStage.ACTIVATED and wf._version_repo.get_mandate_current("m1") == (3, "ACTIVE")
    print("ok - UC-1(LOOSEN 전체 경로: Risk+QA 병렬 승인 -> 사용자 승인 -> 활성화) 통과")

    # 4) UC-3 사용자 거절 - 이전 Version 유지.
    r7 = wf.submit(mandate_id="m1", fund_id="f1", policy=_policy("0.4"),
                   objective_text="한도 확대 2", objective={}, effective_from=_tick(), created_by="u",
                   trace_id="t3", now=_clock[0], previous_policy=_policy("0.3"))
    v4_id = wf._version_repo.get_mandate_version_id("m1", 4)
    for role, dept in ((RequiredRole.RISK, "risk-management"), (RequiredRole.QA, "qa-department")):
        a = approvals.find(ObjectType.MANDATE_VERSION, v4_id, role)
        approvals.save(decide_approval(a, decision=ApprovalDecision.APPROVED, actor_department=dept,
                                       at=_tick()))
    wf.advance(r7.case_id, at=_tick())
    user_a3 = approvals.find(ObjectType.MANDATE_VERSION, v4_id, RequiredRole.USER)
    approvals.save(decide_approval(user_a3, decision=ApprovalDecision.REJECTED, at=_tick(),
                                   actor_user_id="user-1", reason="너무 위험함"))
    r8 = wf.advance(r7.case_id, at=_tick())
    assert r8.stage is ChangeStage.USER_REJECTED, r8
    assert wf._version_repo.get_mandate_current("m1") == (3, "ACTIVE"), "거절인데 Version이 바뀜"
    print("ok - UC-3(사용자 거절 - 이전 Version 유지) 통과")

    # 5) UC-6 Risk 거절 - 사용자 승인 단계로 가지 않는다.
    r9 = wf.submit(mandate_id="m1", fund_id="f1", policy=_policy("0.5"),
                   objective_text="한도 확대 3", objective={}, effective_from=_tick(), created_by="u",
                   trace_id="t4", now=_clock[0], previous_policy=_policy("0.3"))
    v5_id = wf._version_repo.get_mandate_version_id("m1", 5)
    risk_a5 = approvals.find(ObjectType.MANDATE_VERSION, v5_id, RequiredRole.RISK)
    approvals.save(decide_approval(risk_a5, decision=ApprovalDecision.REJECTED,
                                   actor_department="risk-management", at=_tick(), reason="한도 충돌"))
    r10 = wf.advance(r9.case_id, at=_tick())
    assert r10.stage is ChangeStage.REVIEW_REJECTED, r10
    assert approvals.find(ObjectType.MANDATE_VERSION, v5_id, RequiredRole.USER) is None, \
        "Risk가 거절했는데 사용자 승인 요청이 만들어짐"
    print("ok - UC-6(Risk 거절 - 사용자 승인 단계로 안 감) 통과")

    # 6) UC-5 만료 - advance() 호출 시점에 지연 평가되고, 승인 방향으로 떨어지지 않는다.
    review_created_at = _tick()
    r11 = wf.submit(mandate_id="m1", fund_id="f1", policy=_policy("0.45"),
                    objective_text="한도 확대 4", objective={}, effective_from=review_created_at,
                    created_by="u", trace_id="t5", now=review_created_at,
                    previous_policy=_policy("0.3"),
                    review_expires_at=review_created_at + timedelta(hours=1))
    v6_id = wf._version_repo.get_mandate_version_id("m1", 6)
    risk_a6 = approvals.find(ObjectType.MANDATE_VERSION, v6_id, RequiredRole.RISK)
    assert risk_a6.decision is ApprovalDecision.PENDING
    t_late = review_created_at + timedelta(hours=2)  # expires_at을 지남
    r12 = wf.advance(r11.case_id, at=t_late)
    assert r12.stage is ChangeStage.REVIEW_REJECTED and "EXPIRED" in r12.detail, r12
    assert approvals.find(ObjectType.MANDATE_VERSION, v6_id, RequiredRole.RISK).decision \
        is ApprovalDecision.EXPIRED, "지연 평가로 EXPIRED가 저장되지 않음"
    _clock[0] = t_late

    # UC-5: USER approval expires relative to Risk/QA completion, not submit time.
    user_request_at = _tick()
    r13 = wf.submit(mandate_id="m1", fund_id="f1", policy=_policy("0.46"),
                    objective_text="user approval expiry", objective={},
                    effective_from=user_request_at, created_by="u", trace_id="t6",
                    now=user_request_at, previous_policy=_policy("0.3"),
                    user_approval_ttl_seconds=60 * 60)
    v7_id = wf._version_repo.get_mandate_version_id("m1", 7)
    for role, dept in ((RequiredRole.RISK, "risk-management"), (RequiredRole.QA, "qa-department")):
        approval = approvals.find(ObjectType.MANDATE_VERSION, v7_id, role)
        approvals.save(decide_approval(approval, decision=ApprovalDecision.APPROVED,
                                       actor_department=dept, at=user_request_at))
    r14 = wf.advance(r13.case_id, at=user_request_at)
    assert r14.stage is ChangeStage.AWAITING_USER_APPROVAL, r14
    user_a7 = approvals.find(ObjectType.MANDATE_VERSION, v7_id, RequiredRole.USER)
    assert user_a7.expires_at == user_request_at + timedelta(hours=1), user_a7
    r15 = wf.advance(r13.case_id, at=user_request_at + timedelta(hours=2))
    assert r15.stage is ChangeStage.USER_REJECTED, r15
    assert approvals.find(ObjectType.MANDATE_VERSION, v7_id, RequiredRole.USER).decision \
        is ApprovalDecision.EXPIRED
    assert wf._version_repo.get_mandate_current("m1") == (3, "ACTIVE")
    print("ok - UC-5(user approval expiry preserves prior version)")

    # UC-4: a counterproposal starts a new version and repeats every review gate.
    counter_at = user_request_at + timedelta(hours=3)
    r16 = wf.submit(mandate_id="m1", fund_id="f1", policy=_policy("0.35"),
                    objective_text="counterproposal", objective={}, effective_from=counter_at,
                    created_by="u", trace_id="t7", now=counter_at,
                    previous_policy=_policy("0.3"))
    assert r16.stage is ChangeStage.AWAITING_REVIEW and r16.version == 8, r16
    v8_id = wf._version_repo.get_mandate_version_id("m1", 8)
    for role, dept in ((RequiredRole.RISK, "risk-management"), (RequiredRole.QA, "qa-department")):
        approval = approvals.find(ObjectType.MANDATE_VERSION, v8_id, role)
        assert approval is not None and approval.decision is ApprovalDecision.PENDING
        approvals.save(decide_approval(approval, decision=ApprovalDecision.APPROVED,
                                       actor_department=dept, at=counter_at))
    r17 = wf.advance(r16.case_id, at=counter_at)
    assert r17.stage is ChangeStage.AWAITING_USER_APPROVAL, r17
    user_a8 = approvals.find(ObjectType.MANDATE_VERSION, v8_id, RequiredRole.USER)
    approvals.save(decide_approval(user_a8, decision=ApprovalDecision.APPROVED,
                                   actor_user_id="user-1", at=counter_at))
    r18 = wf.advance(r16.case_id, at=counter_at)
    assert r18.stage is ChangeStage.ACTIVATED
    assert wf._version_repo.get_mandate_current("m1") == (8, "ACTIVE")
    print("ok - UC-4(counterproposal restarts the complete approval pipeline)")

    # UC-7: recreate the orchestrator between each decision; only repository state resumes.
    restart_at = counter_at + timedelta(hours=1)
    r19 = wf.submit(mandate_id="m1", fund_id="f1", policy=_policy("0.47"),
                    objective_text="restart recovery", objective={}, effective_from=restart_at,
                    created_by="u", trace_id="t8", now=restart_at,
                    previous_policy=_policy("0.35"))
    v9_id = wf._version_repo.get_mandate_version_id("m1", 9)
    for role, dept in ((RequiredRole.RISK, "risk-management"), (RequiredRole.QA, "qa-department")):
        approval = approvals.find(ObjectType.MANDATE_VERSION, v9_id, role)
        approvals.save(decide_approval(approval, decision=ApprovalDecision.APPROVED,
                                       actor_department=dept, at=restart_at))
    wf_after_restart = MandateChangeWorkflow(
        version_repo=wf._version_repo, version_service=MandateVersionService(wf._version_repo),
        activation_service=MandateActivationService(wf._version_repo),
        approval_repo=approvals, case_repo=cases,
    )
    r20 = wf_after_restart.advance(r19.case_id, at=restart_at)
    assert r20.stage is ChangeStage.AWAITING_USER_APPROVAL, r20
    user_a9 = approvals.find(ObjectType.MANDATE_VERSION, v9_id, RequiredRole.USER)
    approvals.save(decide_approval(user_a9, decision=ApprovalDecision.APPROVED,
                                   actor_user_id="user-1", at=restart_at))
    wf_after_second_restart = MandateChangeWorkflow(
        version_repo=wf._version_repo, version_service=MandateVersionService(wf._version_repo),
        activation_service=MandateActivationService(wf._version_repo),
        approval_repo=approvals, case_repo=cases,
    )
    r21 = wf_after_second_restart.advance(r19.case_id, at=restart_at)
    assert r21.stage is ChangeStage.ACTIVATED
    assert wf._version_repo.get_mandate_current("m1") == (9, "ACTIVE")
    print("ok - UC-7(recreated workflow resumes from persisted approvals and case)")

    # UC-6 also applies when QA, rather than Risk, rejects the proposal.
    qa_reject_at = restart_at + timedelta(hours=1)
    r22 = wf.submit(mandate_id="m1", fund_id="f1", policy=_policy("0.49"),
                    objective_text="qa rejection", objective={}, effective_from=qa_reject_at,
                    created_by="u", trace_id="t9", now=qa_reject_at,
                    previous_policy=_policy("0.47"))
    v10_id = wf._version_repo.get_mandate_version_id("m1", 10)
    qa_a10 = approvals.find(ObjectType.MANDATE_VERSION, v10_id, RequiredRole.QA)
    approvals.save(decide_approval(qa_a10, decision=ApprovalDecision.REJECTED,
                                   actor_department="qa-department", at=qa_reject_at))
    r23 = wf.advance(r22.case_id, at=qa_reject_at)
    assert r23.stage is ChangeStage.REVIEW_REJECTED, r23
    assert approvals.find(ObjectType.MANDATE_VERSION, v10_id, RequiredRole.USER) is None
    assert wf._version_repo.get_mandate_current("m1") == (9, "ACTIVE")
    print("ok - UC-6(QA rejection never reaches user approval)")
    print("ok - UC-5(만료 지연 평가 - 승인 방향으로 안 떨어짐) 통과")

    # 7) 종료된 Case 재advance 차단.
    try:
        wf.advance(r7.case_id, at=_tick())
        raise AssertionError("RESOLVED Case를 다시 advance함")
    except CaseAlreadyResolvedError:
        pass
    print("ok - 종료된 Case 재advance 차단 확인")

    print("ok - HITL Mandate 변경 워크플로 7개 시나리오(UC-1,2,3,5,6 포함) 통과")

    # -----------------------------------------------------------------------
    # 실 DB 통합 검증 - PostgresMandateVersionRepository/PostgresApprovalRepository/
    # PostgresCaseRepository 세 개를 실제로 조합해 UC-1 전체 경로(제출 -> Risk/QA 병렬
    # 승인 -> 사용자 승인 -> 활성화)를 태운다. 세 Repository는 각자 독립 커넥션 풀이고
    # 하나의 DB 트랜잭션으로 묶이지 않는다 - 이게 이 모듈의 설계다(모듈 docstring
    # "승인 대기의 진실은 DB다" 참고, 원자성이 아니라 각 단계 재실행 가능성으로 안전성을
    # 확보한다). 이 통합 점검은 그 조합이 실제로 맞물려 돌아가는지만 본다.
    # -----------------------------------------------------------------------
    import os

    from dotenv import load_dotenv

    load_dotenv()  # 저장소 루트 .env - 이미 설정된 값은 덮어쓰지 않는다.

    dsn = os.environ.get("GOVERNANCE_WORKFORCE_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not dsn:
        print("DATABASE_URL 미설정 - 실 DB 통합 검증은 건너뛴다")
        raise SystemExit(0)

    from postgres_approval_repository import PostgresApprovalRepository
    from postgres_case_repository import PostgresCaseRepository
    from postgres_repository import PostgresMandateVersionRepository

    version_repo = PostgresMandateVersionRepository.connect(dsn)
    approval_repo = PostgresApprovalRepository.connect(dsn)
    case_repo = PostgresCaseRepository.connect(dsn)
    wf = MandateChangeWorkflow(
        version_repo=version_repo, version_service=MandateVersionService(version_repo),
        activation_service=MandateActivationService(version_repo),
        approval_repo=approval_repo, case_repo=case_repo,
    )

    mandate_name = "HITL Mandate Change selfcheck (change_workflow.py)"
    try:
        conn = version_repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute("select fund_id from accounting.funds where fund_code = %s",
                           ("TEST-CEO-MANDATE",))
                fund_row = cur.fetchone()
                cur.execute("select user_id from governance.user_profiles limit 1")
                user_row = cur.fetchone()
                cur.execute(
                    "select case_id from governance.cases where created_by = 'selfcheck' "
                    "and case_type = 'MANDATE_CHANGE' order by created_at limit 1"
                )
                existing_case = cur.fetchone()
        finally:
            version_repo._pool.putconn(conn)

        if fund_row is None or user_row is None:
            print("SKIP - TEST-CEO-MANDATE Fund 또는 플레이스홀더 회원이 없다")
            raise SystemExit(0)
        fund_id, owner_user_id = str(fund_row[0]), str(user_row[0])

        if existing_case is not None:
            # governance.case_events는 append-only라 정리할 수 없다(GOV-02 Case Root
            # 자체 점검과 같은 상황) - 실행마다 새 Case를 쌓지 않고 기존 흔적으로 읽기
            # 경로만 재확인한다.
            case_id = str(existing_case[0])
            case = case_repo.get(case_id)
            assert case is not None and case.is_terminal
            timeline = case_repo.timeline(case_id)
            assert len(timeline) >= 1
            print(f"ok - 기존 자체 점검 Case({case.display_id}, {case.status.value})로 "
                  f"읽기 경로 검증 통과 - event {len(timeline)}건")
            print("SKIP - 쓰기 경로는 건너뛴다 (case_events append-only라 정리 불가, "
                  "공유 DB에 행 누적 방지)")
            raise SystemExit(0)

        t0 = datetime(2026, 8, 4, tzinfo=timezone.utc)
        conn = version_repo._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "insert into governance.mandates (fund_id, owner_user_id, name) "
                    "values (%s, %s, %s) returning mandate_id",
                    (fund_id, owner_user_id, mandate_name),
                )
                mandate_id = str(cur.fetchone()[0])
            conn.commit()
        finally:
            version_repo._pool.putconn(conn)

        v1_id = None  # finally 블록이 submit() 실패 시에도 안전하게 참조할 수 있게 선초기화.
        try:
            # created_by="selfcheck" - 위 stale-detection 쿼리가 찾는 값과 일치시킨다
            # (governance.cases.created_by는 uuid가 아니라 자유 텍스트라 안전하다). 실제
            # Mandate Version의 created_by는 별개로 version_created_by=owner_user_id를 쓴다
            # (submit() 인자가 다르다 - Case.created_by와 mandate_versions.created_by는
            # 컬럼 타입이 달라 같은 값을 공유할 수 없다).
            r0 = wf.submit(mandate_id=mandate_id, fund_id=fund_id, policy=_policy("0.2"),
                           objective_text="실 DB 통합 점검 - 최초 활성화", objective={},
                           effective_from=t0, created_by="selfcheck", trace_id=str(uuid.uuid4()),
                           now=t0, version_created_by=owner_user_id)
            assert r0.stage is ChangeStage.AWAITING_REVIEW, r0
            v1_id = version_repo.get_mandate_version_id(mandate_id, 1)
            for role, dept in ((RequiredRole.RISK, "risk-management"),
                              (RequiredRole.QA, "qa-department")):
                a = approval_repo.find(ObjectType.MANDATE_VERSION, v1_id, role)
                approval_repo.save(decide_approval(a, decision=ApprovalDecision.APPROVED,
                                                   actor_department=dept, at=t0))
            r1 = wf.advance(r0.case_id, at=t0)
            assert r1.stage is ChangeStage.AWAITING_USER_APPROVAL, r1
            user_a = approval_repo.find(ObjectType.MANDATE_VERSION, v1_id, RequiredRole.USER)
            approval_repo.save(decide_approval(user_a, decision=ApprovalDecision.APPROVED, at=t0,
                                               actor_user_id=owner_user_id, reason="실 DB 통합 점검"))
            r2 = wf.advance(r0.case_id, at=t0)
            assert r2.stage is ChangeStage.ACTIVATED, r2
            assert version_repo.get_mandate_current(mandate_id) == (1, "ACTIVE")
            case = case_repo.get(r0.case_id)
            assert case.status is CaseStatus.RESOLVED
            timeline = case_repo.timeline(r0.case_id)
            assert [e.to_status for e in timeline] == [CaseStatus.OPEN, CaseStatus.ACKNOWLEDGED,
                                                        CaseStatus.RESOLVED]
            print(f"ok - 실 DB 통합 검증 통과 - Case({case.display_id}) "
                  f"OPEN->ACKNOWLEDGED->RESOLVED, Mandate v1 ACTIVE 확인")
        finally:
            # mandates/mandate_versions/mandate_decisions/approvals는 append-only가 아니라
            # 정리 가능하다 - governance.cases/case_events만 흔적으로 남는다(위 설명 참고).
            conn = version_repo._pool.getconn()
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "delete from governance.mandate_decisions where mandate_version_id in "
                        "(select mandate_version_id from governance.mandate_versions "
                        "where mandate_id = %s)", (mandate_id,),
                    )
                    cur.execute(
                        "delete from governance.mandate_versions where mandate_id = %s",
                        (mandate_id,),
                    )
                    cur.execute("delete from governance.mandates where mandate_id = %s", (mandate_id,))
                conn.commit()
            finally:
                version_repo._pool.putconn(conn)
            if v1_id is not None:
                conn = approval_repo._pool.getconn()
                try:
                    with conn.cursor() as cur:
                        cur.execute(
                            "delete from governance.approvals where object_id = %s", (v1_id,)
                        )
                    conn.commit()
                finally:
                    approval_repo._pool.putconn(conn)
            print("note - Mandate/Version/Decision/Approval 정리 완료, Case 1건은 남는다 "
                  "(case_events append-only)")
    finally:
        version_repo.close()
        approval_repo.close()
        case_repo.close()
