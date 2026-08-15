from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "departments/04-quant-backtest/pipeline"
sys.path.insert(0, str(PIPELINE))

import alpha_ast  # noqa: E402
import backtest_runner  # noqa: E402
import dataset_spec  # noqa: E402
import microstructure_builder  # noqa: E402


V4_FIELDS = {
    "depth_imbalance_l1",
    "depth_imbalance_l10",
    "depth_imbalance_slope",
    "size_weighted_ofi",
}


def test_v4_fields_are_executable_and_use_v4_dataset() -> None:
    assert V4_FIELDS.issubset(alpha_ast.FIELDS)
    for field in V4_FIELDS:
        config = {"signal_expr": {"op": "ts_mean", "field": field, "n": 3}}
        assert backtest_runner.micro_dataset_for(config) == (
            "krx-microstructure-daily", "v4")


def test_existing_micro_ast_stays_on_immutable_v3() -> None:
    config = {
        "signal_expr": {
            "op": "ts_mean", "field": "order_flow_imbalance", "n": 3,
        }
    }
    assert backtest_runner.micro_dataset_for(config) == (
        "krx-microstructure-daily", "v3")


def test_price_only_ast_has_no_auxiliary_dataset() -> None:
    config = {"signal_expr": {"op": "ts_mean", "field": "close", "n": 3}}
    assert backtest_runner.required_micro_dataset(config) is None


class _ManifestCursor:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params):
        assert "quant.dataset_manifests" in sql
        assert params == ("krx-microstructure-daily", "v4")

    def fetchone(self):
        return self.row


class _ManifestConn:
    def __init__(self, row):
        self.row = row

    def cursor(self):
        return _ManifestCursor(self.row)


def test_auxiliary_manifest_identity_is_sealed_into_config() -> None:
    config = {
        "signal_expr": {
            "op": "ts_mean", "field": "depth_imbalance_slope", "n": 3,
        }
    }
    lineage = backtest_runner.seal_micro_lineage(
        config, _ManifestConn(("dataset-v4", "abc123", 150000)))
    assert lineage == {
        "dataset_id": "dataset-v4",
        "name": "krx-microstructure-daily",
        "version": "v4",
        "content_hash": "abc123",
        "row_count": 150000,
    }
    assert config["_resolved_auxiliary_datasets"] == [lineage]
    before = backtest_runner.input_hash("daily", config, "code", 0)
    config["_resolved_auxiliary_datasets"][0]["content_hash"] = "changed"
    after = backtest_runner.input_hash("daily", config, "code", 0)
    assert before != after


def test_dataset_versions_do_not_retroactively_gain_v4_columns() -> None:
    v3 = dataset_spec.spec_for("krx-microstructure-daily", "v3")
    v4 = dataset_spec.spec_for("krx-microstructure-daily", "v4")
    assert v3 is dataset_spec.MICROSTRUCTURE_DAILY_V3
    assert v4 is dataset_spec.MICROSTRUCTURE_DAILY_V4
    assert V4_FIELDS.isdisjoint(v3.columns)
    assert V4_FIELDS.issubset(v4.columns)
    assert v4.source_versions == {"microstructure_features": "ms-daily-v4"}


def test_both_builder_origins_compute_same_explicit_axes_without_event_sort() -> None:
    local = microstructure_builder._SQL_BUILD
    external = microstructure_builder._SQL_BUILD_EXTERNAL
    for sql in (local, external):
        assert all(field in sql for field in V4_FIELDS)
        assert "row_number(" not in sql.lower()
    assert "bid_sizes[1]" in local and "avg(depth_imbalance)" in local
    assert "avg(bi::float8)" in external
    assert "bid_vol10" in external and "ask_vol10" in external
    assert "sum(ofi_contrib)" in external
    assert "sum(side * volume)" not in external


def test_schema_migration_declares_every_v4_field() -> None:
    migration = (ROOT / "timescaledb/migrations/007_microstructure_depth_clocks.sql") \
        .read_text(encoding="utf-8")
    assert all(field in migration for field in V4_FIELDS)
    assert "microstructure_v4_signed_flow_bounds" in migration
    assert "microstructure_v4_depth_bounds" in migration


def test_v4_bounds_reject_the_old_external_side_failure() -> None:
    microstructure_builder.assert_v4_bounds(
        order_flow_imbalance=1.0,
        size_weighted_ofi=-1.0,
        depth_imbalance_slope=2.0,
    )
    try:
        microstructure_builder.assert_v4_bounds(order_flow_imbalance=2.866409)
    except ValueError as exc:
        assert "order_flow_imbalance" in str(exc)
    else:
        raise AssertionError("side=1/5 오염 OFI 를 허용했다")
