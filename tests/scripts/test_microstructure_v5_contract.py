from pathlib import Path
import sys
from datetime import date


ROOT = Path(__file__).resolve().parents[2]
PIPELINE = ROOT / "departments" / "04-quant-backtest" / "pipeline"
sys.path.insert(0, str(PIPELINE))

import alpha_ast  # noqa: E402
import backtest_runner  # noqa: E402
import dataset_spec  # noqa: E402
import feature_catalog  # noqa: E402
import factory_bridge  # noqa: E402
import microstructure_builder  # noqa: E402


V5_FIELDS = {"book_depth_notional_l1", "book_depth_notional_l10"}


def test_depth_capacity_fields_are_executable_only_on_v5() -> None:
    assert V5_FIELDS.issubset(alpha_ast.FIELDS)
    for field in V5_FIELDS:
        config = {"signal_expr": {"op": "ts_mean", "field": field, "n": 3}}
        assert backtest_runner.micro_dataset_for(config) == (
            "krx-microstructure-daily", "v5")

    v4 = dataset_spec.spec_for("krx-microstructure-daily", "v4")
    v5 = dataset_spec.spec_for("krx-microstructure-daily", "v5")
    assert v5 is dataset_spec.MICROSTRUCTURE_DAILY_V5
    assert V5_FIELDS.isdisjoint(v4.columns)
    assert V5_FIELDS.issubset(v5.columns)
    assert v5.source_versions == {"microstructure_features": "ms-daily-v5"}


def test_gate0_counts_only_the_ast_required_dataset_version() -> None:
    proposal = {
        "suggested_params": {"signal_expr": {
            "op": "ts_mean", "field": "book_depth_notional_l10", "n": 3}},
        "data_requirements": {"tables": ["microstructure_features"]},
    }
    assert factory_bridge._micro_dataset_for_proposal(proposal) == (
        "krx-microstructure-daily", "v5")
    sql = " ".join(factory_bridge._SQL_MICRO_AVAILABLE_DAYS.lower().split())
    assert "m.name = %s and m.version = %s" in sql


def test_ofi_over_depth_capacity_is_a_valid_mechanism_interaction() -> None:
    expr = {
        "op": "div",
        "args": [
            {"op": "ts_last", "field": "ofi_close", "n": 1},
            {"op": "ts_mean", "field": "book_depth_notional_l10", "n": 3},
        ],
    }
    parsed = alpha_ast.parse(expr)
    assert alpha_ast.fields_of(parsed) == {"ofi_close", "book_depth_notional_l10"}
    assert backtest_runner.micro_dataset_for({"signal_expr": expr}) == (
        "krx-microstructure-daily", "v5")
    alignment = alpha_ast.check_alignment(
        expr, "마감 주문흐름 압력을 10호가 유동성 수용력으로 나눈 가격충격 가설")
    assert alignment["ok"], alignment


def test_both_origins_compute_notional_depth_in_one_quote_scan() -> None:
    local = microstructure_builder._SQL_BUILD
    external = microstructure_builder._SQL_BUILD_EXTERNAL
    for sql, table in ((local, "market.market_quotes"),
                       (external, "public.quotes")):
        assert all(field in sql for field in V5_FIELDS)
        assert sql.count(f"from {table}") == 1
        assert "/ 1e6" in sql
    assert "bid_prices[1] * bid_sizes[1]" in local
    assert "bid_vol10" in external and "ask_vol10" in external


def test_external_manifest_hashes_complete_typed_rows_not_only_counts() -> None:
    sql = microstructure_builder._SQL_BUILD_EXTERNAL
    assert "hash_record_extended(quotes, 0)" in sql
    assert "hash_record_extended(quotes, 1)" in sql
    assert "hash_record_extended(ticks, 0)" in sql
    assert "hash_record_extended(ticks, 1)" in sql
    assert "jsonb_build_array" not in sql

    d = date(2026, 8, 14)
    original = microstructure_builder.external_content_fingerprints(
        d, "005930", 100, 200, "quote-xor", "quote-sum",
        "trade-xor", "trade-sum")
    corrected = microstructure_builder.external_content_fingerprints(
        d, "005930", 100, 200, "quote-xor-corrected", "quote-sum",
        "trade-xor", "trade-sum")
    assert original[0] != corrected[0]
    assert original[2] != corrected[2]
    assert all(len(value) == 64 for value in original)


def test_external_source_days_use_the_same_kst_calendar_as_aggregation() -> None:
    source = Path(microstructure_builder.__file__).read_text(encoding="utf-8")
    assert "(ts at time zone 'Asia/Seoul')::date" in source
    assert "select distinct ts::date from public.ticks" not in source


def test_v5_schema_and_runtime_reject_negative_capacity() -> None:
    migration = (ROOT / "timescaledb" / "migrations" /
                 "008_microstructure_depth_capacity.sql").read_text(encoding="utf-8")
    assert all(field in migration for field in V5_FIELDS)
    assert "microstructure_v5_depth_capacity_nonnegative" in migration
    microstructure_builder.assert_v5_capacity(
        book_depth_notional_l1=0, book_depth_notional_l10=10)
    try:
        microstructure_builder.assert_v5_capacity(book_depth_notional_l1=-1)
    except ValueError as exc:
        assert "book_depth_notional_l1" in str(exc)
    else:
        raise AssertionError("negative displayed depth was accepted")


def test_feature_catalog_defaults_to_latest_immutable_feature_set() -> None:
    assert "ms-daily-v5" in feature_catalog.measure.__kwdefaults__.values()
    names = {name for name, _direction, _mechanism in feature_catalog.MICRO_FEATURES}
    assert V5_FIELDS.issubset(names)


def test_daily_refresh_targets_v5_and_multiday_fdw_is_bounded() -> None:
    autopilot = (ROOT / "departments" / "01-research" / "factory" /
                 "factory_autopilot.py").read_text(encoding="utf-8")
    builder = (ROOT / "departments" / "04-quant-backtest" / "pipeline" /
               "microstructure_builder.py").read_text(encoding="utf-8")
    assert '_MS_DATASET = "krx-microstructure-daily/v5"' in autopilot
    assert "a.fdw and not (one_explicit_day or a.days == 1)" in builder
    assert "다일 백필은 --external-dsn" in builder
    assert "days = [d for d in days if d not in done]" in builder
    assert "days = [d for d in days if d.isoweekday() <= 5]" in builder
    assert "calendar = official_trading_days(meta" in builder
    assert "평일 추측으로 미시구조 날짜를 만들지 않는다" in builder
