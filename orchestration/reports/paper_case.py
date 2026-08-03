"""Build a deterministic, non-binding paper investment case report.

The report deliberately separates three facts that are easy to conflate:

* Hermes smoke success proves profile invocation and contract routing only.
* A paper forecast is an illustrative scenario and is not a market signal.
* A CEO paper decision is a final case disposition, not permission to submit
  an order, approve a Risk limit, or write the ledger.

No department ``scripts.py`` is imported here. This module is an orchestration
boundary and report projection only.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from orchestration.workflows.contracts import StepRun, WorkflowRun

REALTIME_STAGE_DEFINITIONS: tuple[tuple[str, str, str, str, str], ...] = (
    ("research", "research-department", "case_request", "research_packet", "HOLD"),
    ("trading", "trading-department", "research_packet", "order_intent", "HOLD"),
    ("risk", "risk-management", "order_intent", "risk_decision", "REJECT"),
    ("qa", "qa-department", "risk_decision", "qa_assessment", "ESCALATE"),
    ("oms-fill-gate", "trading-department", "qa_assessment", "execution_result", "HOLD"),
    (
        "accounting",
        "accounting-portfolio-department",
        "execution_result",
        "accounting_snapshot",
        "BREAK",
    ),
    ("ceo", "ceo-agent", "accounting_snapshot", "ceo_case_summary", "ESCALATE"),
)

DEFAULT_ILLUSTRATIVE_FORECAST: dict[str, Decimal] = {
    "up": Decimal("0.45"),
    "sideways": Decimal("0.30"),
    "down": Decimal("0.25"),
}


class PaperCaseError(ValueError):
    """Raised when a paper case does not satisfy the boundary contract."""


@dataclass(frozen=True)
class PaperCaseInput:
    """Input contract for a non-mutating paper case."""

    case_id: str
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: Decimal
    stage: str = "paper"

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> PaperCaseInput:
        try:
            limit_price = Decimal(str(raw["limit_price"]))
        except (KeyError, InvalidOperation, ValueError) as exc:
            raise PaperCaseError("limit_price must be a positive decimal") from exc

        try:
            quantity = int(raw["quantity"])
        except (KeyError, TypeError, ValueError) as exc:
            raise PaperCaseError("quantity must be a positive integer") from exc

        case = cls(
            case_id=str(raw.get("case_id", "paper-case")),
            symbol=str(raw.get("symbol", "")),
            side=str(raw.get("side", "")).upper(),
            quantity=quantity,
            order_type=str(raw.get("order_type", "")).upper(),
            limit_price=limit_price,
            stage=str(raw.get("stage", "paper")).lower(),
        )
        case.validate()
        return case

    def validate(self) -> None:
        if not self.case_id.strip():
            raise PaperCaseError("case_id is required")
        if not self.symbol.strip():
            raise PaperCaseError("symbol is required")
        if self.side not in {"BUY", "SELL"}:
            raise PaperCaseError("side must be BUY or SELL")
        if self.quantity <= 0:
            raise PaperCaseError("quantity must be positive")
        if self.order_type != "LIMIT":
            raise PaperCaseError("paper report currently accepts LIMIT only")
        if self.limit_price <= 0:
            raise PaperCaseError("limit_price must be positive")
        if self.stage != "paper":
            raise PaperCaseError("paper report cannot be used for live stage")


@dataclass(frozen=True)
class PaperStageReport:
    stage_id: str
    owner: str
    status: str
    input_contract: str
    output_contract: str
    failure_action: str
    evidence: str
    binding: bool = False


@dataclass(frozen=True)
class PaperCaseReport:
    case: PaperCaseInput
    run_id: str
    mode: str
    workflow_status: str
    final_decision: str
    decision_reason: str
    stages: tuple[PaperStageReport, ...]
    forecast: Mapping[str, Decimal]
    generated_at: str
    runtime_error: str | None = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def report_status(self) -> str:
        complete = bool(self.stages) and all(
            stage.status in {"PAPER_SMOKE_PASS", "PAPER_DOMAIN_PASS"}
            for stage in self.stages
        )
        if self.workflow_status != "COMPLETED" or not complete:
            return "INCONCLUSIVE"
        return "PAPER_EXECUTED" if self.mode == "paper" else "PAPER_CONNECTED"

    def _ceo_metadata(self) -> str:
        decision = self.metadata.get("ceo_decision")
        if not isinstance(decision, Mapping):
            return "not_available"
        runtime = decision.get("runtime")
        if isinstance(runtime, Mapping):
            return f"{runtime.get('profile', 'ceo-agent')} / {runtime.get('model', 'unknown')} / {runtime.get('call_status', 'unknown')}"
        return str(decision.get("binding_decision", "not_available"))

    def to_markdown(self) -> str:
        forecast_rows = "\n".join(
            f"| T+5 {label} | {value:.2%} |"
            for label, value in self.forecast.items()
        )
        stage_rows = "\n".join(
            "| {stage_id} | {owner} | {status} | `{input_contract}` → "
            "`{output_contract}` | {binding} | {failure_action} | {evidence} |".format(
                stage_id=stage.stage_id,
                owner=stage.owner,
                status=stage.status,
                input_contract=stage.input_contract,
                output_contract=stage.output_contract,
                binding="Yes" if stage.binding else "No",
                failure_action=stage.failure_action,
                evidence=stage.evidence.replace("|", "\\|"),
            )
            for stage in self.stages
        )
        runtime_error = self.runtime_error or "없음"
        return f"""# Paper Investment Case Report

