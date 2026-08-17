from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
FACTORY = ROOT / "departments" / "01-research" / "factory"


def _run_self_check(script: str, *args: str) -> str:
    completed = subprocess.run(
        [sys.executable, str(FACTORY / script), *args],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return completed.stdout


def test_intraday_proposal_uses_seconds_ast_not_daily_ast() -> None:
    output = _run_self_check("publish_gate.py")
    assert "인트라데이 AST 계약 분리" in output


def test_intraday_history_does_not_inherit_daily_family_budget() -> None:
    output = _run_self_check("proposal_intake.py", "--check")
    assert "에이전트가 게이트에 답할 수 있다" in output
