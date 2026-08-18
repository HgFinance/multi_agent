from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "aws_database_bootstrap.py"
SPEC = importlib.util.spec_from_file_location("aws_database_bootstrap", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
bootstrap = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = bootstrap
SPEC.loader.exec_module(bootstrap)


def test_discovers_complete_unique_canonical_chains() -> None:
    control = bootstrap.discover_migrations(
        bootstrap.CONTROL_MIGRATIONS, bootstrap.CONTROL_PATTERN
    )
    market = bootstrap.discover_migrations(
        bootstrap.MARKET_MIGRATIONS, bootstrap.MARKET_PATTERN
    )

    assert len(control) == 87
    assert len(market) == 8
    assert control[-1].path.name == "20260818001700_trading_runtime_risk_read.sql"
    assert market[-1].path.name == "008_microstructure_depth_capacity.sql"
    assert len({migration.version for migration in control}) == len(control)
    assert len({migration.version for migration in market}) == len(market)
    assert all(len(migration.checksum) == 64 for migration in (*control, *market))


def test_outer_transactions_are_owned_by_runner() -> None:
    wrapped = "\ufeff\nBEGIN;\nselect 1;\nCOMMIT;\n"
    assert bootstrap.migration_body(wrapped) == "select 1;"
    unwrapped = "-- comment\nselect 1;"
    assert bootstrap.migration_body(unwrapped) == unwrapped


def test_history_must_be_an_exact_prefix() -> None:
    migrations = bootstrap.discover_migrations(
        bootstrap.MARKET_MIGRATIONS, bootstrap.MARKET_PATTERN
    )
    assert bootstrap.validate_applied_prefix(
        migrations, [migrations[0].version, migrations[1].version], "market"
    ) == 2
    with pytest.raises(bootstrap.BootstrapError, match="contiguous prefix"):
        bootstrap.validate_applied_prefix(
            migrations, [migrations[1].version], "market"
        )


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        ("postgresql://user:secret@timescaledb:5432/control", "control"),
        ("postgres://user:secret@timescaledb/market?sslmode=disable", "market"),
        ("postgresql://user:secret@timescaledb/a%5Fb", "a_b"),
    ],
)
def test_database_name_parser_never_needs_credentials(dsn: str, expected: str) -> None:
    assert bootstrap.database_name_from_dsn(dsn) == expected


def test_main_does_not_render_unexpected_exception_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hidden = "postgresql://admin:do-not-print@private.example/control"

    def fail(_arguments: object) -> None:
        raise RuntimeError(hidden)

    monkeypatch.setattr(bootstrap, "run", fail)
    assert bootstrap.main([]) == 1
    output = capsys.readouterr().err
    assert hidden not in output
    assert "RuntimeError" in output


def test_paper_seed_defaults_are_the_approved_scope() -> None:
    assert str(bootstrap.DEFAULT_USER_ID) == "00000000-0000-4000-8000-00000000cec0"
    assert str(bootstrap.DEFAULT_FUND_ID) == "5c26db42-ce83-4daf-b1dc-c81680c13a6c"
    assert str(bootstrap.DEFAULT_BOOK_ID) == "07d913de-9a5b-4cf5-b893-31a625445761"
    assert bootstrap.DEFAULT_CASH_KRW == 1_000_000_000
    assert {code for code, _name, _type in bootstrap.ACCOUNT_CHART} >= {
        "1000",
        "1100",
        "2000",
        "3000",
        "5000",
        "5100",
    }


def test_runtime_login_contract_has_one_exact_settable_role_per_login() -> None:
    assert bootstrap.RUNTIME_LOGIN_MEMBERSHIPS == {
        "hgfinance_runtime": ("service_role", True),
        "hgfinance_order_runtime": ("svc_order_orchestrator", False),
        "hgfinance_trading_runtime": ("svc_trading_api", False),
        "hgfinance_accounting_runtime": ("svc_accounting_ledger", False),
    }
    assert bootstrap.RUNTIME_LOGIN_PASSWORD_KEYS == {
        "hgfinance_runtime": "HEDGEFUND_RUNTIME_DB_PASSWORD",
        "hgfinance_order_runtime": "HEDGEFUND_ORDER_DB_PASSWORD",
        "hgfinance_trading_runtime": "HEDGEFUND_TRADING_DB_PASSWORD",
        "hgfinance_accounting_runtime": "HEDGEFUND_ACCOUNTING_DB_PASSWORD",
    }
    assert bootstrap.GENERIC_RUNTIME_SET_ROLES == (
        "svc_quant",
        "svc_audit_api",
        "svc_qa_worker",
        "svc_qa_reproducer",
    )
    for login in (
        "hgfinance_order_runtime",
        "hgfinance_trading_runtime",
        "hgfinance_accounting_runtime",
    ):
        assert len(bootstrap._memberships_for_login(login)) == 1
        assert "service_role" not in bootstrap._memberships_for_login(login)


def test_generic_control_compatibility_excludes_critical_mutations() -> None:
    privileges = bootstrap.GENERIC_EXECUTION_PRIVILEGES
    for table_name in (
        "user_order_requests",
        "user_order_interpretations",
        "user_order_request_events",
        "user_directives",
        "user_directive_proofs",
        "user_directive_legs",
        "paper_order_reservations",
        "paper_user_directive_fills",
        "paper_directive_barriers",
    ):
        assert table_name not in privileges
    assert privileges == {"market_snapshots": ("SELECT",)}
    assert bootstrap.GENERIC_READ_SCHEMAS == ("api", "accounting", "reference")
    assert "reference" not in bootstrap.GENERIC_DML_SCHEMAS
    assert bootstrap.TRADING_OUTBOX_RELAY_UPDATE_COLUMNS == (
        "status",
        "sent_at",
        "attempts",
        "last_error",
        "available_at",
    )


def test_runtime_database_passwords_are_distinct_long_and_url_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = {}
    for index, (login, key) in enumerate(
        bootstrap.RUNTIME_LOGIN_PASSWORD_KEYS.items()
    ):
        value = chr(ord("a") + index) * 40
        expected[login] = value
        monkeypatch.setenv(key, value)

    assert bootstrap.runtime_login_passwords() == expected


def test_runtime_database_password_validation_never_discloses_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hidden = "not/url/safe/credential/that-must-stay-hidden"
    for index, key in enumerate(bootstrap.RUNTIME_LOGIN_PASSWORD_KEYS.values()):
        monkeypatch.setenv(key, chr(ord("k") + index) * 40)
    monkeypatch.setenv("HEDGEFUND_ORDER_DB_PASSWORD", hidden)

    with pytest.raises(bootstrap.BootstrapError) as error:
        bootstrap.runtime_login_passwords()

    assert "HEDGEFUND_ORDER_DB_PASSWORD" in str(error.value)
    assert hidden not in str(error.value)


def test_runtime_database_passwords_reject_duplicates_without_disclosure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    duplicate = "d" * 40
    for index, key in enumerate(bootstrap.RUNTIME_LOGIN_PASSWORD_KEYS.values()):
        monkeypatch.setenv(key, chr(ord("p") + index) * 40)
    monkeypatch.setenv("HEDGEFUND_RUNTIME_DB_PASSWORD", duplicate)
    monkeypatch.setenv("HEDGEFUND_TRADING_DB_PASSWORD", duplicate)

    with pytest.raises(bootstrap.BootstrapError, match="all be distinct") as error:
        bootstrap.runtime_login_passwords()

    assert duplicate not in str(error.value)
