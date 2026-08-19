from pathlib import Path
import inspect
import sys


ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "departments/04-quant-backtest/pipeline"
sys.path.insert(0, str(PIPELINE))

import experiment_orchestrator  # noqa: E402
from experiment_orchestrator import (  # noqa: E402
    dataset_of,
    execution_data_products,
)


def test_microstructure_requirement_keeps_daily_bars_as_execution_base() -> None:
    requested = execution_data_products(["microstructure_features"])

    assert requested == ["market_bars", "microstructure_features"]


def test_orchestrator_resolves_the_augmented_execution_requirements() -> None:
    source = inspect.getsource(experiment_orchestrator.orchestrate)

    assert "res = resolve_data(" in source
    assert "execution_products, meta_conn=conn, market_conn=market_conn" in source
    assert "research_lane=" in source


def test_dataset_selection_prefers_bars_over_feature_only_dataset() -> None:
    resolved = {
        "required_data_products": [
            "krx-microstructure-daily/v3",
            "krx-basket-daily/v3",
        ]
    }

    assert dataset_of(resolved) == ("krx-basket-daily", "v3")


def test_explicit_daily_bars_are_not_duplicated() -> None:
    assert execution_data_products(
        ["market_bars", "microstructure_features"]
    ) == ["market_bars", "microstructure_features"]
    assert execution_data_products(
        ["krx-basket-daily/v3", "krx-microstructure-daily/v3"]
    ) == ["krx-basket-daily/v3", "krx-microstructure-daily/v3"]
