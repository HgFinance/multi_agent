"""Accounting/Portfolio employee Worker registry: official figures stay deterministic.

직원 2명. **그중 LLM 을 쓰는 것은 1명뿐이다**(2026-08-07).

  exception-investigation-worker  예외 조사와 설명 — LLM
  back-office-runner              백오피스 잡무 — **LLM 없음.** 결정론 모듈 출력을 옮긴다

기존 8명(portfolio-control / ledger-reconciliation / nav-close / treasury-liquidity /
pnl-attribution / investor-reporting / valuation-corporate-actions / fee-accrual-tax)을
LLM 1 + 잡무 1 로 줄였다. 근거는 부서 헌장이다.

  마스터플랜 19.12  "공식 숫자는 Accounting Engine 이 계산하며
                     **Agent 는 예외 조사와 설명을 담당한다**"
  마스터플랜 19.16  "모든 숫자는 공식 Reporting API 에서만 가져오며
                     Agent 가 수치를 계산하거나 수정하지 않는다"
  SOUL.md           "You use only figures the Accounting Engine has confirmed"

**기존 8명은 축이 틀려 있었다.** portfolio / treasury / pnl / reporting / valuation /
fee-tax 로 나뉘어 있었는데 그 이름의 뜻은 "그 도메인의 *수치* 를 보는 분석가"다.
수치는 헌장상 처음부터 에이전트 것이 아니므로, **에이전트에게 없는 권한을 축으로
직원을 나눈 것**이었다. 도메인마다 qwen3:1.7b 를 하나씩 얹으면 각자 자기 도메인
수치를 한국어 문장으로 옮기는데, 그게 정확히 CLAUDE.local.md 원칙 5 위반 경로다.

헌장이 쓰는 축은 도메인이 아니라 **예외 종류**이고, 조사가 필요한 예외는 둘 다
"차이가 났는데 원인이 확정되지 않았다"로 같은 모양이다:

  19.11 Middle Office     거래 사실이 안 맞는다 (Break) -> break_triage.py
  19.12 Fund Accounting   수치가 설명이 안 된다 (항등식 잔차) -> accounting_ops.pnl_exception

방법이 같으므로(원인 후보 -> 근거 대조 -> 인용 검증 -> fail-closed) 직원 하나가
근거 provider 셋을 물고 처리한다. `nav_close_memory` 는 별도 직원이 아니라 이
직원의 근거다 — 지난 마감에서 뭐가 걸렸는지가 이번 조사의 재료다.

**Bull/Bear 같은 대립쌍은 만들지 않는다.** 트레이딩이 둘로 쪼갠 이유는 "논지가
틀렸다"고 말해 줄 결정론 모듈이 없어서였다(ADR-0005). 회계엔 있다 — `check_aging()`
이 Break 을 조용히 늙지 못하게 하고, `DailyReport.unexplained_pnl` 이 잔차를 0 으로
반올림하지 않고, `portfolio.py` 가 Mark 없으면 NAV 를 거부한다. 반대편 압력이 이미
코드이고 LLM 상대역보다 강하다. SOUL 이 금지한 "발견한 Break 을 스스로 immaterial 로
닫는 것"은 상대역이 아니라 **권한을 안 주는 것**으로 막는다 — 이 파일에 Severity 를
바꾸거나 `is_official` 을 True 로 만드는 경로가 없다.

자체 점검: python departments/05-accounting-portfolio/employee_workers.py
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
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

import yaml  # noqa: E402

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

OPS_PATH = _BASE / "accounting_ops.yaml"

# LLM 직원. 하나뿐이다 - 나머지는 back-office-runner 로 합쳐졌다.
INVESTIGATOR = "exception-investigation-worker"

WORKER_SPECS = (
    WorkerSpec(
        INVESTIGATOR,
        "Accounting exception investigator — reconciliation Breaks, unexplained PnL and close readiness",
        ("accounting.ledger.read", "accounting.reconciliation.read", "accounting.nav_close.read"),
        "always",
        ("ledger_snapshot", "fills", "reconciliation", "daily_report", "nav_close"),
    ),
)

GROUNDED_WORKERS = frozenset({INVESTIGATOR})

# 개명 전 id. config 의 legacy_personalities_are_aliases 와 짝이며 감사 추적용이다.
# 조사 축(예외)이 아니라 도메인 축으로 붙어 있던 이름이라 바꿨다 - 위 docstring 참고.
LEGACY_ALIASES = {
    "ledger-reconciliation-worker": INVESTIGATOR,
    "nav-close-worker": INVESTIGATOR,
    "pnl-attribution-worker": INVESTIGATOR,
}

# 인용 가능한 근거 id 의 접두사. 이 접두사로 시작하는 ref 만 검증 대상이다.
CITATION_PREFIXES = ("case:", "cause:", "mem:")

# ── back-office-runner: 백오피스 잡무 (LLM 없음) ───────────────────────────
RUNNER_ID = "back-office-runner"
RUNNER_ROLE = "Back-office runner — 결정론 잡무(포지션·자금·손익·보고·평가·수수료 조회)"

# 흡수한 7명의 도구 허용목록 합집합. 감사에서 "이 직원이 무엇을 읽었나"가 남아야 한다.
RUNNER_TOOLS = (
    "accounting.portfolio_snapshot.read",
    "accounting.nav_close.read",
    "accounting.treasury.read",
    "accounting.pnl.read",
    "accounting.reporting.read",
    "accounting.valuation.read",
    "accounting.corporate_actions.read",
    "accounting.fees_tax.read",
)

# 강등된 직원 -> 그 답을 원래 만들던 결정론 모듈. 이 표가 강등의 근거다.
# 값이 미구현인 항목은 그렇게 적는다 - 있는 척하지 않는다.
RUNNER_ABSORBED = {
    "portfolio-control-worker": "ledger/ledger.py (rebuild) + portfolio/portfolio.py (value_portfolio)",
    "treasury-liquidity-worker": "미구현 - Treasury 모듈 없음(마스터플랜 19.13)",
    "investor-reporting-worker": "reporting/daily_report.py (F23 build_daily_report)",
    "valuation-corporate-actions-worker": "corporate_actions/corporate_actions.py (F25)",
    "fee-accrual-tax-worker": "미구현 - 발생주의 Fee/Tax 도메인 not_started",
}

# 사무원이 payload 에서 옮겨오는 블록. **없으면 없다고 적고 지어내지 않는다.**
RUNNER_BLOCKS = {
    "portfolio": ("portfolio_snapshot", "positions", "cash"),
    "treasury": ("margin", "collateral"),
    "pnl": ("daily_report", "costs"),
    "reporting": ("reporting_snapshot", "risk_snapshot"),
    "valuation": ("valuation", "corporate_action"),
    "fees_tax": ("fees", "expenses", "tax_accruals"),
}


def load_ops(path: Path = OPS_PATH) -> dict[str, Any]:
    """운영 파라미터. 튜닝 대상 숫자는 코드가 아니라 YAML 에 있다."""
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


# ── 근거 provider 3종 ──────────────────────────────────────────────────────
# 셋 다 같은 규율이다: 근거가 없으면 **없다고 적고 추정을 권하지 않는다.**

def _break_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """19.11 Middle Office — 대사 Break 의 원인 후보와 Aging/SLA.

    판정은 `reconciliation.py` 가 유지한다. 여기서는 '왜 났을 법한가'의 근거만 붙는다.
    """
    evidence: dict[str, Any] = {}
    recon = payload.get("reconciliation")
    triaged = recon.get("triage") if isinstance(recon, Mapping) else None
    citable: set[str] = set()
    if triaged:
        evidence["break_triage"] = "\n\n".join(triage_context(t) for t in triaged)
        citable = {ref for t in triaged for ref in (t.get("citable_ids") or [])}
    else:
        evidence["break_triage"] = "원인 후보 근거가 없습니다. 원인을 추정해 단정하지 마십시오."
    try:
        rows = payload.get("open_breaks") or []
        evidence["break_aging"] = (
            check_aging(rows) if rows
            else {"checked": False, "reason": "미종결 Break 목록이 payload 에 없다"})
    except BreakTriageError as exc:
        # 기한을 못 재면 기한 안이라고 말하지 않는다 (개발 원칙 9).
        evidence["break_aging"] = {"checked": False, "sla_breached": None,
                                   "error": type(exc).__name__, "detail": str(exc)}
    evidence["_citable"] = citable
    return evidence


def _pnl_evidence(payload: Mapping[str, Any], ops: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """19.12 Fund Accounting — NAV 항등식 잔차와 그 원인 후보.

    `DailyReport.unexplained_pnl` 이 0 이 아니면 원장·평가·자본 유출입 중 어딘가가
    어긋난 것이다. **잔차를 0 으로 반올림하지 않는다**(SOUL 27행) — 여기서도
    tolerance 는 *조사 생략* 기준일 뿐이고 값 자체는 그대로 보고된다.
    """
    settings = (ops or load_ops()).get("pnl_exception") or {}
    candidates = settings.get("cause_candidates") or []
    report = payload.get("daily_report")
    if not isinstance(report, Mapping) or "unexplained_pnl" not in report:
        return {"pnl_exception": "미설명 손익 근거가 없습니다. 항등식 잔차를 추정하지 마십시오.",
                "_citable": set()}

    try:
        residual = Decimal(str(report["unexplained_pnl"]))
    except (InvalidOperation, TypeError, ValueError):
        # 파싱 못 한 잔차를 0 으로 보지 않는다 - 모르면 모른다고 올린다.
        return {"pnl_exception": (
            f"미설명 손익 값을 해석할 수 없습니다: {report['unexplained_pnl']!r}. "
            "0 으로 간주하지 마십시오."), "_citable": set()}

    try:
        tolerance = Decimal(str(settings.get("residual_tolerance", 0)))
    except (InvalidOperation, TypeError, ValueError):
        tolerance = Decimal(0)

    if abs(residual) <= tolerance:
        return {"pnl_exception": (
            f"미설명 손익 {residual} (허용 잔차 {tolerance} 이내). 조사 대상 아님. "
            "값은 반올림되지 않고 그대로 보고됩니다."), "_citable": set()}

    lines = [f"미설명 손익 {residual} 이 허용 잔차 {tolerance} 를 넘습니다.",
             "NAV 변화 = 총손익 - 비용 + 자본 유출입 항등식이 어긋났습니다.",
             "수치는 원장에서만 나옵니다. 아래는 원인 후보이며 확정이 아닙니다.", ""]
    lines += [f"- [{c['id']}] {c['cause']}\n  확인: {c['check']}" for c in candidates]
    return {"pnl_exception": "\n".join(lines),
            "_citable": {str(c["id"]) for c in candidates}}


def _memory_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """마감 기억(FinMem 계층). **비공식 보조 자료다.**

    별도 직원이 아니라 조사 재료다 - 지난 마감에서 뭐가 걸렸는지가 이번 조사의 절반이다.
    """
    block = payload.get("nav_close")
    query = str(block.get("query") if isinstance(block, Mapping) else "") or "마감 준비"
    try:
        recalled = recall(query)
    except CloseMemoryError as exc:
        recalled = {"recalled": [], "authoritative": False, "is_official": False,
                    "error": type(exc).__name__}
    return {"close_memory": close_memory_context(recalled),
            "_citable": {m["memory_id"] for m in recalled.get("recalled", [])}}


_PROVIDERS = (_break_evidence, _pnl_evidence, _memory_evidence)


def _investigator_tool(base):
    """조사관의 evidence 에 예외 근거 3종을 얹는다.

    provider 하나가 죽어도 나머지는 남긴다 - 근거 하나가 없다고 조사를 통째로
    포기하면 그게 더 위험하다. 대신 실패는 `evidence_errors` 로 드러낸다.
    """

    def read_context(payload: Mapping[str, Any]) -> Mapping[str, Any]:
        evidence = dict(base(payload))
        citable: set[str] = set()
        errors: list[str] = []
        for provider in _PROVIDERS:
            try:
                produced = dict(provider(payload))
            except Exception as exc:  # noqa: BLE001 - 근거 하나가 죽어도 나머지는 남긴다
                errors.append(f"{provider.__name__}:{type(exc).__name__}")
                continue
            citable |= produced.pop("_citable", set())
            evidence.update(produced)
        evidence["citable_ids"] = sorted(citable)
        evidence["evidence_errors"] = errors
        # 기억이 무엇을 말하든, 잔차가 얼마든 마감 확정 권한은 생기지 않는다.
        evidence["is_official"] = False
        evidence["source_of_record"] = "accounting.* (ledger / portfolio_snapshots / nav_runs)"
        return evidence

    return read_context


def back_office_runner(payload: Mapping[str, Any]) -> dict[str, Any]:
    """백오피스 잡무 담당. **모델을 부르지 않는다.**

    합쳐진 5명이 하던 일을 그대로 한다. 다만 서술을 만들지 않고 결정론 모듈이
    확정한 값을 그대로 옮긴다 — `summary` 필드가 없는 것이 이 직원의 요지다.
    헌장 19.16 "Agent 가 수치를 계산하거나 수정하지 않는다"를 계층으로 만든 것이다.
    """
    facts: dict[str, Any] = {}
    missing: list[str] = []
    for block, fields in RUNNER_BLOCKS.items():
        present = {f: payload[f] for f in fields if f in payload and payload[f] is not None}
        if present:
            facts[block] = present
        else:
            # 없는 것을 없다고 적는다. 빈 dict 로 두면 "확인했고 비어 있다"로 읽힌다.
            missing.append(block)

    return {
        "worker_id": RUNNER_ID,
        "role": RUNNER_ROLE,
        "tools": list(RUNNER_TOOLS),
        "status": "COMPLETED",
        "attempts": 1,
        "llm": False,               # ← 이 직원은 모델을 안 부른다. 계약으로 박는다
        "output": {
            "worker_id": RUNNER_ID,
            "facts": facts,
            "missing_blocks": missing,
            "absorbed": dict(RUNNER_ABSORBED),
            "decided_by": "deterministic",
            "is_official": False,     # 확정은 Accounting Engine + 승인 절차가 한다
            "authoritative": False,
        },
        "error": None,
        "output_contract": "accounting.back-office-runner.v1",
    }


def accounting_tools() -> dict[str, Any]:
    """LLM 직원별 read-only tool. 근거는 `_investigator_tool` 이 얹는다."""
    tools = dict(tools_for_specs(WORKER_SPECS))
    tools[INVESTIGATOR] = _investigator_tool(tools[INVESTIGATOR])
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
                if str(r).startswith(CITATION_PREFIXES)]
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
    """직원 레지스트리 진입점. **회계본부에 들어오는 유일한 문이다.**"""
    tools = accounting_tools()
    # stage 는 event name 이 쓰는 이름 공간이다 - 부서 키(accounting-portfolio)가 아니라 accounting.
    result = run_worker_registry(WORKER_SPECS, payload, tools=tools, llm=llm, stage="accounting")
    result = _apply_citation_checks(result, tools, payload)

    # back-office-runner 는 레지스트리 밖이다. 공용 런타임은 그래프마다 LLM 을
    # 부르므로 거기 넣으면 "LLM 없음"이 프롬프트 문장이 되고 실행 경로로는 뚫린다.
    result["workers"].append(back_office_runner(payload))
    result["executed"].append(RUNNER_ID)
    result["llm_worker_count"] = len(WORKER_SPECS)
    return result


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    import json
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
        "daily_report": {"unexplained_pnl": "-42.50"},
        "portfolio_snapshot": {"nav": "1000000"},
    }

    tools = accounting_tools()
    evidence = tools[INVESTIGATOR](payload)

    # 1. LLM 직원은 조사관 하나뿐이고 나머지는 back-office-runner 로 합쳐졌다
    ids = [s.worker_id for s in WORKER_SPECS]
    assert ids == [INVESTIGATOR], ids
    for gone in ("portfolio-control-worker", "ledger-reconciliation-worker",
                 "nav-close-worker", "treasury-liquidity-worker", "pnl-attribution-worker",
                 "investor-reporting-worker", "valuation-corporate-actions-worker",
                 "fee-accrual-tax-worker"):
        assert gone not in ids, f"합쳐진 직원이 남아 있다: {gone}"
    assert all(s.trigger == "always" for s in WORKER_SPECS), "조건부 LLM 직원이 남았다"
    assert set(LEGACY_ALIASES.values()) == {INVESTIGATOR}
    print("  LLM 직원 1 + 잡무 1        OK")

    # 2. 19.11 Break 근거 + Aging (결정론 SLA 가 조용히 늙는 Break 을 막는다)
    assert "cause:unposted_fee_or_tax" in evidence["break_triage"]
    assert "수치는 원장에서만" in evidence["break_triage"]
    assert evidence["break_aging"]["sla_breached"] is True
    assert evidence["break_aging"]["by_aging_status"]["OVERDUE"] == ["b-old"]
    print("  Break 근거 + Aging         OK")

    # 3. 19.12 항등식 잔차 - 후보를 주되 확정하지 않는다
    assert "-42.50" in evidence["pnl_exception"]
    assert "확정이 아닙니다" in evidence["pnl_exception"]
    assert "cause:unposted_journal" in evidence["citable_ids"]
    # 허용 잔차 안이면 조사 대상이 아니지만 **값은 반올림되지 않는다**
    small = _pnl_evidence({"daily_report": {"unexplained_pnl": "0.4"}})
    assert "조사 대상 아님" in small["pnl_exception"]
    assert "반올림되지 않고" in small["pnl_exception"] and "0.4" in small["pnl_exception"]
    # 해석 못 한 잔차를 0 으로 보지 않는다
    bad = _pnl_evidence({"daily_report": {"unexplained_pnl": "n/a"}})
    assert "0 으로 간주하지 마십시오" in bad["pnl_exception"] and bad["_citable"] == set()
    print("  항등식 잔차 조사           OK")

    # 4. 마감 기억은 비공식이고, 어떤 회상 뒤에도 is_official 이 붙지 않는다
    assert evidence["is_official"] is False
    assert evidence["source_of_record"].startswith("accounting.")
    assert "비공식" in evidence["close_memory"] or "기억: 없음" in evidence["close_memory"]
    print("  마감 기억 비공식 표기      OK")

    # 5. 근거가 없으면 없다고 적는다 - 추정하라고 하지 않는다
    bare = tools[INVESTIGATOR]({"ledger_snapshot": {}})
    assert "추정해 단정하지" in bare["break_triage"]
    assert "추정하지 마십시오" in bare["pnl_exception"]
    assert bare["break_aging"]["checked"] is False
    assert bare["citable_ids"] == [] and bare["evidence_errors"] == []
    print("  근거 부재 표기             OK")

    # 6. **back-office-runner 는 모델을 안 부르고 없는 블록을 지어내지 않는다**
    runner = back_office_runner(payload)
    assert runner["llm"] is False and "summary" not in runner["output"]
    assert runner["output"]["facts"]["portfolio"]["portfolio_snapshot"]["nav"] == "1000000"
    assert runner["output"]["facts"]["pnl"]["daily_report"]["unexplained_pnl"] == "-42.50"
    # Treasury·Fee/Tax 는 도메인 자체가 미구현이다 - 빈 값이 아니라 '없음'으로 나온다
    assert "treasury" in runner["output"]["missing_blocks"]
    assert "fees_tax" in runner["output"]["missing_blocks"]
    assert "미구현" in runner["output"]["absorbed"]["treasury-liquidity-worker"]
    assert runner["output"]["decided_by"] == "deterministic"
    assert runner["output"]["is_official"] is False
    assert RUNNER_ID not in {s.worker_id for s in WORKER_SPECS}
    print("  잡무 결정론 산출 + 미구현  OK")

    # 7. 색인 밖 인용은 escalate 된다 (승인 방향 fallback 없음)
    fake = {"workers": [{"worker_id": INVESTIGATOR, "status": "COMPLETED",
                         "output": {"summary": "s", "evidence_refs": ["case:made_up"],
                                    "escalate": False}}],
            "executed": [INVESTIGATOR], "failed": [], "degraded": False}
    checked = _apply_citation_checks(fake, tools, payload)
    assert checked["workers"][0]["evidence_citations"]["unknown_refs"] == ["case:made_up"]
    assert checked["workers"][0]["output"]["escalate"] is True
    assert checked["degraded"] is True and checked["executed"] == []
    assert checked["is_official"] is False

    good = {"workers": [{"worker_id": INVESTIGATOR, "status": "COMPLETED",
                         "output": {"summary": "s",
                                    "evidence_refs": ["cause:unposted_fee_or_tax",
                                                      "cause:valuation_asof_gap"],
                                    "escalate": False}}],
            "executed": [INVESTIGATOR], "failed": [], "degraded": False}
    ok = _apply_citation_checks(good, tools, payload)
    assert ok["workers"][0]["evidence_citations"]["grounded"] is True
    assert ok["degraded"] is False
    print("  인용 날조 -> escalate      OK")

    # 8. 실제 레지스트리 실행 - 스텁 LLM 은 **1번만** 불린다
    calls: list[str] = []

    def stub(_system: str, prompt: str) -> str:
        worker_id = next((line.split(":", 1)[1].strip() for line in prompt.splitlines()
                          if line.startswith("Worker id:")), "?")
        calls.append(worker_id)
        return json.dumps({"summary": f"ctx for {worker_id}", "confidence": 0.8,
                           "evidence_refs": ["cause:unposted_fee_or_tax"], "escalate": False})

    out = run_employee_workers(payload, llm=stub)
    assert out["degraded"] is False, out.get("failed")
    assert set(out["executed"]) == {INVESTIGATOR, RUNNER_ID}, sorted(out["executed"])
    assert out["llm_worker_count"] == 1
    assert calls == [INVESTIGATOR], f"LLM 이 잡무 직원까지 불렸다: {calls}"
    assert out["is_official"] is False
    print("  2명 실행 / LLM 은 1회      OK")

    # 9. 조사관이 날조해도 잡무 직원 보고는 살아 있다
    def faker(_system: str, _prompt: str) -> str:
        return json.dumps({"summary": "s", "confidence": 0.5,
                           "evidence_refs": ["mem:invented"], "escalate": False})

    dirty = run_employee_workers(payload, llm=faker)
    assert dirty["failed"] == [INVESTIGATOR], dirty["failed"]
    assert dirty["executed"] == [RUNNER_ID], dirty["executed"]
    assert dirty["degraded"] is True
    assert dirty["is_official"] is False
    print("  날조 -> 조사관만 degrade   OK")

    # 10. **직원이 공식이라고 주장해도 공식이 되지 않는다**
    #     LLM 문장에서 회계 확정을 끌어내지 않는다는 규칙(원칙 5)의 실제 배선 검사.
    def claims_official(_system: str, _prompt: str) -> str:
        return json.dumps({"summary": "마감 확정합니다", "confidence": 1.0,
                           "evidence_refs": [], "escalate": False,
                           "is_official": True, "authoritative": True})

    claimed = run_employee_workers(payload, llm=claims_official)
    assert claimed["is_official"] is False
    assert claimed["workers"][0]["output"].get("is_official") is not True
    assert _apply_citation_checks({"workers": []}, tools, payload)["is_official"] is False
    print("  공식 주장 무효화           OK")

    print(f"ok - 회계 직원 레지스트리 10개 영역 점검 통과 "
          f"(LLM {len(WORKER_SPECS)}명 + 잡무 1명, 잡무는 모델을 부르지 않는다)")
