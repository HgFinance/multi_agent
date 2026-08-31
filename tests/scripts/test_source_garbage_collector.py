from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "source_garbage_collector", ROOT / "scripts" / "source_garbage_collector.py"
)
assert SPEC and SPEC.loader
garbage_collector = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(garbage_collector)


def test_retired_orphan_candidates_are_removed_without_references() -> None:
    report = garbage_collector.build_report(ROOT)

    assert report["candidate_count"] == 3
    findings = {finding["path"]: finding for finding in report["findings"]}

    assert set(findings) == {
        "apps/api/fact_router.py",
        "apps/api/ceo_hermes_client.py",
        "departments/02-trading/contracts/packet_gate.py",
    }
    assert all(
        finding["exists"] is False
        and finding["static_references"] == []
        and finding["test_references"] == []
        and finding["status"] == "REMOVED"
        for finding in findings.values()
    )
