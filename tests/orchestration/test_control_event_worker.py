from __future__ import annotations

import json
from pathlib import Path

from orchestration.control_event_worker import CHILDREN, healthcheck


def test_healthcheck_requires_both_live_department_processes(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "health.json"
    path.write_text(
        json.dumps(
            {
                "heartbeat": 100.0,
                "children": {
                    child.name: {"pid": index + 10, "status": "running"}
                    for index, child in enumerate(CHILDREN)
                },
            }
        ),
        encoding="utf-8",
    )
    observed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        "orchestration.control_event_worker.os.kill",
        lambda pid, sig: observed.append((pid, sig)),
    )

    assert healthcheck(path, now=105.0)
    assert observed == [(10, 0), (11, 0)]


def test_healthcheck_rejects_missing_consumer_or_stale_heartbeat(
    tmp_path: Path, monkeypatch
) -> None:
    path = tmp_path / "health.json"
    path.write_text(
        json.dumps(
            {
                "heartbeat": 100.0,
                "children": {
                    CHILDREN[0].name: {"pid": 10, "status": "running"},
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("orchestration.control_event_worker.os.kill", lambda *_: None)

    assert not healthcheck(path, now=105.0)
    assert not healthcheck(path, now=111.0)
