import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/factory_runtime_contract.py"
SPEC = importlib.util.spec_from_file_location("factory_runtime_contract", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
contract = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(contract)


def test_current_tree_satisfies_factory_runtime_contract() -> None:
    manifest = contract.build_manifest(ROOT)
    # The image manifest is persisted as JSON, where tuples become lists.
    checked = contract.verify_manifest(ROOT, json.loads(json.dumps(manifest)))

    assert contract.REQUIRED_AST_FIELDS.issubset(checked["alpha_ast_fields"])
    assert set(checked["files"]) == set(contract.CRITICAL_FILES)


def test_modified_runtime_file_is_rejected() -> None:
    manifest = contract.build_manifest(ROOT)
    first = contract.CRITICAL_FILES[0]
    manifest["files"][first] = "0" * 64

    with pytest.raises(RuntimeError, match="factory runtime drift"):
        contract.verify_manifest(ROOT, manifest)


def test_old_ast_registry_cannot_be_blessed(monkeypatch: pytest.MonkeyPatch) -> None:
    real_loader = contract._load_alpha_ast

    def without_intraday_fields(root: Path):
        module = real_loader(root)
        module.FIELDS = tuple(
            field for field in module.FIELDS if field not in contract.REQUIRED_AST_FIELDS
        )
        return module

    monkeypatch.setattr(contract, "_load_alpha_ast", without_intraday_fields)
    with pytest.raises(RuntimeError, match="required AST fields missing"):
        contract.build_manifest(ROOT)


def test_writer_connection_forces_read_write(monkeypatch: pytest.MonkeyPatch) -> None:
    writer_path = ROOT / "departments/04-quant-backtest/pipeline/db_writer.py"
    spec = importlib.util.spec_from_file_location("factory_db_writer", writer_path)
    assert spec is not None and spec.loader is not None
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)

    calls = []

    class FakeConnection:
        def set_session(self, **kwargs):
            calls.append(("set_session", kwargs))

        def close(self):
            calls.append(("close", {}))

    fake = SimpleNamespace(
        connect=lambda dsn, **kwargs: (
            calls.append(("connect", {"dsn": dsn, **kwargs})) or FakeConnection()
        )
    )
    monkeypatch.setitem(sys.modules, "psycopg2", fake)
    monkeypatch.delenv("DATABASE_RUNTIME_ROLE", raising=False)
    monkeypatch.delenv("DATABASE_SESSION_URL", raising=False)

    conn = writer.connect("postgresql://example", connect_timeout=7)

    assert isinstance(conn, FakeConnection)
    assert calls == [
        ("connect", {"dsn": "postgresql://example", "connect_timeout": 7}),
        ("set_session", {"readonly": False}),
    ]


def test_writer_connection_drops_to_scoped_runtime_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer_path = ROOT / "departments/04-quant-backtest/pipeline/db_writer.py"
    spec = importlib.util.spec_from_file_location("scoped_factory_db_writer", writer_path)
    assert spec is not None and spec.loader is not None
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    calls = []

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql):
            calls.append(("execute", sql))

        def fetchone(self):
            return ("svc_quant",)

    class FakeConnection:
        def set_session(self, **kwargs):
            calls.append(("set_session", kwargs))

        def cursor(self):
            return FakeCursor()

        def commit(self):
            calls.append(("commit", {}))

        def rollback(self):
            calls.append(("rollback", {}))

        def close(self):
            calls.append(("close", {}))

    monkeypatch.setenv("DATABASE_RUNTIME_ROLE", "svc_quant")
    monkeypatch.delenv("DATABASE_SESSION_URL", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "psycopg2",
        SimpleNamespace(
            connect=lambda dsn, **kwargs: (
                calls.append(("connect", {"dsn": dsn, **kwargs}))
                or FakeConnection()
            )
        ),
    )

    writer.connect(
        "postgresql://user:secret@aws-1-ap-northeast-2.pooler.supabase.com:6543/postgres"
    )

    assert calls == [
        (
            "connect",
            {
                "dsn": "postgresql://user:secret@aws-1-ap-northeast-2.pooler.supabase.com:5432/postgres",
                "connect_timeout": 20,
            },
        ),
        ("set_session", {"readonly": False}),
        ("execute", 'SET ROLE "svc_quant"'),
        ("commit", {}),
        ("execute", "select current_user"),
        ("commit", {}),
    ]


def test_writer_rejects_transaction_pool_session_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer_path = ROOT / "departments/04-quant-backtest/pipeline/db_writer.py"
    spec = importlib.util.spec_from_file_location("pooled_factory_db_writer", writer_path)
    assert spec is not None and spec.loader is not None
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    monkeypatch.setenv("DATABASE_RUNTIME_ROLE", "svc_quant")
    monkeypatch.setenv(
        "DATABASE_SESSION_URL",
        "postgresql://user:secret@pool.example:6543/postgres",
    )

    with pytest.raises(RuntimeError, match="transaction-pool port 6543"):
        writer.runtime_session_dsn("postgresql://unused")


def test_writer_rejects_broad_runtime_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer_path = ROOT / "departments/04-quant-backtest/pipeline/db_writer.py"
    spec = importlib.util.spec_from_file_location("broad_factory_db_writer", writer_path)
    assert spec is not None and spec.loader is not None
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)
    monkeypatch.setenv("DATABASE_RUNTIME_ROLE", "postgres")

    with pytest.raises(RuntimeError, match="must be svc_quant"):
        writer.connect("postgresql://example")


def test_writer_rejects_role_that_does_not_survive_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer_path = ROOT / "departments/04-quant-backtest/pipeline/db_writer.py"
    spec = importlib.util.spec_from_file_location("reset_factory_db_writer", writer_path)
    assert spec is not None and spec.loader is not None
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, _sql):
            return None

        def fetchone(self):
            return ("postgres",)

    class Connection:
        def set_session(self, **_kwargs):
            return None

        def cursor(self):
            return Cursor()

        def commit(self):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setenv("DATABASE_RUNTIME_ROLE", "svc_quant")
    monkeypatch.setenv("DATABASE_SESSION_URL", "postgresql://session.example:5432/db")
    monkeypatch.setitem(
        sys.modules,
        "psycopg2",
        SimpleNamespace(connect=lambda *_args, **_kwargs: Connection()),
    )

    with pytest.raises(RuntimeError, match="did not persist"):
        writer.connect("postgresql://unused")


def test_writer_rejects_unsafe_runtime_role_before_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer_path = ROOT / "departments/04-quant-backtest/pipeline/db_writer.py"
    spec = importlib.util.spec_from_file_location("unsafe_factory_db_writer", writer_path)
    assert spec is not None and spec.loader is not None
    writer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(writer)

    class FakeConnection:
        def set_session(self, **_kwargs):
            return None

        def rollback(self):
            return None

        def close(self):
            return None

    monkeypatch.setenv("DATABASE_RUNTIME_ROLE", 'svc_quant"; reset role; --')
    monkeypatch.setitem(
        sys.modules, "psycopg2",
        SimpleNamespace(connect=lambda *_args, **_kwargs: FakeConnection()),
    )

    with pytest.raises(RuntimeError, match="safe SQL role"):
        writer.connect("postgresql://example")
