#!/usr/bin/env python3
"""부서 Scorecard 를 HR 부서장(LLM) 컨텍스트용 마크다운 표로 인코딩한다 (2026-08-26 신규).

소유: 영주 (Agent Workforce 인사팀)
근거: hermes/config.yaml selection-performance-agent — "각 부서의 Queue/SLA/Cost/Quality
      Scorecard 와 Eval score 추세를 주기적으로 검토해 개정이 필요한 Profile 을 식별한다".
      그 검토 대상인 build_department_scorecard() 응답은 window/capacity/cost/quality 4블록
      중첩 JSON 이라 부서 6개를 나란히 놓고 비교하기에 맞지 않는 모양이다.

idle_report.py 와 같은 자리다 — observability.py 가 판정을 만들고 idle_report 가 사람용으로
옮기듯, cost.py 가 집계·판정을 만들고 이 모듈은 **LLM 이 읽을 형태로 옮기기만** 한다.
그래서 여기에도 LLM 이 없고, 임계값도 없다.

## 이 렌더러가 지키는 세 가지 규칙

**1. `—` 와 `0` 을 같게 렌더링하지 않는다.**
cost.py 불변식 3 그대로다. Snapshot 이 없어서 값이 없는 것(`—`)을 `0` 으로 채우면
"예산 여유 있음"·"오류 없음"으로 정반대로 읽힌다. 블록 전체가 없는 경우(capacity: null)는
셀을 비우는 데 그치지 않고 **관측 컬럼에 NO_SNAPSHOT 을 따로 세운다** — 필드 하나가 빈
것과 스냅샷 자체가 없는 것은 다른 사실이다.

**2. 판정을 여기서 만들지 않는다.**
status·권고 컬럼에는 assess_budget() 이 이미 만든 BudgetStatus/RecommendedAction 값만
싣는다. 이 모듈은 WARNING_RATIO 를 모르고, 알 필요도 없다(CLAUDE.md: 규칙 판정은 Python
이 하고 LLM 은 서술만 한다 — 그 판정을 프롬프트 안의 루브릭으로 옮기면 결정론이 사라진다).

**3. 관측 창이 다른 부서를 조용히 한 표에 세우지 않는다.**
창이 서로 다르면 부서별 창 표를 따로 내고 경고를 남긴다. 같은 표에 있다는 이유로
비교 가능하다고 읽히는 게 이 브리프에서 가장 비싼 오독이다.

## 왜 중첩 JSON 대신 표인가

부서 6개 × 지표 18개는 관계형 데이터고, 중첩 JSON 으로 주면 `department_code` 같은 key
문자열이 부서 수만큼 반복돼 토큰을 먹는다(_compact() 는 8000자에서 그냥 자르므로 반복 key
가 늘수록 실제 수치가 잘려나갈 확률이 올라간다). 자체 점검이 두 표현의 문자 수를 같이
출력하니 실측으로 확인할 수 있다.

사용:
  python departments/07-agent-workforce/scorecard/scorecard_brief.py            # 자체 점검
  python departments/07-agent-workforce/scorecard/scorecard_brief.py --input s.json
  cat scorecards.json | python departments/07-agent-workforce/scorecard/scorecard_brief.py -
"""

from __future__ import annotations

import argparse
import json
import sys
from enum import Enum
from pathlib import Path
from typing import Any, Sequence

try:
    from reporting import md_cell
except ModuleNotFoundError:  # scorecard/ 에서 직접 실행 — 부서 루트가 sys.path 에 없다
    # insert(0) 이 아니라 append 다. 부서 루트에는 scripts.py 가 있고, 저장소 루트에는
    # scripts/ 패키지가 있다 — 앞에 끼우면 이 모듈을 import 한 프로세스 전체에서
    # `import scripts.*` 가 모듈에 막혀 깨진다(같은 세션에서 도는 다른 테스트가 실제
    # 피해자다). 뒤에 붙이면 기존 경로가 먼저 이기고 reporting 만 추가로 잡힌다.
    sys.path.append(str(Path(__file__).resolve().parent.parent))
    from reporting import md_cell  # type: ignore[no-redef]

SCHEMA_VERSION = "workforce.scorecard_brief.v1"

# 결측 표기. reporting.md_cell(None) 이 내는 값과 같아야 한다 — 두 곳이 갈리면
# 범례가 표와 어긋난다.
MISSING = "—"


