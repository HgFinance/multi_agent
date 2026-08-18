from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "departments" / "01-research" / "api"
COLLECTORS = ROOT / "departments" / "01-research" / "collectors"
for path in (API_DIR, COLLECTORS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

SPEC = importlib.util.spec_from_file_location(
    "market_api_transaction_pool_test_module", API_DIR / "market_api.py")
assert SPEC is not None and SPEC.loader is not None
market_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(market_api)


class _Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.rows = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        normalized = " ".join(str(sql).lower().split())
        self.connection.executions.append((normalized, params))
        if normalized == "set transaction read only":
            return
        self.description = [("value",)]
        self.rows = [("governed",)]

    def fetchall(self):
        return list(self.rows)


class _Connection:
    def __init__(self):
        self.closed = False
        self.executions = []
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_market_api_uses_only_transaction_local_read_only_on_port_6543(
        monkeypatch):
    connection = _Connection()
    connect_calls = []
    pool_dsn = (
        "postgresql://user:secret@aws-1-ap-northeast-2."
        "pooler.supabase.com:6543/market")
    monkeypatch.setattr(
        market_api, "load_project_env",
        lambda: {"TIMESCALE_DATABASE_URL": pool_dsn})
    monkeypatch.setitem(
        sys.modules, "psycopg2",
        SimpleNamespace(connect=lambda *args, **kwargs: (
            connect_calls.append((args, kwargs)) or connection)))
    monkeypatch.setattr(market_api, "_ts", None)

    rows = market_api._query(
        "select value from market.sample where instrument_id=%s", ("iid",))

    assert rows == [{"value": "governed"}]
    assert connect_calls == [((pool_dsn,), {"connect_timeout": 10})]
    assert connection.executions == [
        ("set transaction read only", ()),
        ("select value from market.sample where instrument_id=%s", ("iid",)),
    ]
    assert connection.commits == 0
    assert connection.rollbacks == 1


def test_research_read_apis_never_execute_session_default_read_only_sql():
    executed_literals = []
    for path in (API_DIR / "market_api.py", API_DIR / "main.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (not isinstance(node, ast.Call) or not node.args
                    or not isinstance(node.func, ast.Attribute)
                    or node.func.attr != "execute"
                    or not isinstance(node.args[0], ast.Constant)
                    or not isinstance(node.args[0].value, str)):
                continue
            executed_literals.append(
                " ".join(node.args[0].value.lower().split()))

    assert executed_literals.count("set transaction read only") == 2
    assert not any("default_transaction_read_only" in statement
                   for statement in executed_literals)