> 이 문서는 주문·브로커·원장·DB를 변경하지 않는 `paper-e2e` 또는 `paper` 결과다.
> 시장 데이터 기반 투자 자문이나 실거래 승인으로 사용할 수 없다.

## 1. CEO 요약

| 항목 | 값 |
|---|---|
| Case ID | `{self.case.case_id}` |
| Pipeline | `{self.report_status}` |
| Workflow run | `{self.run_id}` |
| Execution mode | `{self.mode}` |
| Workflow status | `{self.workflow_status}` |
| Binding decision | **`{self.final_decision}`** |
| Binding | `False` |
| Generated at | `{self.generated_at}` |

### CEO 최종 페이퍼 판정

**{self.final_decision}**

{self.decision_reason}

CEO adapter: `{self._ceo_metadata()}`

CEO는 모든 부서 결과를 종합해 이 Case의 페이퍼 판정을 내릴 수 있지만,
Risk 한도 승인·주문 제출·원장 수정·NAV 확정 권한을 갖지 않는다. 실거래 전환은
별도의 결정론적 Risk/QA/OMS 및 승인된 production adapter가 필요하다.

## 2. 입력 Case

| 필드 | 값 |
|---|---|
| Symbol | `{self.case.symbol}` |
| Side | `{self.case.side}` |
| Quantity | `{self.case.quantity}` |
| Order type | `{self.case.order_type}` |
| Limit price | `{self.case.limit_price:.2f}` |
| Stage | `{self.case.stage}` |

## 3. Paper 예측 — 예시 시나리오

> 아래 확률은 외부 시세·포트폴리오·정책 Corpus를 조회해 산출한 값이 아니다.
> 연결 검증을 위한 고정 illustrative baseline이며, CEO 판정이나 주문 수량에 사용하지 않았다.

| Horizon / outcome | Probability |
|---|---:|
{forecast_rows}

Prediction status: `SIMULATION_ONLY`  
Prediction binding: `False`  
Prediction action: `HOLD` — 실제 Snapshot과 근거가 없으므로 진입 신호로 승격하지 않음

## 4. 전체 부서 종합

| Step | Hermes/Profile | Status | Contract handoff | Binding | Failure action | Evidence |
|---|---|---|---|---|---|---|
{stage_rows}

`PAPER_SMOKE_PASS`는 프로필 호출과 계약 경계만 통과했다는 뜻이다.
`PAPER_DOMAIN_PASS`는 Research/Risk/QA 부서 진입점과 CEO 종합 adapter가
실행됐다는 뜻이다. 어느 경우에도 Broker 제출, 체결 확정, Ledger posting을 의미하지 않는다.