class BlockStatus(str, Enum):
    """블록 단위 관측 여부. 필드 결측(`—`)과 구분하려고 따로 세운다."""

    OBSERVED = "OBSERVED"
    NO_SNAPSHOT = "NO_SNAPSHOT"  # Snapshot 자체가 없음 — 값이 0인 것이 아니다


# (표시 이름, payload key). 단위는 DDL 을 따른다(cost.py 불변식 5).
CAPACITY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("arrivals", "arrivals"),
    ("queue_p95_ms", "queue_p95_ms"),
    ("duration_p95_ms", "duration_p95_ms"),
    ("retry_rate", "retry_rate"),
    ("error_rate", "error_rate"),
    ("utilization", "utilization"),
)
COST_COLUMNS: tuple[tuple[str, str], ...] = (
    ("input_tokens", "input_tokens"),
    ("output_tokens", "output_tokens"),
    ("model_cost", "model_cost"),
    ("tool_cost", "tool_cost"),
    ("infra_cost", "infra_cost"),
    ("case_count", "case_count"),
    ("currency", "currency"),
)
QUALITY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("eval_score", "eval_score"),
    ("finding_count", "finding_count"),
    ("rework_rate", "rework_rate"),
)

# 배열은 셀에 넣지 않는다 — 참조 표로 따로 뺀다(1행 1참조).
REFERENCE_KEYS: tuple[str, ...] = ("eval_run_ids", "role_kpi")


def _table(headers: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    """마크다운 표 한 개. 셀 이스케이프는 reporting.md_cell 한 곳만 담당한다."""

    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join("---" for _ in headers) + " |",
    ]
    lines.extend("| " + " | ".join(md_cell(cell) for cell in row) + " |" for row in rows)
    return lines


def _block_rows(
    payloads: Sequence[dict[str, Any]],
    *,
    block: str,
    columns: Sequence[tuple[str, str]],
) -> list[list[Any]]:
    """블록 하나를 부서 × 지표 행으로 편다.

    블록이 None 이면 값 셀을 전부 결측으로 두고 관측 컬럼에 NO_SNAPSHOT 을 세운다 —
    0 으로 채우지 않는다(규칙 1).
    """

    rows: list[list[Any]] = []
    for payload in payloads:
        data = payload.get(block)
        observed = isinstance(data, dict)
        row: list[Any] = [
            payload["department_code"],
            (BlockStatus.OBSERVED if observed else BlockStatus.NO_SNAPSHOT).value,
        ]
        row.extend((data.get(key) if observed else None) for _label, key in columns)
        rows.append(row)
    return rows


def render_capacity_table(payloads: Sequence[dict[str, Any]]) -> list[str]:
    headers = ["부서", "관측", *(label for label, _key in CAPACITY_COLUMNS)]
    return _table(headers, _block_rows(payloads, block="capacity", columns=CAPACITY_COLUMNS))


def render_cost_table(payloads: Sequence[dict[str, Any]]) -> list[str]:
    headers = ["부서", "관측", *(label for label, _key in COST_COLUMNS)]
    return _table(headers, _block_rows(payloads, block="cost", columns=COST_COLUMNS))


def render_quality_table(payloads: Sequence[dict[str, Any]]) -> list[str]:
    """quality 는 블록이 항상 있다 — 대신 eval_score 소유자가 여기가 아니다."""

    headers = ["부서", *(label for label, _key in QUALITY_COLUMNS), "eval_run 참조"]
    rows: list[list[Any]] = []
    for payload in payloads:
        quality = payload.get("quality") or {}
        row: list[Any] = [payload["department_code"]]
        row.extend(quality.get(key) for _label, key in QUALITY_COLUMNS)
        # 참조는 개수만 싣고 값은 참조 표로 보낸다. 0건은 "참조가 없었다"는 관측
        # 사실이므로 결측이 아니다(cost.py build_department_scorecard 주석과 동일).
        row.append(len(quality.get("eval_run_ids") or []))
        rows.append(row)
    return _table(headers, rows)


