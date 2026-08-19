import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/bootstrap_intraday_archive_fdw.py"
SPEC = importlib.util.spec_from_file_location("bootstrap_intraday_archive_fdw", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
fdw = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = fdw
SPEC.loader.exec_module(fdw)


def source_endpoint(**overrides):
    values = {
        "host": "archive.internal",
        "port": 5432,
        "database": "market_archive",
        "user": "trader",
        "password": "source-secret",
    }
    values.update(overrides)
    return fdw.DatabaseEndpoint(**values)


def exact_state(source=None):
    source = source or source_endpoint()
    return fdw.TargetState(
        extension_exists=True,
        schema_exists=True,
        current_user="postgres",
        server_fdw="postgres_fdw",
        server_options=source.server_options,
        mapping_options={"user": source.user, "password": source.password},
        relations={
            table: fdw.RelationState(
                kind="f",
                server=fdw.SERVER_NAME,
                options={"schema_name": fdw.REMOTE_SCHEMA, "table_name": table},
            )
            for table in fdw.REQUIRED_COLUMNS
        },
        columns=dict(fdw.REQUIRED_COLUMNS),
    )


def test_database_url_parse_is_secret_safe_and_copies_only_fdw_options() -> None:
    endpoint = fdw.parse_database_url(
        "postgresql://trader:p%40ss@archive.internal:5439/market_archive"
        "?sslmode=require&connect_timeout=9",
        label="INTRADAY_ARCHIVE_DATABASE_URL",
        require_credentials=True,
    )

    assert endpoint.host == "archive.internal"
    assert endpoint.port == 5439
    assert endpoint.database == "market_archive"
    assert endpoint.user == "trader"
    assert endpoint.password == "p@ss"
    assert endpoint.server_options == {
        "host": "archive.internal",
        "port": "5439",
        "dbname": "market_archive",
        "fetch_size": "50000",
        "sslmode": "require",
    }
    assert "p@ss" not in repr(endpoint)


@pytest.mark.parametrize(
    "value,message",
    [
        ("", "is required"),
        ("mysql://u:p@host/db", "must use"),
        ("postgresql://host/db", "must include source user and password"),
    ],
)
def test_source_database_url_is_fail_closed(value: str, message: str) -> None:
    with pytest.raises(fdw.BootstrapError, match=message):
        fdw.parse_database_url(
            value,
            label="INTRADAY_ARCHIVE_DATABASE_URL",
            require_credentials=True,
        )


def test_empty_target_has_complete_minimal_bootstrap_plan() -> None:
    state = fdw.TargetState(
        extension_exists=False,
        schema_exists=False,
        current_user="postgres",
    )

    actions = fdw.build_plan(state, source_endpoint(), reconfigure=False)

    assert [action.kind for action in actions] == [
        "create_extension",
        "create_schema",
        "create_server",
        "create_mapping",
        "import_table",
        "import_table",
    ]
    assert {action.table for action in actions if action.kind == "import_table"} == {
        "quotes",
        "ticks",
    }


def test_exact_target_is_idempotent() -> None:
    source = source_endpoint()

    assert fdw.build_plan(exact_state(source), source, reconfigure=False) == []


def test_masked_mapping_password_is_accepted_only_with_later_read_probe() -> None:
    source = source_endpoint()
    state = exact_state(source)
    state = fdw.TargetState(
        **{
            **state.__dict__,
            "mapping_options": {"user": "trader", "password": "********"},
        }
    )

    assert fdw.build_plan(state, source, reconfigure=False) == []


def test_reconfigure_always_rotates_a_masked_mapping_password() -> None:
    source = source_endpoint(password="rotated-secret")
    state = exact_state(source)
    state = fdw.TargetState(
        **{
            **state.__dict__,
            "mapping_options": {"user": "trader", "password": "********"},
        }
    )

    actions = fdw.build_plan(state, source, reconfigure=True)

    assert [action.kind for action in actions] == ["alter_mapping"]
    assert "rotated-secret" not in repr(actions)


def test_server_drift_fails_without_leaking_the_secret() -> None:
    source = source_endpoint(password="do-not-print")
    state = exact_state(source)
    state = fdw.TargetState(
        **{
            **state.__dict__,
            "server_options": {**source.server_options, "host": "old.internal"},
        }
    )

    with pytest.raises(fdw.ConfigurationDrift) as raised:
        fdw.build_plan(state, source, reconfigure=False)

    assert "--reconfigure" in str(raised.value)
    assert "do-not-print" not in str(raised.value)


def test_explicit_reconfigure_repairs_safe_metadata_drift() -> None:
    source = source_endpoint()
    state = exact_state(source)
    state = fdw.TargetState(
        **{
            **state.__dict__,
            "server_options": {**source.server_options, "host": "old.internal"},
            "mapping_options": {"user": "old_role", "password": "old"},
            "columns": {
                **state.columns,
                "quotes": frozenset({"ts", "symbol"}),
            },
        }
    )

    actions = fdw.build_plan(state, source, reconfigure=True)

    assert [action.kind for action in actions] == [
        "alter_server",
        "alter_mapping",
        "recreate_foreign_table",
    ]
    assert actions[-1].table == "quotes"


def test_local_table_collision_is_never_dropped_automatically() -> None:
    source = source_endpoint()
    state = exact_state(source)
    state = fdw.TargetState(
        **{
            **state.__dict__,
            "relations": {
                **state.relations,
                "quotes": fdw.RelationState(kind="r"),
            },
        }
    )

    with pytest.raises(fdw.ConfigurationDrift, match="manual intervention"):
        fdw.build_plan(state, source, reconfigure=True)


class FakeCursor:
    def __init__(self, responses):
        self.responses = list(responses)
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, query, params=None):
        self.queries.append((str(query), params))

    def _next(self, method):
        actual_method, value = self.responses.pop(0)
        assert actual_method == method
        return value

    def fetchone(self):
        return self._next("fetchone")

    def fetchall(self):
        return self._next("fetchall")


