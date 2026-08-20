#!/usr/bin/env python3
"""유휴 Agent 관측 결과를 사람이 읽는 리포트로 만든다 (2026-08-20 신규).

소유: 영주 (Agent Workforce 인사팀)
근거: hermes/SOUL.md "Current State Read Path"(권위 있는 읽기 경로) + Working Style
      ("Flag underperforming or idle existing Agents ... as readily as you propose
      new hires") — 그 지시를 실제로 수행할 간선이 없어서 채운다.

observability.py 가 판정을 만들고, 이 모듈은 그 판정을 **옮기기만** 한다. 집계도
셈이지 판단이 아니라서 여기에도 LLM 이 없다(quality.py/cost.py 와 같은 이유).

## 이 리포트가 지키는 단 하나의 규칙

**"우리가 모른다"를 "쉬고 있다"로 렌더링하지 않는다.**

네 상태를 끝까지 구분해서 보여준다 - 합쳐서 "유휴 N명"이라고 쓰는 순간 HR 이
정리 대상 목록으로 읽고, 관측 실패나 미발화 trigger 때문에 **실제로 일하는
Agent 가 잘린다**(2026-08-20 실측으로 그 위험이 확인됐다: 계측 배선이 빠진
경로의 Worker 는 전부 UNOBSERVED 로 보였다).

  ACTIVE      임계 시간 안에 실행이 관측됨
  IDLE        관측은 됐는데 임계 시간보다 오래 전 — **유일하게** 조치를 논할 수 있는 상태
  UNOBSERVED  이 창 안에 한 번도 안 잡힘 — conditional Worker 의 trigger 미발화일 수 있다
  UNAVAILABLE Langfuse 비활성·조회 실패 — 관측 자체를 못 했다

사용:
  python departments/07-agent-workforce/scorecard/idle_report.py
  python departments/07-agent-workforce/scorecard/idle_report.py --json
  python departments/07-agent-workforce/scorecard/idle_report.py --lookback-hours 72 --idle-threshold-hours 8
  python departments/07-agent-workforce/scorecard/idle_report.py --strict   # 관측 불가가 있으면 exit 2
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Sequence

try:
    from observability import (
        INVESTMENT_DEPARTMENT_STAGE,
        IdleStatus,
        WorkerIdleReport,
        WorkerRegistryUnavailable,
        check_idle_agents,
    )
except ModuleNotFoundError:  # 저장소 루트에서 직접 실행
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from observability import (  # type: ignore[no-redef]
        INVESTMENT_DEPARTMENT_STAGE,
        IdleStatus,
        WorkerIdleReport,
        WorkerRegistryUnavailable,
        check_idle_agents,
    )

# 표시 순서. 조치가 필요한 상태를 위로 올린다 - 스크롤해야 보이는 리포트는 안 읽힌다.
STATUS_ORDER: tuple[IdleStatus, ...] = (
    IdleStatus.IDLE,
    IdleStatus.UNOBSERVED,
    IdleStatus.UNAVAILABLE,
    IdleStatus.ACTIVE,
)

STATUS_NOTE: dict[IdleStatus, str] = {
    IdleStatus.IDLE: "관측됐지만 임계 시간보다 오래 전 - 조치를 논할 수 있는 유일한 상태",
    IdleStatus.UNOBSERVED: "이 창 안에 관측 없음 - trigger 미발화일 수 있어 결함으로 단정하지 않는다",
    IdleStatus.UNAVAILABLE: "관측 자체를 못 했다 - '쉬고 있다'가 아니라 '모른다'",
    IdleStatus.ACTIVE: "임계 시간 안에 실행 관측됨",
}


def summarize(reports: Sequence[WorkerIdleReport]) -> dict[str, int]:
    """상태별 인원. 없는 상태도 0 으로 채운다 - 키가 빠지면 소비자가 KeyError 를 만난다."""

    counted = Counter(report.status.value for report in reports)
    return {status.value: counted.get(status.value, 0) for status in STATUS_ORDER}


def as_payload(
    reports: Sequence[WorkerIdleReport],
    *,
    lookback_hours: float,
    idle_threshold_hours: float,
    now: datetime,
) -> dict[str, Any]:
    """기계용 JSON. API 응답(idle_agents)과 같은 원소 모양에 집계만 덧붙인다."""

    return {
        "schema_version": "workforce.idle_report.v1",
        "generated_at": now.isoformat(),
        "lookback_hours": lookback_hours,
        "idle_threshold_hours": idle_threshold_hours,
        "total_workers": len(reports),
        "summary": summarize(reports),
        "idle_agents": [report.as_dict() for report in reports],
    }


def _row(report: WorkerIdleReport) -> tuple[str, ...]:
    last_seen = report.last_seen_at.strftime("%m-%d %H:%M") if report.last_seen_at else "—"
    idle = f"{report.idle_hours:.1f}h" if report.idle_hours is not None else "—"
    return (report.department, report.worker_id, report.trigger, report.status.value, last_seen, idle)


def render_text(payload: dict[str, Any], reports: Sequence[WorkerIdleReport]) -> str:
    """고정폭 표. 렌더링이지 판단이 아니다 - 여기서 상태를 합치거나 고쳐 쓰지 않는다."""

    summary = payload["summary"]
    lines = [
        f"유휴 Agent 리포트 — {payload['generated_at']} 기준",
        f"창 {payload['lookback_hours']:g}h · 유휴 임계 {payload['idle_threshold_hours']:g}h · "
        f"대상 {payload['total_workers']}명 (6개 투자본부 LLM Worker)",
        "  " + " · ".join(f"{status.value} {summary[status.value]}" for status in STATUS_ORDER),
        "",
    ]

    headers = ("부서", "워커", "trigger", "상태", "마지막 관측", "유휴")
    rows = [_row(report) for report in sorted(
        reports, key=lambda r: (STATUS_ORDER.index(r.status), r.department, r.worker_id)
    )]
    widths = [max(len(str(cell)) for cell in column) for column in zip(headers, *rows)] if rows else [
        len(header) for header in headers
    ]
    lines.append("  ".join(header.ljust(width) for header, width in zip(headers, widths)).rstrip())
    lines.append("  ".join("-" * width for width in widths))
    for row in rows:
        lines.append("  ".join(str(cell).ljust(width) for cell, width in zip(row, widths)).rstrip())

    lines.append("")
    for status in STATUS_ORDER:
        if summary[status.value]:
            lines.append(f"  {status.value}: {STATUS_NOTE[status]}")

    if summary[IdleStatus.UNAVAILABLE.value]:
        lines.append("")
        lines.append(
            "  ⚠ UNAVAILABLE 이 있으면 이 리포트로 인원 조치를 결정하지 않는다 - "
            "관측 경로(LANGFUSE_* 자격증명)를 먼저 고친다."
        )
    return "\n".join(lines)


def build_report(
    *,
    lookback_hours: float = 24.0,
    idle_threshold_hours: float = 4.0,
    departments: tuple[str, ...] = tuple(INVESTMENT_DEPARTMENT_STAGE),
    now: datetime | None = None,
    reader: Any = None,
    include_heads: bool = False,
) -> tuple[dict[str, Any], list[WorkerIdleReport]]:
    now = now or datetime.now(timezone.utc)
    reports = check_idle_agents(
        reader=reader,
        departments=departments,
        lookback_hours=lookback_hours,
        idle_threshold_hours=idle_threshold_hours,
        now=now,
        include_heads=include_heads,
    )
    payload = as_payload(
        reports,
        lookback_hours=lookback_hours,
        idle_threshold_hours=idle_threshold_hours,
        now=now,
    )
    return payload, reports


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="6개 투자본부 LLM Worker 유휴 리포트")
    parser.add_argument("--lookback-hours", type=float, default=24.0)
    parser.add_argument("--idle-threshold-hours", type=float, default=4.0)
    parser.add_argument("--department", action="append", dest="departments",
                        choices=sorted(INVESTMENT_DEPARTMENT_STAGE),
                        help="반복 지정 가능. 생략하면 6개 본부 전부")
    parser.add_argument("--json", action="store_true", help="workforce.idle_report.v1 JSON 출력")
    parser.add_argument("--include-heads", action="store_true",
                        help="부서장(Hermes Profile head_persona)도 함께 본다. 기본은 Worker 만 - "
                             "관측 대상 인원이 조용히 늘면 '8명 중 3명 유휴' 같은 문장의 뜻이 바뀐다")
    parser.add_argument("--strict", action="store_true",
                        help="UNAVAILABLE 이 하나라도 있으면 exit 2 (관측 경로 감시용)")
    args = parser.parse_args(argv)

    # Windows 기본 콘솔은 cp949 라 표의 괘선·em dash 에서 UnicodeEncodeError 로 죽는다
    # (2026-08-20 실측). 리포트가 콘솔 인코딩 때문에 안 보이는 일은 없어야 한다.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    try:
        payload, reports = build_report(
            lookback_hours=args.lookback_hours,
            idle_threshold_hours=args.idle_threshold_hours,
            departments=tuple(args.departments) if args.departments else tuple(INVESTMENT_DEPARTMENT_STAGE),
            include_heads=args.include_heads,
        )
    except WorkerRegistryUnavailable as exc:
        # 빈 리포트(=유휴 없음)로 위장하지 않는다. app.py 가 503 으로 알리는 것과 같은 이유.
        print(f"worker registry 를 읽지 못했다: {exc}", file=sys.stderr)
        return 3
    except ValueError as exc:
        print(f"잘못된 인자: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json
          else render_text(payload, reports))

    if args.strict and payload["summary"][IdleStatus.UNAVAILABLE.value]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
