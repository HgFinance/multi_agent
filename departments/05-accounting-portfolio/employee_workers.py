"""Accounting/Portfolio employee Worker registry: official figures stay deterministic.

두 직원만 근거를 주입받는다 (2026-08-05):

  ledger-reconciliation-worker  Break Triage — 원인 후보와 과거 해소 사례, 그리고
                                Aging/SLA 판정. 판정은 reconciliation.py 가 유지하고
                                여기서는 '왜 났을 법한가'의 근거만 붙는다.
  nav-close-worker              Layered Memory — 과거 마감에서 걸린 것(FinMem 계층).
                                **비공식 보조 자료다.** 수치는 원장에서만 나온다.

둘 다 반환된 `evidence_refs` 를 검증한다. 색인 밖 id 를 인용하면 그 직원 보고는
escalate 된다 — 승인 방향으로 fallback 하지 않는다.

**기억 계층과 System of Record 를 섞지 않는다.** nav-close-worker 의 evidence 에는
`is_official: false` 가 항상 붙고, 이 파일에 그 값을 True 로 만드는 경로가 없다.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

_BASE = Path(__file__).resolve().parent
for _p in (str(_BASE.parents[1]), str(_BASE.parent), str(_BASE),
           str(_BASE / "reconciliation"), str(_BASE / "ledger")):
    if _p not in sys.path:
        sys.path.append(_p)

try:
    from departments.employee_worker_runtime import (
        WorkerLLM,
        WorkerSpec,
        run_worker_registry,
        tools_for_specs,
    )
except ModuleNotFoundError:
    from employee_worker_runtime import (
        WorkerLLM,
        WorkerSpec,
        run_worker_registry,
        tools_for_specs,
    )

from break_triage import (  # noqa: E402 - sys.path 조정 뒤라야 import 된다
    BreakTriageError,
    check_aging,
    triage_context,
)
from nav_close_memory import (  # noqa: E402
    CloseMemoryError,
    close_memory_context,
    recall,
)

WORKER_SPECS = (
    WorkerSpec("portfolio-control-worker", "Portfolio control and position-state analyst", ("accounting.portfolio_snapshot.read",), "always", ("portfolio_snapshot", "positions", "cash")),
    WorkerSpec("ledger-reconciliation-worker", "Ledger, fund-accounting and broker-reconciliation analyst", ("accounting.ledger.read", "accounting.reconciliation.read"), "always", ("ledger_snapshot", "fills", "reconciliation")),
    WorkerSpec("nav-close-worker", "NAV close and official-figure readiness analyst", ("accounting.nav_close.read",), "nav_close", ("nav_snapshot", "open_breaks", "approval_state")),
    WorkerSpec("treasury-liquidity-worker", "Treasury, collateral and liquidity analyst", ("accounting.treasury.read",), "treasury_signal", ("cash", "margin", "collateral")),
    WorkerSpec("pnl-attribution-worker", "PnL and performance attribution analyst", ("accounting.pnl.read",), "pnl_request", ("pnl_snapshot", "fills", "costs")),
    WorkerSpec("investor-reporting-worker", "Investor reporting and disclosure consistency analyst", ("accounting.reporting.read",), "investor_report", ("reporting_snapshot", "pnl_snapshot", "risk_snapshot")),
    WorkerSpec("valuation-corporate-actions-worker", "Valuation and corporate-actions analyst", ("accounting.valuation.read", "accounting.corporate_actions.read"), "corporate_action", ("valuation", "corporate_action")),
    WorkerSpec("fee-accrual-tax-worker", "Fee, expense and tax-accrual consistency analyst", ("accounting.fees_tax.read",), "fee_accrual", ("fees", "expenses", "tax_accruals")),
)

TRIAGE_WORKER = "ledger-reconciliation-worker"
MEMORY_WORKER = "nav-close-worker"
GROUNDED_WORKERS = frozenset({TRIAGE_WORKER, MEMORY_WORKER})


def _triage_tool(base):
    """대사 직원의 evidence 에 원인 후보와 Aging/SLA 를 얹는다.

    payload 의 `reconciliation.triage` 는 마감 파이프라인의 `break_triage` 목록,
    `open_breaks` 는 미종결 Break 행 목록이다. 없으면 없다고 적고 지어내지 않는다.
    """

    def read_context(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        evidence = dict(base(payload))
        recon = payload.get("reconciliation")
        triaged = recon.get("triage") if isinstance(recon, Mapping) else None
        citable: set[str] = set()
        if triaged:
            evidence["break_triage"] = "\n\n".join(triage_context(t) for t in triaged)
            citable = {ref for t in triaged for ref in (t.get("citable_ids") or [])}
        else:
            evidence["break_triage"] = (
                "원인 후보 근거가 없습니다. 원인을 추정해 단정하지 마십시오.")
        try:
            rows = payload.get("open_breaks") or []
            evidence["break_aging"] = (
                check_aging(rows) if rows
                else {"checked": False, "reason": "미종결 Break 목록이 payload 에 없다"})
        except BreakTriageError as exc:
            # 기한을 못 재면 기한 안이라고 말하지 않는다 (개발 원칙 9).
            evidence["break_aging"] = {"checked": False, "sla_breached": None,
                                       "error": type(exc).__name__, "detail": str(exc)}
        evidence["citable_ids"] = sorted(citable)
        return evidence

    return read_context


def _memory_tool(base):
    """마감 직원의 evidence 에 과거 마감 기억을 얹는다. **비공식이다.**"""

    def read_context(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        evidence = dict(base(payload))
        block = payload.get("nav_close")
        query = str(block.get("query") if isinstance(block, Mapping) else "") or "마감 준비"
        try:
            recalled = recall(query)
        except CloseMemoryError as exc:
            recalled = {"recalled": [], "authoritative": False, "is_official": False,
                        "error": type(exc).__name__}
        evidence["close_memory"] = close_memory_context(recalled)
        evidence["citable_ids"] = sorted(m["memory_id"] for m in recalled.get("recalled", []))
        # 기억이 무엇을 말하든 마감 확정 권한은 생기지 않는다.
        evidence["is_official"] = False
        evidence["source_of_record"] = "accounting.* (ledger / portfolio_snapshots / nav_runs)"
        return evidence

    return read_context


def accounting_tools() -> dict[str, Any]:
    tools = dict(tools_for_specs(WORKER_SPECS))
    tools[TRIAGE_WORKER] = _triage_tool(tools[TRIAGE_WORKER])
    tools[MEMORY_WORKER] = _memory_tool(tools[MEMORY_WORKER])
    return tools


def _apply_citation_checks(result: dict[str, Any], tools: Mapping[str, Any],
                           payload: Mapping[str, Any]) -> dict[str, Any]:
    """근거를 받은 직원의 인용을 검증한다. 날조가 있으면 escalate 한다."""
    allowed_by_worker = {
        worker_id: set(tools[worker_id](payload).get("citable_ids") or [])
        for worker_id in GROUNDED_WORKERS if worker_id in tools
    }
    for report in result.get("workers", []):
        worker_id = report.get("worker_id")
        if worker_id not in GROUNDED_WORKERS:
            continue
        output = report.get("output") or {}
        allowed = allowed_by_worker.get(worker_id, set())
        refs = [str(r) for r in (output.get("evidence_refs") or [])
                if str(r).startswith(("case:", "cause:", "mem:"))]
        unknown = sorted({r for r in refs if r not in allowed})
        report["evidence_citations"] = {"refs": refs, "unknown_refs": unknown,
                                        "grounded": bool(refs) and not unknown}
        if unknown:
            output["escalate"] = True
            report["status"] = "DEGRADED"
            result["degraded"] = True
            if worker_id not in result.get("failed", []):
                result.setdefault("failed", []).append(worker_id)
            if worker_id in result.get("executed", []):
                result["executed"].remove(worker_id)
    # 이 계층은 마감을 확정하지 않는다 - 계약으로 박는다.
    result["is_official"] = False
    return result


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None) -> dict[str, Any]:
    tools = accounting_tools()
    result = run_worker_registry(WORKER_SPECS, payload, tools=tools, llm=llm)
    return _apply_citation_checks(result, tools, payload)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    triaged = [{
        "break_id": "b1", "kind": "cash_mismatch", "severity": "high", "escalates": False,
        "similar_cases": [], "cause_candidates": [
            {"id": "cause:unposted_fee_or_tax", "cause": "수수료 분개 미posting",
             "check": "journal_lines 확인"}],
        "citable_ids": ["cause:unposted_fee_or_tax"], "evidence_basis": "taxonomy_only",
    }]
    payload = {
        "ledger_snapshot": {"balanced": True},
        "reconciliation": {"triage": triaged},
        "open_breaks": [{"break_id": "b-old", "severity": "material", "status": "OPEN",
                         "kind": "position_mismatch",
                         "created_at": (now - timedelta(hours=30)).isoformat()}],
        "nav_close": {"query": "브로커 명세서 지연"},
    }

    tools = accounting_tools()

    # 1. 대사 직원이 원인 후보와 Aging 을 먼저 받는다
    recon_evidence = tools["ledger-reconciliation-worker"](payload)
    assert "cause:unposted_fee_or_tax" in recon_evidence["break_triage"]
    assert "수치는 원장에서만" in recon_evidence["break_triage"]
    assert recon_evidence["break_aging"]["sla_breached"] is True
    assert recon_evidence["break_aging"]["by_aging_status"]["OVERDUE"] == ["b-old"]
    assert recon_evidence["citable_ids"] == ["cause:unposted_fee_or_tax"]
    print("  대사 직원 근거 + Aging     OK")

    # 2. 근거가 없으면 없다고 적는다 - 추정하라고 하지 않는다
    bare = tools["ledger-reconciliation-worker"]({"ledger_snapshot": {}})
    assert "추정해 단정하지" in bare["break_triage"]
    assert bare["break_aging"]["checked"] is False
    assert bare["citable_ids"] == []
    print("  근거 부재 표기             OK")

    # 3. **마감 직원 기억은 비공식이다**
    nav_evidence = tools["nav-close-worker"](payload)
    assert nav_evidence["is_official"] is False
    assert nav_evidence["source_of_record"].startswith("accounting.")
    assert "비공식" in nav_evidence["close_memory"] or "기억: 없음" in nav_evidence["close_memory"]
    print("  마감 기억 비공식 표기      OK")

    # 4. 근거를 안 받는 직원은 그대로다
    other = tools["portfolio-control-worker"]({"portfolio_snapshot": {"nav": "1"}})
    assert "break_triage" not in other and "close_memory" not in other
    print("  비대상 직원 불변           OK")

    # 5. 색인 밖 인용은 escalate 된다
    fake = {"workers": [{"worker_id": "ledger-reconciliation-worker", "status": "COMPLETED",
                         "output": {"summary": "s", "evidence_refs": ["case:made_up"],
                                    "escalate": False}}],
            "executed": ["ledger-reconciliation-worker"], "failed": [], "degraded": False}
    checked = _apply_citation_checks(fake, tools, payload)
    assert checked["workers"][0]["evidence_citations"]["unknown_refs"] == ["case:made_up"]
    assert checked["workers"][0]["output"]["escalate"] is True
    assert checked["degraded"] is True and checked["executed"] == []
    assert checked["is_official"] is False

    good = {"workers": [{"worker_id": "ledger-reconciliation-worker", "status": "COMPLETED",
                         "output": {"summary": "s",
                                    "evidence_refs": ["cause:unposted_fee_or_tax"],
                                    "escalate": False}}],
            "executed": ["ledger-reconciliation-worker"], "failed": [], "degraded": False}
    ok = _apply_citation_checks(good, tools, payload)
    assert ok["workers"][0]["evidence_citations"]["grounded"] is True
    assert ok["degraded"] is False
    print("  인용 날조 -> escalate      OK")

    # 6. 직원 계층은 어떤 경우에도 마감을 확정하지 않는다
    assert _apply_citation_checks({"workers": []}, tools, payload)["is_official"] is False
    print("  is_official 항상 False     OK")

    print("ok - 회계 직원 근거 주입 6개 영역 점검 통과 "
          "(대사=Triage/Aging, 마감=Layered Memory, 둘 다 비공식)")