class FakeConnection:
    def __init__(self, responses):
        self.cursor_instance = FakeCursor(responses)

    def cursor(self):
        return self.cursor_instance


def _source_column_rows():
    return [
        (table, column)
        for table, required in fdw.REQUIRED_COLUMNS.items()
        for column in required
    ]


def test_source_coverage_uses_chunk_metadata_and_bounded_probes() -> None:
    start = datetime(2026, 5, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=100)
    latest_start = end - timedelta(days=1)
    responses = [("fetchall", _source_column_rows())]
    for _table in fdw.REQUIRED_COLUMNS:
        responses.extend(
            [
                ("fetchone", (start, end, 62)),
                ("fetchone", (latest_start, end)),
                ("fetchone", (latest_start, "005930")),
            ]
        )
    connection = FakeConnection(responses)

    coverage = fdw.validate_source(connection)

    assert set(coverage) == {"quotes", "ticks"}
    assert all(item.calendar_days == 100 for item in coverage.values())
    queries = [query.casefold() for query, _params in connection.cursor_instance.queries]
    assert any("timescaledb_information.chunks" in query for query in queries)
    assert not any("min(ts)" in query or "max(ts)" in query for query in queries)
    raw_queries = [
        query
        for query in queries
        if "from public.quotes" in query or "from public.ticks" in query
    ]
    assert raw_queries
    assert all("limit 1" in query and "ts >= %s" in query for query in raw_queries)


def test_source_short_coverage_fails_closed() -> None:
    start = datetime(2026, 8, 1, tzinfo=timezone.utc)
    end = start + timedelta(days=10)
    connection = FakeConnection(
        [
            ("fetchall", _source_column_rows()),
            ("fetchone", (start, end, 2)),
            ("fetchone", (start, end)),
        ]
    )

    with pytest.raises(fdw.BootstrapError, match="covers only 10 calendar days"):
        fdw.validate_source(connection)


def test_main_never_echoes_a_driver_exception_or_dsn(capsys) -> None:
    target = "postgresql://postgres:target-secret@market.internal/market"
    source = "postgresql://trader:source-secret@archive.internal/archive"

    def exploding_connector(dsn, **_kwargs):
        raise RuntimeError(f"driver included {dsn}")

    status = fdw.main(
        ["--check"],
        environ={
            "TIMESCALE_DATABASE_URL": target,
            "INTRADAY_ARCHIVE_DATABASE_URL": source,
        },
        connector=exploding_connector,
    )

    output = capsys.readouterr()
    assert status == 2
    assert "source database connection failed" in output.err
    assert "source-secret" not in output.err
    assert "target-secret" not in output.err
    assert output.out == ""


def test_runtime_required_l10_and_trade_columns_are_contractual() -> None:
    assert {"bid10", "ask10", "bid_vol10", "ask_vol10", "spread"}.issubset(
        fdw.QUOTE_REQUIRED_COLUMNS
    )
    assert {"price", "volume", "ofi_contrib"}.issubset(
        fdw.TICK_REQUIRED_COLUMNS
    )


def test_runbook_declares_aws_and_local_secret_contract() -> None:
    runbook = (
        ROOT / "docs/02-engineering/INTRADAY_ARCHIVE_FDW_RUNBOOK.md"
    ).read_text(encoding="utf-8")

    assert "TIMESCALE_DATABASE_URL" in runbook
    assert "INTRADAY_ARCHIVE_DATABASE_URL" in runbook
    assert "--check" in runbook
    assert "--reconfigure" in runbook
    assert "AWS Secrets Manager" in runbook
    assert "61-session" in runbook
