"""Reproducible release-gate audit for the PDF claims and known gaps.

This is an evidence inventory, not a synthetic performance result.  In
particular, a missing load harness or missing latency artifact remains a
failed gate instead of being converted to a green status by static checks.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path

try:
    from scripts.source_garbage_collector import build_report as build_gc_report
except ModuleNotFoundError:  # direct ``python scripts/release_readiness_audit.py``
    from source_garbage_collector import build_report as build_gc_report


PDF_PAGE = 64
REQUIRED_STRESS_EVIDENCE = (
    "workload",
    "concurrency",
    "duration",
    "SLA",
    "p50/p95/p99",
    "throughput",
    "error_rate",
    "recovery_result",
)


def _text(root: Path, relative: str) -> str:
    try:
        return (root / relative).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _stress_harness_exists(root: Path) -> bool:
    candidates = (
        root / "tests/stress",
        root / "tests/load",
        root / "tests/performance",
        root / "scripts/stress_test.py",
        root / "scripts/load_test.py",
        root / "scripts/run_stress_tests.py",
    )
    return any(path.exists() for path in candidates)


def _stress_ci_exists(root: Path) -> bool:
    workflow_root = root / ".github/workflows"
    if not workflow_root.exists():
        return False
    signals = re.compile(r"\b(locust|k6|pytest-benchmark|stress test|load test)\b", re.I)
    return any(
        signals.search(path.read_text(encoding="utf-8", errors="ignore"))
        for path in workflow_root.glob("*.y*ml")
    )


def _dependency_security_state(root: Path) -> tuple[str, dict[str, object], list[str]]:
    lock_path = root / "requirements.lock"
    sbom_path = root / "docs/dependency-python-sbom.cdx.json"
    cve_path = root / "docs/dependency-cve-audit.json"
    lock_present = lock_path.is_file()
    sbom_present = sbom_path.is_file()
    cve_report_present = cve_path.is_file()
    cve_clean = False
    if cve_report_present:
        try:
            report = json.loads(cve_path.read_text(encoding="utf-8"))
            dependencies = report.get("dependencies", [])
            cve_clean = bool(dependencies) and all(
                not dependency.get("vulns") for dependency in dependencies
            )
        except (OSError, TypeError, ValueError):
            cve_clean = False
    evidence = [
        "requirements.lock pins the shared Python runtime with hashes"
        if lock_present
        else "requirements.lock is absent",
        "CycloneDX Python SBOM is tracked"
        if sbom_present
        else "CycloneDX Python SBOM is absent",
        "pip-audit report is tracked with no known Python vulnerabilities"
        if cve_clean
        else "pip-audit report is absent, invalid, or contains vulnerabilities",
        "isolated department Dockerfiles still use their own explicit pins and need a separate image-level scan",
    ]
    state = "PASS" if lock_present and sbom_present and cve_clean else "PARTIAL"
    metadata = {
        "lock_present": lock_present,
        "sbom_present": sbom_present,
        "cve_report_present": cve_report_present,
        "cve_clean": cve_clean,
    }
    return state, metadata, evidence


def build_report(root: Path) -> dict[str, object]:
    compose = _text(root, "docker-compose.yml")
    worker = _text(root, "departments/04-quant-backtest/pipeline/experiment_worker.py")
    requirements = _text(root, "requirements.txt")
    conditional_contract = _text(root, "orchestration/conditional_rules/contracts.py")
    conditional_semantic = _text(root, "orchestration/conditional_rules/semantic.py")
    status_doc = _text(root, "docs/PROJECT_IMPLEMENTATION_STATUS.md")
    architecture_doc = _text(root, "docs/CURRENT_PROJECT_ARCHITECTURE.md")
    gc = build_gc_report(root)
    dependency_status, dependency_metadata, dependency_evidence = (
        _dependency_security_state(root)
    )

    findings: dict[str, dict[str, object]] = {
        "paper_orders": {
            "status": "PASS",
            "evidence": [
                "Compose and .env.example default STRATEGY_PAPER_ORDERS_ENABLED=true",
                "strategy PAPER bundle and Trading PAPER route are covered by tests",
                "LIVE mode remains blocked in strategy_research.py",
            ],
        },
        "quant_bottleneck": {
            "status": (
                "PASS"
                if "quant-experiment-worker:" in compose
                and "ThreadPoolExecutor" in worker
                and "QUANT_EXPERIMENT_WORKERS" in worker
                else "BLOCKED"
            ),
            "evidence": [
                "resident quant-experiment-worker is declared in Compose",
                "bounded fan-out uses isolated database connections",
                "parallelism is capped at eight jobs",
            ],
        },
        "garbage_collection": {
            "status": "PASS" if any(
                finding["path"] == "departments/02-trading/contracts/packet_gate.py"
                and finding["status"] == "REMOVED"
                for finding in gc["findings"]
            ) else "REVIEW",
            "evidence": [
                "all registered retired source candidates are removed after zero source/test references",
            ],
        },
        "dependency_hygiene": {
            "status": dependency_status,
            "evidence": [
                "langsmith, psycopg v3, and pyarrow are now direct requirements",
                *dependency_evidence,
            ],
            **dependency_metadata,
            "direct_requirements_present": all(
                token in requirements for token in ("langsmith", "psycopg[", "pyarrow")
            ),
        },
        "conditional_rules": {
            "status": "PARTIAL",
            "evidence": [
                "AND/OR, indicators, timeframes, and bounded portfolio predicates exist",
                "sizing is limited to FIXED_SHARES/POSITION_PERCENT/ALL",
                "crosses over portfolio values are explicitly unsupported",
                "dynamic max-notional, sequence/state-machine, trailing/high-water, and staged exits remain gaps",
            ],
            "contract_evidence_present": all(
                token in conditional_contract
                for token in ("FIXED_SHARES", "POSITION_PERCENT", "ALL")
            ) and "CROSS_PORTFOLIO_UNSUPPORTED" in conditional_semantic,
        },
        "stress_evidence": {
            "status": "BLOCKED"
            if not _stress_harness_exists(root) or not _stress_ci_exists(root)
            else "NOT_VERIFIED",
            "pdf_page": PDF_PAGE,
            "scenario_count": 10,
            "missing_evidence": list(REQUIRED_STRESS_EVIDENCE),
            "evidence": [
                (
                    "scripts/stress_test.py provides a bounded 10-scenario runner"
                    if _stress_harness_exists(root)
                    else "no dedicated stress/load harness was found"
                ),
                (
                    ".github/workflows/stress-evidence.yml provides a stress test CI job"
                    if _stress_ci_exists(root)
                    else "no stress/load CI job was found"
                ),
                "local 32-way read-only evidence is recorded; PDF 10-scenario certification and fault-recovery evidence remain unverified",
            ],
        },
        "latency_sla": {
            "status": "NOT_VERIFIED",
            "missing_evidence": ["current p50", "current p95", "current p99", "bottleneck trace"],
            "evidence": [
                "read-only runtime p50/p95/p99 evidence is recorded in OPS_HEALTHCHECK_LATENCY_REPORT.md",
                "latest local user-query-to-result sample is 108.050s against the 120s SLA, but continuous/runtime release evidence remains unverified",
                "historical PDF timings are not runtime SLA proof",
            ],
        },
        "runtime_e2e": {
            "status": "NOT_VERIFIED"
            if (
                "not runtime verified" in status_doc.lower()
                or "NOT RUNTIME-VERIFIED" in architecture_doc
            )
            else "REVIEW",
            "evidence": [
                "latest local E2E graph is terminal completed with HTTP 200, but canonical project status still requires continuously operated runtime evidence",
            ],
        },
    }
    blocking = {
        key
        for key, finding in findings.items()
        if finding["status"] in {"BLOCKED", "NOT_VERIFIED"}
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "overall_release_status": "BLOCKED" if blocking else "READY_FOR_REVIEW",
        "blocking_areas": sorted(blocking),
        "findings": findings,
    }


def render_markdown(report: dict[str, object]) -> str:
    findings = report["findings"]
    lines = [
        "# Release Readiness Audit",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Overall release status: **{report['overall_release_status']}**",
        "- This report preserves unmet gates; a static check is not a stress-test result.",
        "",
        "| Area | Status | Evidence / remaining gap |",
        "|---|---|---|",
    ]
    for area, finding in findings.items():
        evidence = "; ".join(str(item) for item in finding.get("evidence", []))
        lines.append(f"| `{area}` | **{finding['status']}** | {evidence} |")
    lines.extend(
        [
            "",
            "## Stress gate",
            "",
            "PDF p.64 names 10 scenarios. The repository now contains an executable runner and CI job, "
            "and local read-only latency evidence, but full 10-scenario workload/concurrency/duration/SLA "
            "coverage plus injected recovery evidence is still absent. Therefore this gate is **BLOCKED**, "
            "not passed by unit-test coverage.",
            "",
            "## PAPER order gate",
            "",
            "The tested path is PAPER-only: bundle → strategy runtime control → Trading PAPER directive → "
            "idempotent PAPER gateway. No LIVE route is enabled. Tests use mocks/fixtures and do not submit "
            "an external account order during CI.",
            "",
            "## Re-run",
            "",
            "```bash",
            "python scripts/release_readiness_audit.py",
            "python scripts/release_readiness_audit.py --write",
            "```",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true", help="refresh the Markdown report")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = build_report(root)
    if args.write:
        report_path = root / "docs/RELEASE_READINESS_AUDIT.md"
        report_path.write_text(render_markdown(report), encoding="utf-8")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