## 5. Production adapter 승인 기준

실제 운영 adapter는 다음 순서를 모두 통과해야 한다. 한 단계라도 실패하면 자동 승격하지 않고
`HOLD` 또는 `ESCALATE`한다.

1. **Adapter manifest 고정:** 소유 팀, 버전/commit, 입력·출력 계약, 허용 도구, 금지 부작용을 등록한다.
2. **QA 독립 검증:** schema/contract, replay, idempotency, timeout/retry, 로그·trace·PII 마스킹을 검증한다.
3. **Risk 승인:** 실제 Portfolio/Market Snapshot, limit, Stress/VaR/Greeks, Kill Switch와 fail-closed를 검증한다.
4. **Paper acceptance:** 주문 제출 없이 전체 handoff와 예상 결과를 재현하고, 실패 주입 시 HOLD/REJECT/ESCALATE를 확인한다.
5. **운영 승인:** CEO/권한 있는 운영자가 승인 범위·유효기간·rollback owner를 명시한 approval record를 만든다.
6. **Production gate:** IAM이 승인된 immutable artifact만 배포하고, shadow/canary 후에만 live 권한을 별도로 부여한다.

필수 승인 증거: `adapter_version`, `artifact_digest`, `qa_run_id`, `risk_run_id`,
`replay_hash`, `approval_id`, `approved_scope`, `expires_at`, `rollback_plan`.

## 6. 안전성·한계

- Broker order, Paper Broker fill, Ledger posting, Supabase/Redis/Notion write: **수행하지 않음**
- 실제 시장 데이터·계좌 잔고·정책 원문: **이 보고서에는 없음**
- Hermes smoke/runtime 오류: `{runtime_error}`
- 최종 바인딩 상태: **{self.final_decision}**

따라서 이 결과는 “연결이 잘 되었는가”에 대한 페이퍼 검증으로는 유효하지만,
“투자해도 되는가”에 대한 승인 결과는 아니다.
"""


def build_paper_case_report(
    case: PaperCaseInput,
    *,
    workflow_run: WorkflowRun | None = None,
    run_id: str = "paper-report-unbound",
    runtime_error: str | None = None,
    generated_at: datetime | None = None,
    forecast: Mapping[str, Decimal] | None = None,
) -> PaperCaseReport:
    """Combine all realtime stages into one CEO-facing paper report."""

    case.validate()
    steps_by_id: dict[str, StepRun] = {}
    workflow_status = "NOT_EXECUTED"
    workflow_mode = "paper-e2e"
    workflow_metadata: Mapping[str, object] = {}
    if workflow_run is not None:
        run_id = workflow_run.run_id
        workflow_mode = workflow_run.mode
        workflow_status = workflow_run.status
        workflow_metadata = workflow_run.metadata
        steps_by_id = {step.step_id: step for step in workflow_run.steps}

    stages: list[PaperStageReport] = []
    previous_blocked = False
    for stage_id, owner, input_contract, output_contract, failure_action in REALTIME_STAGE_DEFINITIONS:
        step = steps_by_id.get(stage_id)
        if step is None:
            status = "SKIPPED_SAFE" if previous_blocked else "NOT_EXECUTED"
            evidence = "이전 단계 미완료로 안전하게 건너뜀" if previous_blocked else "실행 증거 없음"
            previous_blocked = True
        elif step.status == "DISPATCHED":
            status = (
                "PAPER_DOMAIN_DEGRADED"
                if workflow_mode == "paper" and "status=DEGRADED" in (step.detail or "")
                else "PAPER_DOMAIN_PASS"
                if workflow_mode == "paper"
                else "PAPER_SMOKE_PASS"
            )
            evidence = step.detail or "Hermes smoke 통과"
        elif step.status in {"FAILED", "BLOCKED"}:
            status = "BLOCKED_SAFE"
            evidence = step.detail or f"{step.status}"
            previous_blocked = True
        else:
            status = step.status
            evidence = step.detail or "workflow 기록"
        stages.append(
            PaperStageReport(
                stage_id=stage_id,
                owner=owner,
                status=status,
                input_contract=input_contract,
                output_contract=output_contract,
                failure_action=failure_action,
                evidence=evidence,
            )
        )

    all_paper_steps_passed = bool(stages) and all(
        stage.status in {"PAPER_SMOKE_PASS", "PAPER_DOMAIN_PASS"}
        for stage in stages
    )
    if all_paper_steps_passed and workflow_status == "COMPLETED":
        if workflow_mode == "paper":
            reason = (
                "Research/Risk/QA 실행 결과와 CEO Luna 종합까지 연결됐다. 그러나 "
                "Paper adapter는 주문 제출·체결·원장 반영을 하지 않고, 결과도 바인딩 "
                "승인이 아니므로 실거래 승격 근거가 없다."
            )
        else:
            reason = (
                "7개 Hermes Profile smoke와 handoff 계약은 통과했다. 그러나 이 실행은 "
                "실제 직원 작업·시장 Snapshot·결정론적 Risk/QA 결과·OMS/Fill·원장 반영을 "
                "수행하지 않았으므로 실거래 승격 근거가 없다."
            )
    else:
        reason = (
            "전체 부서 결과가 완료되지 않았거나 실행 증거가 부족하다. 누락된 근거를 "
            "재현 가능한 run으로 보강하기 전까지 신규 진입을 차단한다."
        )

    return PaperCaseReport(
        case=case,
        run_id=run_id,
        mode=workflow_mode,
        workflow_status=workflow_status,
        final_decision="HOLD / ESCALATE",
        decision_reason=reason,
        stages=tuple(stages),
        forecast=forecast or DEFAULT_ILLUSTRATIVE_FORECAST,
        generated_at=(generated_at or datetime.now(timezone.utc)).isoformat(),
        runtime_error=runtime_error,
        metadata=workflow_metadata,
    )


def write_paper_case_report(report: PaperCaseReport, output_path: Path) -> Path:
    """Write only a local Markdown projection and return its path."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report.to_markdown(), encoding="utf-8")
    return output_path


