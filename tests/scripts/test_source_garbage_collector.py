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


def test_retired_orphan_candidates_have_no_tracked_python_references() -> None:
    report = garbage_collector.build_report(ROOT)

    assert report["candidate_count"] == 3
    findings = {finding["path"]: finding for finding in report["findings"]}

    assert findings["apps/api/fact_router.py"]["exists"] is True
    assert findings["apps/api/fact_router.py"]["test_references"]
    assert findings["apps/api/fact_router.py"]["status"] == "REVIEW"
    assert findings["apps/api/ceo_hermes_client.py"]["exists"] is True
    assert findings["apps/api/ceo_hermes_client.py"]["test_references"]
    assert findings["apps/api/ceo_hermes_client.py"]["status"] == "REVIEW"
    assert findings["departments/02-trading/contracts/packet_gate.py"] == {
        "path": "departments/02-trading/contracts/packet_gate.py",
        "exists": False,
        "static_references": [],
        "test_references": [],
        "status": "REMOVED",
    }
