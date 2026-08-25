#!/usr/bin/env python3
"""F23: Daily Report — CEO Office 담당분 (본부별 Snapshot 조립·게이트).

소유: 영주 (CEO Office)
근거: docs/02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md F23,
      docs/05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md 5.4(Daily Report 필수 Section), 3.1(참조 데이터),
      docs/02-engineering/UNIFIED_DOMAIN_API_SPEC.md 5.4(Reporting)

F23은 두 부서가 나눠 맡는다. 회계본부의 `departments/05-accounting-portfolio/reporting/
daily_report.py`는 전략별 PnL·Drawdown·비용·오류 **수치를 계산**한다. 이 모듈은 **CEO Office
절반**이다 — 각 본부(회계/리스크/리서치/실행/전략/QA)가 이미 확정한 공식 Snapshot ID를
`governance.report_runs` 한 행으로 **조립**만 하고, 숫자를 다시 계산하지 않는다
(팀 가이드 3.1 "CEO는 외부 시장 데이터를 수집하지 않고 본부별 공식 API를 참조한다").

여기에 LLM은 없다. 완결성 판정과 idempotency는 결정론적 코드만 한다.

불변식:
  1. CEO는 다른 본부의 수치를 보유하지 않는다. 이 모듈이 다루는 값은 전부
     "무엇을(snapshot_id) 언제 기준(as_of)으로" 참조하는지 뿐이고, PnL·NAV 같은
     실제 숫자 필드는 어디에도 없다.
  2. 필수 Section(portfolio, risk)이 없으면 Report를 QUEUED로 진행시키지 않고 FAILED로
     떨어뜨린다 — 회사 상태의 근간(NAV/Risk State)이 없는 채로 "완료"라고 보고하지 않는다
     (개발 원칙 9: 위험한 기능은 실패 시 확대가 아니라 차단).
  3. 그 외 Section(research/execution/strategy/qa)은 없어도 진행한다 — "오늘 아무 일도
     없었다"는 그 자체로 유효한 정보이며, Read API 미비와 구분하지 않은 채 전부 필수로
     묶으면 조용한 날마다 Report가 영원히 실패한다.
  4. 같은 Section 구성(snapshot_id 집합)·템플릿·기준일에 다시 요청하면 새 행을 만들지
     않고 기존 Report를 반환한다 (content_hash idempotency, Mandate content_hash와 동일 원칙).
  5. `pending_user_action_case_ids`(결정이 필요한 사용자 Action)만은 예외적으로 CEO가
     직접 센다 — 이건 다른 본부 수치가 아니라 governance.escalations/approvals라는
     CEO Office 소유 데이터이기 때문이다.

자체 점검: python departments/00-ceo-office/src/reporting/daily_report.py
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime

REQUIRED_SECTION_FIELDS: tuple[str, ...] = ("portfolio", "risk")
ALL_SECTION_FIELDS: tuple[str, ...] = (
    "portfolio", "risk", "research", "execution", "strategy", "qa",
)


@dataclass(frozen=True)
class SnapshotRef:
    """다른 본부가 이미 확정한 공식 Snapshot의 참조. 값 자체는 담지 않는다 (불변식 1)."""

    snapshot_id: str
    as_of: datetime


@dataclass(frozen=True)
class DailyReportSections:
    """5.4 Daily Report 필수 Section의 참조 모음.

    portfolio(회계 NAV/PnL/Position/Cash), risk(리스크 Limit/Breach/Trading State)는 필수.
    research(리서치 Catalyst), execution(체결·Slippage·비용), strategy(전략 성과·Drift),
    qa(Finding·Incident)는 선택 — 없는 게 곧 "오늘 없었다"일 수 있다 (불변식 3).
    """

    portfolio: SnapshotRef | None
    risk: SnapshotRef | None
    research: SnapshotRef | None = None
    execution: SnapshotRef | None = None
    strategy: SnapshotRef | None = None
    qa: SnapshotRef | None = None
    # governance 자체 소유 — 다른 본부 참조가 아니라 CEO가 직접 계산한다 (불변식 5).
    pending_user_action_case_ids: tuple[str, ...] = field(default_factory=tuple)


def missing_required_sections(sections: DailyReportSections) -> tuple[str, ...]:
    return tuple(f for f in REQUIRED_SECTION_FIELDS if getattr(sections, f) is None)


def collect_snapshot_ids(sections: DailyReportSections) -> tuple[str, ...]:
    ids = [
        getattr(sections, f).snapshot_id
        for f in ALL_SECTION_FIELDS
        if getattr(sections, f) is not None
    ]
    return tuple(sorted(ids))


def _canonical_json(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def compute_content_hash(
    *, fund_id: str, report_type: str, as_of: date, template_version: str,
    snapshot_ids: tuple[str, ...],
) -> str:
    """같은 구성 -> 같은 hash (불변식 4). as_of는 날짜만 쓴다 — 같은 회계일이면 같은 Report."""
    payload = {
        "fund_id": fund_id,
        "report_type": report_type,
        "as_of": as_of.isoformat(),
        "template_version": template_version,
        "snapshot_ids": sorted(snapshot_ids),
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ReportRunRow:
    """governance.report_runs 한 행. report_id/created_at은 DB 기본값에 맡긴다."""

    fund_id: str
    report_type: str
    as_of: date
    source_snapshot_ids: tuple[str, ...]
    template_version: str
    content_hash: str
    status: str  # 'QUEUED' | 'FAILED' (RUNNING/COMPLETED은 Reporting Worker가 렌더링 후 전이)
    trace_id: str
    object_path: str | None = None


class ReportRunRepository:
    """조회·저장 인터페이스. 실제 구현은 governance.report_runs에 반영한다."""

    def find_by_content_hash(self, fund_id: str, content_hash: str) -> ReportRunRow | None:
        raise NotImplementedError

    def insert(self, row: ReportRunRow) -> None:
        raise NotImplementedError


class InMemoryReportRunRepository(ReportRunRepository):
    def __init__(self) -> None:
        self._rows: list[ReportRunRow] = []

    def find_by_content_hash(self, fund_id: str, content_hash: str) -> ReportRunRow | None:
        for row in self._rows:
            if row.fund_id == fund_id and row.content_hash == content_hash:
                return row
        return None

    def insert(self, row: ReportRunRow) -> None:
        self._rows.append(row)


@dataclass(frozen=True)
class DailyReportAssembly:
    row: ReportRunRow
    sections: DailyReportSections
    missing_required: tuple[str, ...]
    created: bool  # False면 동일 content_hash의 기존 Report를 재사용했다는 뜻 (불변식 4)


class DailyReportAssembler:
    """F23 CEO 절반 — Section 참조를 report_runs 행으로 조립한다."""

    def __init__(self, repo: ReportRunRepository) -> None:
        self._repo = repo

    def assemble(
        self, *, fund_id: str, as_of: date, template_version: str,
        sections: DailyReportSections, trace_id: str,
    ) -> DailyReportAssembly:
        missing = missing_required_sections(sections)
        snapshot_ids = collect_snapshot_ids(sections)
        content_hash = compute_content_hash(
            fund_id=fund_id, report_type="DAILY", as_of=as_of,
            template_version=template_version, snapshot_ids=snapshot_ids,
        )

        existing = self._repo.find_by_content_hash(fund_id, content_hash)
        if existing is not None:
            return DailyReportAssembly(
                row=existing, sections=sections, missing_required=missing, created=False,
            )

        # 불변식 2 — 필수 Section 누락은 FAILED. 선택 Section 누락은 진행한다 (불변식 3).
        status = "FAILED" if missing else "QUEUED"
        row = ReportRunRow(
            fund_id=fund_id, report_type="DAILY", as_of=as_of,
            source_snapshot_ids=snapshot_ids, template_version=template_version,
            content_hash=content_hash, status=status, trace_id=trace_id,
        )
        self._repo.insert(row)
        return DailyReportAssembly(row=row, sections=sections, missing_required=missing, created=True)


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import timezone

    d0 = date(2026, 7, 31)
    t0 = datetime(2026, 7, 31, 15, 30, tzinfo=timezone.utc)

    def ref(sid: str) -> SnapshotRef:
        return SnapshotRef(snapshot_id=sid, as_of=t0)

    # 1) 필수+선택 모두 있음 -> QUEUED, snapshot_id 전부 수집.
    repo = InMemoryReportRunRepository()
    asm = DailyReportAssembler(repo)
    sections_full = DailyReportSections(
        portfolio=ref("s-portfolio"), risk=ref("s-risk"), research=ref("s-research"),
        execution=ref("s-execution"), strategy=ref("s-strategy"), qa=ref("s-qa"),
        pending_user_action_case_ids=("case-1",),
    )
    r1 = asm.assemble(fund_id="f1", as_of=d0, template_version="v1", sections=sections_full, trace_id="t1")
    assert r1.row.status == "QUEUED" and r1.missing_required == () and r1.created is True
    assert set(r1.row.source_snapshot_ids) == {
        "s-portfolio", "s-risk", "s-research", "s-execution", "s-strategy", "s-qa",
    }

    # 2) 필수 Section(portfolio) 누락 -> FAILED.
    sections_no_portfolio = DailyReportSections(portfolio=None, risk=ref("s-risk"))
    r2 = asm.assemble(
        fund_id="f1", as_of=d0, template_version="v1", sections=sections_no_portfolio, trace_id="t2",
    )
    assert r2.row.status == "FAILED"
    assert r2.missing_required == ("portfolio",)

    # 3) 필수 Section(risk) 누락 -> FAILED.
    sections_no_risk = DailyReportSections(portfolio=ref("s-portfolio"), risk=None)
    r3 = asm.assemble(
        fund_id="f1", as_of=d0, template_version="v1", sections=sections_no_risk, trace_id="t3",
    )
    assert r3.missing_required == ("risk",)

    # 4) 선택 Section이 전부 없어도 필수만 있으면 QUEUED (불변식 3 — 조용한 날도 유효).
    sections_quiet_day = DailyReportSections(portfolio=ref("s-portfolio"), risk=ref("s-risk"))
    r4 = asm.assemble(
        fund_id="f1", as_of=d0, template_version="v1", sections=sections_quiet_day, trace_id="t4",
    )
    assert r4.row.status == "QUEUED", "선택 Section 없음을 실패로 취급했다"
    assert r4.row.source_snapshot_ids == ("s-portfolio", "s-risk")

    # 5) content_hash 재현성 — 같은 구성은 같은 hash, snapshot_id가 다르면 다른 hash.
    h1 = compute_content_hash(
        fund_id="f1", report_type="DAILY", as_of=d0, template_version="v1",
        snapshot_ids=("a", "b"),
    )
    h2 = compute_content_hash(
        fund_id="f1", report_type="DAILY", as_of=d0, template_version="v1",
        snapshot_ids=("b", "a"),
    )
    assert h1 == h2, "정렬 전이면 순서만 다른데 hash가 달라졌다"
    h3 = compute_content_hash(
        fund_id="f1", report_type="DAILY", as_of=d0, template_version="v1",
        snapshot_ids=("a", "c"),
    )
    assert h1 != h3

    # 6) 동일 구성 재요청은 새 행을 안 만들고 기존 Report를 반환한다 (idempotent, 불변식 4).
    r4_again = asm.assemble(
        fund_id="f1", as_of=d0, template_version="v1", sections=sections_quiet_day, trace_id="t4-retry",
    )
    assert r4_again.created is False
    assert r4_again.row is r4.row
    assert len(repo._rows) == 4, "r1/r2/r3/r4 4건 삽입, r4 재요청은 동일 content_hash라 안 늘어나야 한다"

    # 7) pending_user_action_case_ids는 CEO 자체 데이터로 Section에 그대로 실린다 (불변식 5).
    assert r1.sections.pending_user_action_case_ids == ("case-1",)

    print("ok - F23 CEO Daily Report 조립 7개 시나리오 통과")
