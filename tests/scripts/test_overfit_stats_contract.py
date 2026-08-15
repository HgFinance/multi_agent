import math
import sys
from pathlib import Path


PIPELINE = (Path(__file__).resolve().parents[2]
            / "departments" / "04-quant-backtest" / "pipeline")
sys.path.insert(0, str(PIPELINE))

import overfit_stats  # noqa: E402


def test_constant_return_series_has_no_defined_sharpe():
    assert overfit_stats.sharpe([0.001] * 200) is None
    result = overfit_stats.deflated_sharpe([0.001] * 200, trials=5)
    assert result["sharpe"] is None
    assert result["deflated_sharpe"] is None


def test_nonfinite_returns_fail_closed():
    assert overfit_stats.sharpe([0.001] * 199 + [math.nan]) is None
    assert overfit_stats.sharpe([0.001] * 199 + [math.inf]) is None