def _run_from_json(raw: Mapping[str, Any]) -> WorkflowRun:
    """Parse the stable JSON shape emitted by the workflow runner."""

    from orchestration.workflows.contracts import StepRun

    steps = tuple(StepRun(**step) for step in raw.get("steps", []))
    return WorkflowRun(
        run_id=str(raw.get("run_id", "paper-json-run")),
        workflow=str(raw.get("workflow", "investment-case")),
        mode=str(raw.get("mode", "paper-e2e")),
        status=str(raw.get("status", "UNKNOWN")),
        safe_action=raw.get("safe_action"),
        steps=steps,
        metadata=raw.get("metadata", {}),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Write a non-binding CEO paper case report")
    parser.add_argument("--run-json", type=Path, help="workflow runner JSON output")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--case-id", default="paper-case-aapl-001")
    parser.add_argument("--symbol", default="AAPL")
    parser.add_argument("--side", default="BUY")
    parser.add_argument("--quantity", type=int, default=100)
    parser.add_argument("--order-type", default="LIMIT")
    parser.add_argument("--limit-price", default="200.00")
    parser.add_argument("--runtime-error")
    args = parser.parse_args()

    case = PaperCaseInput.from_mapping(
        {
            "case_id": args.case_id,
            "symbol": args.symbol,
            "side": args.side,
            "quantity": args.quantity,
            "order_type": args.order_type,
            "limit_price": args.limit_price,
            "stage": "paper",
        }
    )
    workflow_run = None
    if args.run_json:
        workflow_run = _run_from_json(
            json.loads(args.run_json.read_text(encoding="utf-8"))
        )
    report = build_paper_case_report(
        case,
        workflow_run=workflow_run,
        runtime_error=args.runtime_error,
    )
    output = args.output or Path("orchestration/reports") / f"paper_case_report_{case.case_id}.md"
    path = write_paper_case_report(report, output)
    print(path)
    print(f"CEO paper decision: {report.final_decision}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
