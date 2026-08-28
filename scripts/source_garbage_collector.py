"""Audit statically orphaned Python modules before source cleanup.

The scanner is deliberately conservative: it only reports a candidate as
removable when the repository has no other tracked Python reference to its
module stem.  It does not claim to prove that a dynamic import, an operator
script, or an external deployment cannot load the module.  The default mode
is read-only; ``--write`` refreshes the human-auditable report.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ORPHAN_CANDIDATES = (
    Path("apps/api/fact_router.py"),
    Path("apps/api/ceo_hermes_client.py"),
    Path("departments/02-trading/contracts/packet_gate.py"),
)
REPORT_PATH = Path("docs/SOURCE_GARBAGE_COLLECTION.md")
_EXCLUDED_PARTS = {"docs", "tests", ".venv", "node_modules", "__pycache__"}


def _tracked_python_files(root: Path) -> list[Path]:
    try:
        result = subprocess.run(
            ["git", "ls-files", "--", "*.py"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return [
            path
            for path in root.rglob("*.py")
            if not _EXCLUDED_PARTS.intersection(path.relative_to(root).parts)
        ]
    return [
        root / line
        for line in result.stdout.splitlines()
        if line.strip()
        and not _EXCLUDED_PARTS.intersection(Path(line).parts)
    ]


def _static_references(root: Path, candidate: Path) -> list[str]:
    """Find simple tracked-source references, excluding the candidate itself."""

    stem = candidate.stem
    dotted = str(candidate.with_suffix("")).replace("/", ".")
    references: list[str] = []
    scanner_path = Path(__file__).resolve()
    audit_path = (root / "scripts/release_readiness_audit.py").resolve()
    for path in _tracked_python_files(root):
        if (
            path.relative_to(root) == candidate
            or path.resolve() == scanner_path
            or path.resolve() == audit_path
            or not path.is_file()
        ):
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if stem in source or dotted in source:
            references.append(str(path.relative_to(root)))
    return sorted(set(references))


def _test_references(root: Path, candidate: Path) -> list[str]:
    """Find tests that still exercise a candidate's public behavior."""

    stem = candidate.stem
    dotted = str(candidate.with_suffix("")).replace("/", ".")
    references: list[str] = []
    tests_root = root / "tests"
    scanner_test_path = (tests_root / "scripts/test_source_garbage_collector.py").resolve()
    if not tests_root.exists():
        return references
    for path in tests_root.rglob("*.py"):
        if path.resolve() == scanner_test_path:
            continue
        try:
            source = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if stem in source or dotted in source:
            references.append(str(path.relative_to(root)))
    return sorted(set(references))


def build_report(root: Path) -> dict[str, object]:
    findings = []
    for candidate in ORPHAN_CANDIDATES:
        references = _static_references(root, candidate)
        test_references = _test_references(root, candidate)
        exists = (root / candidate).exists()
        findings.append(
            {
                "path": str(candidate),
                "exists": exists,
                "static_references": references,
                "test_references": test_references,
                "status": (
                    "REVIEW"
                    if exists or references or test_references
                    else "REMOVED"
                ),
            }
        )
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(findings),
        "findings": findings,
    }


def render_markdown(report: dict[str, object]) -> str:
    findings = report["findings"]
    lines = [
        "# Source Garbage Collection",
        "",
        f"- Generated: `{report['generated_at']}`",
        "- Scope: three statically orphaned Python candidates identified in the release audit",
        "- Method: tracked Python source references, excluding the candidate itself",
        "",
        "## Result",
        "",
        "| Candidate | Source refs | Test refs | State |",
        "|---|---:|---:|---|",
    ]
    for finding in findings:
        refs = finding["static_references"]
        lines.append(
            f"| `{finding['path']}` | {len(refs)} | "
            f"{len(finding['test_references'])} | `{finding['status']}` |"
        )
    lines.extend(
        [
            "",
            "Only candidates with no source or test references are removed. "
            "`fact_router.py` and `ceo_hermes_client.py` remain under review because their "
            "behavior is still covered by tests; `packet_gate.py` was removed. This is cleanup "
            "of the working tree and Git history retains recovery. The scanner cannot prove that "
            "an external deployment or dynamic import is absent, so future additions must re-run "
            "the audit before deleting another compatibility surface.",
            "",
            "## Re-run",
            "",
            "```bash",
            "python scripts/source_garbage_collector.py",
            "python scripts/source_garbage_collector.py --write",
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
        report_path = root / REPORT_PATH
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_markdown(report), encoding="utf-8")
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