def render_reference_table(payloads: Sequence[dict[str, Any]]) -> list[str]:
    """배열 참조를 1행 1건으로 편다. 없으면 빈 목록을 반환한다(빈 표를 만들지 않는다)."""

    rows: list[list[Any]] = []
    for payload in payloads:
        quality = payload.get("quality") or {}
        for key in REFERENCE_KEYS:
            for item in quality.get(key) or []:
                value = item if isinstance(item, str) else json.dumps(
                    item, ensure_ascii=False, sort_keys=True, default=str
                )
                rows.append([payload["department_code"], key, value])
    if not rows:
        return []
    return _table(["부서", "참조 종류", "값"], rows)


def render_budget_table(assessments: Sequence[Any]) -> list[str]:
    """assess_budget() 결과를 옮기기만 한다 — 상태·권고를 여기서 다시 계산하지 않는다."""

    if not assessments:
        return []
    headers = [
        "에이전트", "부서", "tokens_used", "daily_budget", "usage_ratio",
        "status", "recommended_action", "통제부서", "note",
    ]
    rows = [
        [
            assessment.employee_code,
            assessment.department_code,
            assessment.tokens_used,
            assessment.daily_budget,
            None if assessment.usage_ratio is None else f"{float(assessment.usage_ratio):.3f}",
            assessment.status.value,
            assessment.recommended_action.value,
            "Y" if assessment.is_control_role else "N",
            assessment.note or None,
        ]
        for assessment in assessments
    ]
    return _table(headers, rows)


def _windows(payloads: Sequence[dict[str, Any]]) -> list[tuple[str, str, str]]:
    return [
        (
            payload["department_code"],
            payload["window"]["window_start"],
            payload["window"]["window_end"],
        )
        for payload in payloads
    ]


def build_scorecard_brief(
    payloads: Sequence[dict[str, Any]],
    *,
    assessments: Sequence[Any] = (),
) -> str:
    """부서장에게 넘길 브리프 한 장. 여기 없는 수치는 부서장도 만들 수 없다."""

    if not payloads:
        raise ValueError("scorecard payload 가 최소 1건 필요하다")
    for payload in payloads:
        if not payload.get("department_code") or not payload.get("window"):
            raise ValueError("scorecard payload 에 department_code/window 가 있어야 한다")

    windows = _windows(payloads)
    uniform = len({(start, end) for _code, start, end in windows}) == 1

    lines = [
        "# 부서 Scorecard 브리프",
        "",
        f"- schema: {SCHEMA_VERSION}",
        f"- 대상 부서: {len(payloads)}개",
    ]
    if uniform:
        _code, start, end = windows[0]
        lines.append(f"- 관측 창: {start} ~ {end} (전 부서 동일)")
    else:
        lines.append("- 관측 창: **부서마다 다르다** — 아래 창 표 참고")

    lines.extend([
        f"- 셀 표기: `{MISSING}` 는 값 없음(미집계·미관측)이고 `0` 은 실제로 관측된 0이다. "
        "둘을 같은 뜻으로 읽지 않는다.",
        "- 관측 컬럼의 NO_SNAPSHOT 은 그 블록의 Snapshot 자체가 없다는 뜻이다. "
        "사용량이 0이라는 뜻이 아니다.",
        "- status·recommended_action 은 결정론 코드(scorecard/cost.py assess_budget)가 이미 "
        "판정한 값이다. 다시 판정하지 말고 서술과 우선순위 근거로만 쓴다.",
        "- eval_score 는 QA/감사본부(audit.eval_runs) 소유라 이 표에 값이 오지 않는다. "
        f"`{MISSING}` 를 품질 문제로 읽지 말고 eval_run 참조를 열어 확인한다.",
        "- 이 표에 없는 수치는 만들지 않는다. 없으면 없다고 쓴다.",
        "",
    ])

    if not uniform:
        lines.extend([
            "## 관측 창",
            "",
            "⚠ 창이 서로 다른 부서를 같은 기준으로 비교하지 않는다.",
            "",
            *_table(["부서", "window_start", "window_end"], windows),
            "",
        ])

    lines.extend(["## 처리량·지연 (capacity)", "", *render_capacity_table(payloads), ""])
    lines.extend(["## 비용 (cost)", "", *render_cost_table(payloads), ""])
    lines.extend(["## 품질 (quality)", "", *render_quality_table(payloads), ""])

    references = render_reference_table(payloads)
    if references:
        lines.extend(["## 참조", "", *references, ""])

    budget = render_budget_table(assessments)
    if budget:
        lines.extend(["## 예산 판정 (결정론 산출물 — 재판정 금지)", "", *budget, ""])

    return "\n".join(lines).rstrip() + "\n"


