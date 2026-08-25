#!/usr/bin/env python3
"""GOV-02 Case Root — 전사 Case의 도메인 계약.

소유: 영주 (CEO Office)
근거: docs/01-product/MINIMUM_SERVICE_UNIT_SPEC.md 12절(governance.cases/case_events),
      docs/02-engineering/UNIFIED_DOMAIN_API_SPEC.md 5.4(Governance - Case Root 는
      /governance/v1/cases), 실행 상태는 contracts/route-registry.v1.json,
      supabase/migrations/20260804000200_governance_case_status.sql(이번에 제안한 status 제약)

여기엔 LLM이 없다. Case 생성과 상태 전이는 전부 결정론적 코드다.

불변식:
  1. **cases는 Projection이고 case_events가 기준이다** (MSU_SPEC 12절). 상태를 바꿀 때마다
     반드시 case_events 한 줄이 함께 쌓이며, event 없이 status만 바꾸는 경로를 두지 않는다.
  2. **case_events는 Append-only이고 sequence는 case별로 1부터 연속이다.** 기존 event를
     수정하거나 삭제하는 함수가 없다.
  3. **Terminal 상태(RESOLVED/CANCELLED)에서는 더 전이하지 않는다.** MSU_SPEC 3절이
     "무기한 대기하거나 설명 없이 사라지는 Case는 허용하지 않는다"고 했으므로 끝난 Case를
     되살리지 않는다 - 새 Case를 만든다.
  4. **`idempotency_key`는 case_events의 unique 제약이다.** 같은 키로 두 번 전이시키면
     DB가 막는다(DDL `idempotency_key text not null unique`).

Investment Case의 19단계(DETECTED/.../EVALUATED)는 여기 없다 - 투자 전용이며 하위타입
governance.investment_cases와 case_events가 소유한다. 마이그레이션 주석에 근거를 적어뒀다.

자체 점검: python departments/00-ceo-office/src/case/case_root.py
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum


class CaseStatus(str, Enum):
    OPEN = "OPEN"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


# 허용 전이. 여기 없는 조합은 전부 IllegalCaseTransition이다 (불변식 3).
_ALLOWED: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.OPEN: frozenset({CaseStatus.ACKNOWLEDGED, CaseStatus.CANCELLED}),
    CaseStatus.ACKNOWLEDGED: frozenset({CaseStatus.RESOLVED, CaseStatus.CANCELLED}),
    CaseStatus.RESOLVED: frozenset(),
    CaseStatus.CANCELLED: frozenset(),
}

TERMINAL = frozenset({CaseStatus.RESOLVED, CaseStatus.CANCELLED})

# case_type은 DDL에 제약이 없다(마이그레이션 주석 참고 - 투자 Case 계약 미정이라 자유 텍스트).
# 스펙 2.2가 예시로 든 값만 상수로 둔다 - 검증이 아니라 호출자 편의용이다.
KNOWN_CASE_TYPES = ("MANDATE_CHANGE", "COMMITTEE", "INCIDENT", "HIRING", "IMPROVEMENT")

# display_id 접두어. NOT NULL unique인데 스펙 2.2 create_case Request에는 없어서 서버가
# 만들어야 한다. MSU_SPEC 8절이 투자 Case를 "IC-20260731-0001" 형태로 보여주므로 같은
# `PREFIX-YYYYMMDD-NNNN` 꼴을 따른다 - 접두어 매핑은 이 저장소가 처음 정하는 값이다.
_DISPLAY_PREFIX: dict[str, str] = {
    "MANDATE_CHANGE": "MC",
    "COMMITTEE": "CM",
    "INCIDENT": "IN",
    "HIRING": "HR",
    "IMPROVEMENT": "IM",
}
_DEFAULT_PREFIX = "GC"  # Generic Case - 위 표에 없는 case_type


class IllegalCaseTransition(Exception):
    """허용되지 않은 Case 상태 전이 (불변식 3)."""


def display_prefix(case_type: str) -> str:
    return _DISPLAY_PREFIX.get(case_type.upper(), _DEFAULT_PREFIX)


def build_display_id(case_type: str, *, created_at: datetime, sequence: int) -> str:
    """`MC-20260804-0001` 꼴. sequence는 (접두어, 날짜)별 연번이며 저장소가 계산한다."""
    if sequence < 1:
        raise ValueError("display_id sequence는 1부터 시작한다")
    return f"{display_prefix(case_type)}-{created_at:%Y%m%d}-{sequence:04d}"


@dataclass(frozen=True)
class CaseRecord:
    """governance.cases 한 행 (현재 상태 Projection)."""

    case_id: str
    fund_id: str
    display_id: str
    case_type: str
    priority: int
    status: CaseStatus
    owner_department: str
    trace_id: str
    created_by: str
    created_at: datetime
    updated_at: datetime
    due_at: datetime | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if not 0 <= self.priority <= 100:
            raise ValueError("priority는 0~100이다 (DDL check와 동일)")
        if self.schema_version < 1:
            raise ValueError("schema_version은 1 이상이다")
        if not self.case_type.strip():
            raise ValueError("case_type은 비울 수 없다")
        if not self.owner_department.strip():
            raise ValueError("owner_department는 비울 수 없다")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL


@dataclass(frozen=True)
class CaseEvent:
    """governance.case_events 한 행 (변경 이력의 기준, Append-only)."""

    case_id: str
    sequence: int
    event_type: str
    from_status: CaseStatus | None
    to_status: CaseStatus
    producer: str
    actor: str
    idempotency_key: str
    occurred_at: datetime
    reason: str | None = None
    payload: dict | None = None
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.sequence < 1:
            raise ValueError("sequence는 1부터 시작한다 (DDL check와 동일)")


def open_case(
    *,
    case_id: str,
    fund_id: str,
    display_id: str,
    case_type: str,
    priority: int,
    owner_department: str,
    trace_id: str,
    created_by: str,
    created_at: datetime,
    idempotency_key: str,
    due_at: datetime | None = None,
    reason: str | None = None,
    payload: dict | None = None,
) -> tuple[CaseRecord, CaseEvent]:
    """Case를 OPEN으로 만들고 그 사실을 기록하는 첫 event(sequence=1)를 함께 낸다 (불변식 1)."""
    record = CaseRecord(
        case_id=case_id, fund_id=fund_id, display_id=display_id, case_type=case_type,
        priority=priority, status=CaseStatus.OPEN, owner_department=owner_department,
        trace_id=trace_id, created_by=created_by, created_at=created_at,
        updated_at=created_at, due_at=due_at,
    )
    event = CaseEvent(
        case_id=case_id, sequence=1, event_type="case.opened", from_status=None,
        to_status=CaseStatus.OPEN, producer="governance-api", actor=created_by,
        idempotency_key=idempotency_key, occurred_at=created_at, reason=reason,
        payload=payload or {},
    )
    return record, event


def transition(
    case: CaseRecord,
    *,
    to_status: CaseStatus,
    actor: str,
    at: datetime,
    next_sequence: int,
    idempotency_key: str,
    reason: str | None = None,
    payload: dict | None = None,
    producer: str = "governance-api",
) -> tuple[CaseRecord, CaseEvent]:
    """상태를 바꾸고 case_events 한 줄을 함께 낸다. 둘은 항상 같이 나온다 (불변식 1).

    next_sequence는 저장소가 계산해 넘긴다 - 도메인이 DB의 현재 최대값을 알 수 없다.
    """
    allowed = _ALLOWED[case.status]
    if to_status not in allowed:
        if case.is_terminal:
            raise IllegalCaseTransition(
                f"{case.status.value}는 Terminal 상태다 - 더 전이할 수 없다 "
                f"(요청: {to_status.value}). 끝난 Case를 되살리지 않고 새 Case를 만든다"
            )
        raise IllegalCaseTransition(
            f"{case.status.value} -> {to_status.value}는 허용되지 않는다 "
            f"(허용: {sorted(s.value for s in allowed)})"
        )

    updated = replace(case, status=to_status, updated_at=at)
    event = CaseEvent(
        case_id=case.case_id, sequence=next_sequence,
        event_type=f"case.{to_status.value.lower()}", from_status=case.status,
        to_status=to_status, producer=producer, actor=actor,
        idempotency_key=idempotency_key, occurred_at=at, reason=reason, payload=payload or {},
    )
    return updated, event


class CaseRepository:
    """조회·저장 인터페이스. 실제 구현은 governance.cases/case_events에 반영한다."""

    def save_new(self, case: CaseRecord, event: CaseEvent) -> None:
        """Case와 첫 event를 한 트랜잭션으로 넣는다 (불변식 1)."""
        raise NotImplementedError

    def apply_transition(self, case: CaseRecord, event: CaseEvent) -> None:
        """status 갱신과 event append를 한 트랜잭션으로 처리한다 (불변식 1)."""
        raise NotImplementedError

    def get(self, case_id: str) -> CaseRecord | None:
        raise NotImplementedError

    def timeline(self, case_id: str) -> list[CaseEvent]:
        raise NotImplementedError

    def next_sequence(self, case_id: str) -> int:
        raise NotImplementedError

    def next_display_sequence(self, case_type: str, at: datetime) -> int:
        raise NotImplementedError


class InMemoryCaseRepository(CaseRepository):
    def __init__(self) -> None:
        self._cases: dict[str, CaseRecord] = {}
        self._events: dict[str, list[CaseEvent]] = {}
        self._idempotency_keys: set[str] = set()

    def _claim_key(self, key: str) -> None:
        if key in self._idempotency_keys:
            raise ValueError(f"idempotency_key 중복: {key} (DDL unique 제약과 동일)")
        self._idempotency_keys.add(key)

    def save_new(self, case: CaseRecord, event: CaseEvent) -> None:
        if any(c.display_id == case.display_id for c in self._cases.values()):
            raise ValueError(f"display_id 중복: {case.display_id}")
        if any(c.trace_id == case.trace_id for c in self._cases.values()):
            raise ValueError(f"trace_id 중복: {case.trace_id}")
        self._claim_key(event.idempotency_key)
        self._cases[case.case_id] = case
        self._events[case.case_id] = [event]

    def apply_transition(self, case: CaseRecord, event: CaseEvent) -> None:
        self._claim_key(event.idempotency_key)
        self._cases[case.case_id] = case
        self._events.setdefault(case.case_id, []).append(event)

    def get(self, case_id: str) -> CaseRecord | None:
        return self._cases.get(case_id)

    def timeline(self, case_id: str) -> list[CaseEvent]:
        return list(self._events.get(case_id, []))

    def next_sequence(self, case_id: str) -> int:
        return len(self._events.get(case_id, [])) + 1

    def next_display_sequence(self, case_type: str, at: datetime) -> int:
        prefix = display_prefix(case_type)
        same_day = [
            c for c in self._cases.values()
            if c.display_id.startswith(f"{prefix}-{at:%Y%m%d}-")
        ]
        return len(same_day) + 1


# ---------------------------------------------------------------------------
# 자체 점검 (python departments/00-ceo-office/src/case/case_root.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timedelta, timezone

    t0 = datetime(2026, 8, 4, tzinfo=timezone.utc)
    fund = "b13f5cd1-5df0-4025-92cf-9be03b1a0296"

    def _open(case_id: str, *, case_type: str = "HIRING", seq: int = 1, key: str | None = None):
        return open_case(
            case_id=case_id, fund_id=fund,
            display_id=build_display_id(case_type, created_at=t0, sequence=seq),
            case_type=case_type, priority=2, owner_department="hr-department",
            trace_id=f"trace-{case_id}", created_by="ceo-agent", created_at=t0,
            idempotency_key=key or f"open-{case_id}",
        )

    # 1) 생성 - OPEN + sequence 1 event가 함께 나온다 (불변식 1).
    case, ev = _open("c1")
    assert case.status is CaseStatus.OPEN
    assert (ev.sequence, ev.from_status, ev.to_status) == (1, None, CaseStatus.OPEN)
    assert case.display_id == "HR-20260804-0001"

    # 2) display_id 형식 - case_type별 접두어, 미지정 타입은 GC.
    assert build_display_id("MANDATE_CHANGE", created_at=t0, sequence=12) == "MC-20260804-0012"
    assert build_display_id("SOMETHING_NEW", created_at=t0, sequence=1) == "GC-20260804-0001"
    try:
        build_display_id("HIRING", created_at=t0, sequence=0)
        raise AssertionError("sequence 0이 통과함")
    except ValueError:
        pass

    # 3) OPEN -> ACKNOWLEDGED -> RESOLVED 정상 경로, 매 전이마다 event가 쌓인다.
    ack, ev2 = transition(case, to_status=CaseStatus.ACKNOWLEDGED, actor="hr-department",
                          at=t0 + timedelta(hours=1), next_sequence=2, idempotency_key="k2")
    assert ack.status is CaseStatus.ACKNOWLEDGED
    assert (ev2.from_status, ev2.to_status, ev2.sequence) == (CaseStatus.OPEN, CaseStatus.ACKNOWLEDGED, 2)
    assert ev2.event_type == "case.acknowledged"
    resolved, ev3 = transition(ack, to_status=CaseStatus.RESOLVED, actor="hr-department",
                               at=t0 + timedelta(hours=2), next_sequence=3, idempotency_key="k3")
    assert resolved.status is CaseStatus.RESOLVED and resolved.is_terminal

    # 4) Terminal에서 더 전이 불가 (불변식 3).
    for target in (CaseStatus.OPEN, CaseStatus.ACKNOWLEDGED, CaseStatus.CANCELLED):
        try:
            transition(resolved, to_status=target, actor="x", at=t0, next_sequence=4,
                       idempotency_key=f"k-bad-{target.value}")
            raise AssertionError(f"RESOLVED -> {target.value}가 통과함")
        except IllegalCaseTransition:
            pass

    # 5) OPEN -> RESOLVED 직행 불가 (ACKNOWLEDGED를 건너뛸 수 없다).
    try:
        transition(case, to_status=CaseStatus.RESOLVED, actor="x", at=t0, next_sequence=2,
                   idempotency_key="k-skip")
        raise AssertionError("OPEN -> RESOLVED 직행이 통과함")
    except IllegalCaseTransition:
        pass

    # 6) 취소는 OPEN과 ACKNOWLEDGED 양쪽에서 가능.
    assert transition(case, to_status=CaseStatus.CANCELLED, actor="x", at=t0,
                      next_sequence=2, idempotency_key="k-c1")[0].status is CaseStatus.CANCELLED
    assert transition(ack, to_status=CaseStatus.CANCELLED, actor="x", at=t0,
                      next_sequence=3, idempotency_key="k-c2")[0].status is CaseStatus.CANCELLED

    # 7) priority 범위와 필수 문자열 검증 (DDL check와 같은 규칙).
    for bad_priority in (-1, 101):
        try:
            _open("cx")[0].__class__(
                case_id="cx", fund_id=fund, display_id="X-1", case_type="HIRING",
                priority=bad_priority, status=CaseStatus.OPEN, owner_department="D",
                trace_id="t", created_by="a", created_at=t0, updated_at=t0,
            )
            raise AssertionError(f"priority {bad_priority}가 통과함")
        except ValueError:
            pass

    # 8) Repository - 생성/전이/timeline 왕복, sequence 연속성.
    repo = InMemoryCaseRepository()
    c2, e2 = _open("c2", seq=repo.next_display_sequence("HIRING", t0))
    repo.save_new(c2, e2)
    assert repo.get("c2").status is CaseStatus.OPEN
    assert repo.next_sequence("c2") == 2
    c2b, e2b = transition(c2, to_status=CaseStatus.ACKNOWLEDGED, actor="hr", at=t0,
                          next_sequence=repo.next_sequence("c2"), idempotency_key="c2-ack")
    repo.apply_transition(c2b, e2b)
    tl = repo.timeline("c2")
    assert [e.sequence for e in tl] == [1, 2]
    assert [e.to_status for e in tl] == [CaseStatus.OPEN, CaseStatus.ACKNOWLEDGED]

    # 9) idempotency_key 중복 차단 (불변식 4).
    c3, e3 = _open("c3", seq=repo.next_display_sequence("HIRING", t0), key="c2-ack")
    try:
        repo.save_new(c3, e3)
        raise AssertionError("idempotency_key 중복이 통과함")
    except ValueError:
        pass

    # 10) display_id 연번이 같은 날 같은 타입 안에서 증가한다.
    assert repo.next_display_sequence("HIRING", t0) == 2
    assert repo.next_display_sequence("INCIDENT", t0) == 1  # 접두어가 다르면 별도 연번

    print("ok - GOV-02 Case Root 도메인 계약 10개 시나리오 통과")
