#!/usr/bin/env python3
"""GOV-02 에스컬레이션 도메인 계약.

소유: 영주 (CEO Office)
근거: supabase/migrations/20260729000200_governance_workforce.sql(governance.escalations —
      severity/status 허용 값이 DDL에 이미 있다),
      docs/02-engineering/UNIFIED_DOMAIN_API_SPEC.md 5.4(Governance)
      (`POST /governance/v1/escalations` 제안), 5.3절(`governance.escalation.v1` Event),
      5.1절("risk-api — Trading State/Breach | CEO | Incident·Escalation")

여기엔 LLM이 없다. 상태 전이는 전부 결정론적 코드다.

상태 값을 새로 정할 필요가 없었다 - DDL에 이미 있다:
  severity: LOW | MEDIUM | HIGH | CRITICAL
  status:   OPEN -> ACKNOWLEDGED -> RESOLVED / CANCELLED

불변식:
  1. **`case_id`는 NOT NULL FK다.** 에스컬레이션은 항상 어떤 Case에 붙는다 - Case 없이
     떠 있는 에스컬레이션을 만들 수 없다(DDL이 강제하고, 그래서 Case Root 구현이 선행됐다).
  2. **RESOLVED에는 resolution이 필수다.** DDL은 resolution을 nullable로 두지만, 사유 없이
     닫힌 에스컬레이션은 추적이 끊긴다(MSU_SPEC 3절 "설명 없이 사라지는 Case는 허용하지
     않는다"와 같은 원칙). 애플리케이션 계층에서 요구한다.
  3. **Terminal(RESOLVED/CANCELLED)에서는 더 전이하지 않는다.** 되살리지 않고 새로 만든다.
  4. **CRITICAL을 조용히 낮추지 않는다.** severity는 생성 후 바뀌지 않는다 - 낮춰야 한다면
     기존 건을 CANCELLED로 닫고 새로 만들어 이력을 남긴다(위험 축소 방향의 무기록 변경 금지,
     CLAUDE.md "위험한 기능은 실패 시 거래 확대가 아니라 차단 방향").

자체 점검: python departments/00-ceo-office/src/escalation/escalation.py
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum


class Severity(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class EscalationStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


_ALLOWED: dict[EscalationStatus, frozenset[EscalationStatus]] = {
    EscalationStatus.OPEN: frozenset({EscalationStatus.ACKNOWLEDGED, EscalationStatus.CANCELLED}),
    EscalationStatus.ACKNOWLEDGED: frozenset({EscalationStatus.RESOLVED, EscalationStatus.CANCELLED}),
    EscalationStatus.RESOLVED: frozenset(),
    EscalationStatus.CANCELLED: frozenset(),
}

TERMINAL = frozenset({EscalationStatus.RESOLVED, EscalationStatus.CANCELLED})


class IllegalEscalationTransition(Exception):
    """허용되지 않은 에스컬레이션 상태 전이 (불변식 3)."""


class MissingResolutionError(Exception):
    """RESOLVED로 닫으려는데 resolution이 없다 (불변식 2)."""


@dataclass(frozen=True)
class EscalationRecord:
    """governance.escalations 한 행."""

    escalation_id: str
    case_id: str
    reason: str
    severity: Severity
    target: str
    status: EscalationStatus
    created_at: datetime
    due_at: datetime | None = None
    resolution: str | None = None
    resolved_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("reason은 비울 수 없다 (DDL not null)")
        if not self.target.strip():
            raise ValueError("target은 비울 수 없다 (DDL not null)")
        if not self.case_id.strip():
            raise ValueError("case_id는 비울 수 없다 (DDL not null FK, 불변식 1)")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL


def open_escalation(
    *,
    escalation_id: str,
    case_id: str,
    reason: str,
    severity: Severity,
    target: str,
    created_at: datetime,
    due_at: datetime | None = None,
) -> EscalationRecord:
    """OPEN 에스컬레이션을 만든다. case_id는 실제 존재하는 Case여야 한다(DB FK가 확인)."""
    if due_at is not None and due_at <= created_at:
        raise ValueError("due_at은 created_at 이후여야 한다")
    return EscalationRecord(
        escalation_id=escalation_id, case_id=case_id, reason=reason, severity=severity,
        target=target, status=EscalationStatus.OPEN, created_at=created_at, due_at=due_at,
    )


def transition(
    escalation: EscalationRecord,
    *,
    to_status: EscalationStatus,
    at: datetime,
    resolution: str | None = None,
) -> EscalationRecord:
    """상태를 전이한다. RESOLVED에는 resolution이 필수다 (불변식 2·3)."""
    allowed = _ALLOWED[escalation.status]
    if to_status not in allowed:
        if escalation.is_terminal:
            raise IllegalEscalationTransition(
                f"{escalation.status.value}는 Terminal이다 - 더 전이할 수 없다 "
                f"(요청: {to_status.value}). 되살리지 않고 새 에스컬레이션을 만든다"
            )
        raise IllegalEscalationTransition(
            f"{escalation.status.value} -> {to_status.value}는 허용되지 않는다 "
            f"(허용: {sorted(s.value for s in allowed)})"
        )

    if to_status is EscalationStatus.RESOLVED and not (resolution or "").strip():
        raise MissingResolutionError(
            "RESOLVED로 닫으려면 resolution이 필요하다 - 사유 없이 닫힌 에스컬레이션은 "
            "추적이 끊긴다 (불변식 2)"
        )

    resolved_at = at if to_status in TERMINAL else None
    return replace(
        escalation, status=to_status, resolution=resolution or escalation.resolution,
        resolved_at=resolved_at,
    )


class EscalationRepository:
    """조회·저장 인터페이스. 실제 구현은 governance.escalations에 반영한다."""

    def save(self, escalation: EscalationRecord) -> None:
        raise NotImplementedError

    def get(self, escalation_id: str) -> EscalationRecord | None:
        raise NotImplementedError

    def list_by_case(self, case_id: str) -> list[EscalationRecord]:
        raise NotImplementedError

    def list_open(self, *, target: str | None = None) -> list[EscalationRecord]:
        """미해결 건 조회. 스펙 5.3의 소비자(담당 본부, QA)가 Owner·기한을 추적하는 경로다."""
        raise NotImplementedError


class InMemoryEscalationRepository(EscalationRepository):
    def __init__(self) -> None:
        self._by_id: dict[str, EscalationRecord] = {}

    def save(self, escalation: EscalationRecord) -> None:
        self._by_id[escalation.escalation_id] = escalation

    def get(self, escalation_id: str) -> EscalationRecord | None:
        return self._by_id.get(escalation_id)

    def list_by_case(self, case_id: str) -> list[EscalationRecord]:
        return [e for e in self._by_id.values() if e.case_id == case_id]

    def list_open(self, *, target: str | None = None) -> list[EscalationRecord]:
        return [
            e for e in self._by_id.values()
            if not e.is_terminal and (target is None or e.target == target)
        ]


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/00-ceo-office/src/escalation/escalation.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta, timezone

    t0 = datetime(2026, 8, 4, tzinfo=timezone.utc)
    t1 = t0 + timedelta(hours=1)

    def _open(eid: str = "e1", *, severity: Severity = Severity.HIGH) -> EscalationRecord:
        return open_escalation(
            escalation_id=eid, case_id="c1", reason="Risk 한도 초과 미해결",
            severity=severity, target="risk-management", created_at=t0,
            due_at=t0 + timedelta(days=1),
        )

    # 1) 생성 - OPEN.
    esc = _open()
    assert esc.status is EscalationStatus.OPEN and esc.resolved_at is None

    # 2) 정상 경로 OPEN -> ACKNOWLEDGED -> RESOLVED(resolution 필수).
    ack = transition(esc, to_status=EscalationStatus.ACKNOWLEDGED, at=t1)
    assert ack.status is EscalationStatus.ACKNOWLEDGED and ack.resolved_at is None
    resolved = transition(ack, to_status=EscalationStatus.RESOLVED, at=t1,
                          resolution="한도 재적용 후 해소")
    assert resolved.status is EscalationStatus.RESOLVED
    assert resolved.resolution == "한도 재적용 후 해소" and resolved.resolved_at == t1

    # 3) resolution 없이 RESOLVED 불가 (불변식 2).
    for bad in (None, "", "   "):
        try:
            transition(ack, to_status=EscalationStatus.RESOLVED, at=t1, resolution=bad)
            raise AssertionError(f"resolution={bad!r}로 RESOLVED가 통과함")
        except MissingResolutionError:
            pass

    # 4) OPEN -> RESOLVED 직행 불가.
    try:
        transition(esc, to_status=EscalationStatus.RESOLVED, at=t1, resolution="x")
        raise AssertionError("OPEN -> RESOLVED 직행이 통과함")
    except IllegalEscalationTransition:
        pass

    # 5) Terminal 이후 전이 불가 (불변식 3).
    for target in (EscalationStatus.OPEN, EscalationStatus.ACKNOWLEDGED, EscalationStatus.CANCELLED):
        try:
            transition(resolved, to_status=target, at=t1, resolution="x")
            raise AssertionError(f"RESOLVED -> {target.value}가 통과함")
        except IllegalEscalationTransition:
            pass

    # 6) 취소는 OPEN/ACKNOWLEDGED 양쪽에서 가능하고 resolution이 필요 없다.
    for src in (esc, ack):
        cancelled = transition(src, to_status=EscalationStatus.CANCELLED, at=t1)
        assert cancelled.status is EscalationStatus.CANCELLED and cancelled.resolved_at == t1

    # 7) severity는 전이로 바뀌지 않는다 (불변식 4).
    critical = _open("e-crit", severity=Severity.CRITICAL)
    after = transition(critical, to_status=EscalationStatus.ACKNOWLEDGED, at=t1)
    assert after.severity is Severity.CRITICAL, "severity가 전이 중에 바뀌었다"

    # 8) 필수 필드 검증 (DDL not null과 같은 규칙).
    for kwargs in ({"reason": "  "}, {"target": ""}, {"case_id": ""}):
        try:
            open_escalation(
                escalation_id="e-bad", case_id=kwargs.get("case_id", "c1"),
                reason=kwargs.get("reason", "r"), severity=Severity.LOW,
                target=kwargs.get("target", "t"), created_at=t0,
            )
            raise AssertionError(f"{kwargs}가 통과함")
        except ValueError:
            pass

    # 9) due_at <= created_at 거부.
    try:
        open_escalation(escalation_id="e-bad", case_id="c1", reason="r",
                        severity=Severity.LOW, target="t", created_at=t0, due_at=t0)
        raise AssertionError("due_at <= created_at이 통과함")
    except ValueError:
        pass

    # 10) Repository - Case별 조회, 미해결 조회, target 필터.
    repo = InMemoryEscalationRepository()
    repo.save(_open("e1"))
    repo.save(_open("e2", severity=Severity.CRITICAL))
    repo.save(transition(_open("e3"), to_status=EscalationStatus.CANCELLED, at=t1))
    assert len(repo.list_by_case("c1")) == 3
    assert len(repo.list_by_case("c-other")) == 0
    assert {e.escalation_id for e in repo.list_open()} == {"e1", "e2"}  # CANCELLED 제외
    assert len(repo.list_open(target="risk-management")) == 2
    assert len(repo.list_open(target="qa-department")) == 0

    print("ok - GOV-02 에스컬레이션 도메인 계약 10개 시나리오 통과")