def _load_payloads(source: str) -> list[dict[str, Any]]:
    """CLI 입력. API 응답 1건, 목록, `{"scorecards": [...]}` 셋 다 받는다."""

    raw = sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("scorecards", [data])
    if not isinstance(data, list):
        raise ValueError("scorecard payload 목록을 읽지 못했다")
    return data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="부서 Scorecard 응답을 HR 부서장 컨텍스트용 마크다운 브리프로 인코딩"
    )
    parser.add_argument(
        "--input", default="-",
        help="scorecard 응답 JSON 파일 경로. 생략하거나 '-' 면 stdin",
    )
    args = parser.parse_args(argv)

    # idle_report.py 와 같은 이유 — Windows 기본 콘솔(cp949)에서 표의 em dash 가
    # UnicodeEncodeError 로 죽는다. stderr 도 함께 바꾼다: 실패 사유가 깨진 글자로
    # 나오면 왜 브리프가 없는지 알 수 없다.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    try:
        payloads = _load_payloads(args.input)
        print(build_scorecard_brief(payloads))
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print(f"브리프를 만들지 못했다: {exc}", file=sys.stderr)
        return 2
    return 0


# ---------------------------------------------------------------------------
# 자체 점검
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from datetime import datetime, timezone
    from decimal import Decimal

    try:
        from cost import (
            CONTROL_DEPARTMENTS,
            BudgetStatus,
            CapacitySnapshot,
            CostSnapshot,
            RecommendedAction,
            TokenBudget,
            assess_budget,
            build_department_scorecard,
        )
    except ModuleNotFoundError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from cost import (  # type: ignore[no-redef]
            CONTROL_DEPARTMENTS,
            BudgetStatus,
            CapacitySnapshot,
            CostSnapshot,
            RecommendedAction,
            TokenBudget,
            assess_budget,
            build_department_scorecard,
        )

    t0 = datetime(2026, 8, 19, tzinfo=timezone.utc)
    t1 = datetime(2026, 8, 26, tzinfo=timezone.utc)

    def capacity(**overrides: Any) -> CapacitySnapshot:
        values: dict[str, Any] = {
            "window_start": t0, "window_end": t1, "arrivals": 120,
            "queue_p95_ms": Decimal("840"), "duration_p95_ms": Decimal("2100"),
            "retry_rate": Decimal("0.02"), "error_rate": Decimal("0"),
            "utilization": Decimal("0.61"),
        }
        values.update(overrides)
        return CapacitySnapshot(**values)

    def cost_snapshot(agent: str = "a1") -> CostSnapshot:
        return CostSnapshot(
            agent_id=agent, profile_version_id="pv1", window_start=t0, window_end=t1,
            input_tokens=1200, output_tokens=800, model_cost=Decimal("3.5"),
            tool_cost=Decimal("0"), infra_cost=Decimal("0"), case_count=12,
        )

    observed = build_department_scorecard(
        department_code="research-department", window_start=t0, window_end=t1,
        capacity=capacity(), cost_snapshots=[cost_snapshot()],
        finding_count=0, rework_rate=Decimal("0.05"),
        quality_references={"eval_run_ids": ["eval-77"], "role_kpi": []},
    )
    # capacity·cost Snapshot 이 전혀 없는 부서. 0 으로 채워지면 안 된다.
    unobserved = build_department_scorecard(
        department_code="risk-management", window_start=t0, window_end=t1,
        capacity=None, cost_snapshots=[], finding_count=None, rework_rate=None,
    )
    brief = build_scorecard_brief([observed, unobserved])

    # 1) 결측과 0 이 다르게 렌더링된다.
    research_capacity = next(l for l in brief.splitlines() if l.startswith("| research-department | OBSERVED"))
    risk_capacity = next(l for l in brief.splitlines() if l.startswith("| risk-management | NO_SNAPSHOT"))
    assert "| 0 |" in research_capacity, research_capacity          # error_rate 0 은 관측된 0
    assert f"| {MISSING} |" not in research_capacity, research_capacity
    assert "| 0 |" not in risk_capacity, risk_capacity              # 스냅샷 없음을 0 으로 채우지 않는다
    assert risk_capacity.count(MISSING) == len(CAPACITY_COLUMNS), risk_capacity
    assert "NO_SNAPSHOT" in brief and "사용량이 0이라는 뜻이 아니다" in brief

    # 2) eval_score 는 값이 아니라 소유자 안내로 나간다.
    assert "eval_score" in brief and "audit.eval_runs" in brief

    # 3) 배열은 셀에 뭉개지 않고 참조 표로 나간다.
    assert "| research-department | eval_run_ids | eval-77 |" in brief, brief
    assert "['eval-77']" not in brief and '["eval-77"]' not in brief

    # 4) 창이 다르면 조용히 한 표에 세우지 않는다.
    shifted = build_department_scorecard(
        department_code="qa-department", window_start=t0,
        window_end=datetime(2026, 8, 25, tzinfo=timezone.utc),
        capacity=capacity(), cost_snapshots=[cost_snapshot()],
    )
    mixed = build_scorecard_brief([observed, shifted])
    assert "부서마다 다르다" in mixed and "## 관측 창" in mixed, mixed
    assert "전 부서 동일" in brief and "## 관측 창" not in brief

    # 5) 판정은 옮기기만 한다 — 통제 부서 초과는 축소 권고가 아니라 CEO Escalation.
    # 통제 부서 코드는 cost.py 소유다 — 여기서 문자열을 복제하면 그쪽이 정본을
    # 고칠 때 이 점검만 조용히 옛 값을 붙들게 된다.
    control_code = sorted(CONTROL_DEPARTMENTS)[0]
    control = assess_budget(
        agent_id="agent-9", employee_code="RISK-01", department_code=control_code,
        budget=TokenBudget(per_case_tokens=100, daily_tokens=1000),
        snapshots=[cost_snapshot()],
    )
    assert control.status is BudgetStatus.EXCEEDED
    assert control.recommended_action is RecommendedAction.ESCALATE_TO_CEO
    with_budget = build_scorecard_brief([observed, unobserved], assessments=[control])
    assert (
        f"| RISK-01 | {md_cell(control_code)} | 2000 | 1000 | 2.000 | EXCEEDED | "
        "ESCALATE_TO_CEO | Y |"
    ) in with_budget, with_budget
    assert "재판정 금지" in with_budget

    # 6) 파이프가 든 값이 표를 깨지 않는다.
    piped = build_department_scorecard(
        department_code="trading-department | 임시", window_start=t0, window_end=t1,
        capacity=None, cost_snapshots=[],
    )
    assert "trading-department \\| 임시" in build_scorecard_brief([piped])

    # 7) CLI 왕복.
    import io
    import tempfile

    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, encoding="utf-8") as handle:
        json.dump({"scorecards": [observed, unobserved]}, handle, ensure_ascii=False)
        temp_path = handle.name
    assert main(["--input", temp_path]) == 0
    Path(temp_path).unlink()
    assert main(["--input", str(Path(temp_path).with_suffix(".missing"))]) == 2

    # 8) 실측 — 같은 내용을 중첩 JSON 으로 줄 때와 문자 수를 비교한다.
    six = [
        build_department_scorecard(
            department_code=code, window_start=t0, window_end=t1,
            capacity=capacity(), cost_snapshots=[cost_snapshot()],
            finding_count=1, rework_rate=Decimal("0.03"),
            quality_references={"eval_run_ids": [f"eval-{code}"], "role_kpi": []},
        )
        # 정본 department_code 는 Hermes Profile 이름이다
        # (supabase/migrations/20260804000300_unify_department_code.sql).
        for code in ("research-department", "trading-department", "risk-management",
                     "quant-backtest-department", "accounting-portfolio-department",
                     "qa-department")
    ]
    as_json = json.dumps(six, ensure_ascii=False, sort_keys=True, default=str)
    as_brief = build_scorecard_brief(six)
    print(f"6개 부서 인코딩 문자 수 — 중첩 JSON {len(as_json)} / 브리프 {len(as_brief)} "
          f"(표 본문만 {sum(len(l) for l in render_capacity_table(six) + render_cost_table(six) + render_quality_table(six))})")
    print("ok - workforce.scorecard_brief.v1 렌더러 점검 통과")
