from __future__ import annotations

from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
FACTORY = ROOT / "departments" / "01-research" / "factory"
CONTRACTS = ROOT / "departments" / "01-research" / "contracts"
for path in (FACTORY, CONTRACTS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


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


def test_intake_counts_exact_primary_without_counting_screening_sidecars() -> None:
    import proposal_intake

    class Cursor:
        def __init__(self):
            self.sql = ""

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql, _params):
            self.sql = sql

        def fetchall(self):
            if "from quant.hypotheses" in self.sql:
                return []
            if "from quant.experiments e" in self.sql and \
                    "config->'intraday_signal_expr'" in self.sql:
                assert "screening_population" not in self.sql
                assert "intraday_session_accesses" in self.sql
                return [("exp-primary", "fam-primary")]
            if "from research.v_current_experiment_outcomes" in self.sql:
                return []
            raise AssertionError(self.sql)

    class Conn:
        def __init__(self):
            self.cursor_value = Cursor()

        def cursor(self):
            return self.cursor_value

    rows = proposal_intake.load_past_outcomes(
        Conn(), "order_flow_imbalance", "krx_all",
        signal_expr={"op": "field", "field": "queue_imbalance_l1"},
        research_lane="INTRADAY_EVENT",
    )
    prior = proposal_intake._prior_check(rows, "")
    assert prior.trials_used == 1
    assert prior.trial_family_id == "fam-primary"
    assert rows == [{
        "decision": "PRIMARY_ATTEMPT",
        "lesson_codes": [],
        "trial_family_id": "fam-primary",
        "experiment_id": "exp-primary",
        "match_scope": "AST_EXACT_PRIMARY",
        "promotion_authority": False,
        "statistical_pressure_only": True,
    }]
