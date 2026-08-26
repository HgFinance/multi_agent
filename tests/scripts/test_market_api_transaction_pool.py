from __future__ import annotations

import ast
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
API_DIR = ROOT / "departments" / "01-research" / "api"
COLLECTORS = ROOT / "departments" / "01-research" / "collectors"
for path in (API_DIR, COLLECTORS):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

SPEC = importlib.util.spec_from_file_location(
    "market_api_transaction_pool_test_module", API_DIR / "market_api.py"
)
assert SPEC is not None and SPEC.loader is not None
market_api = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(market_api)


class _ReadinessCursor:
    def __init__(self, result):
        self.result = result
        self.executions = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def execute(self, sql, params=()):
        self.executions.append((" ".join(str(sql).split()), params))

    def fetchone(self):
        return self.result


class _ReadinessConnection:
    def __init__(self, result):
        self.cursor_instance = _ReadinessCursor(result)
        self.rollbacks = 0
        self.closed = False

    def cursor(self):
        return self.cursor_instance

    def rollback(self):
        self.rollbacks += 1

    def close(self):
        self.closed = True


def test_deep_readiness_checks_symbol_authority_and_market_relation(monkeypatch):
    control = _ReadinessConnection((True,))
    market = _ReadinessConnection((1,))
    connections = iter((control, market))
    calls = []
    monkeypatch.setattr(
        market_api,
        "load_project_env",
        lambda: {"DATABASE_URL": "control", "TIMESCALE_DATABASE_URL": "market"},
    )
    monkeypatch.setitem(
        sys.modules,
        "psycopg2",
        SimpleNamespace(
            connect=lambda dsn, **kwargs: (
                calls.append((dsn, kwargs)) or next(connections)
            )
        ),
    )

    result = market_api._deep_readiness()

    assert result["status"] == "ready"
    assert calls == [
        ("control", {"connect_timeout": 3}),
        ("market", {"connect_timeout": 3}),
    ]
    assert control.rollbacks == market.rollbacks == 1
    assert control.closed and market.closed
    assert any(
        "reference.instrument_symbols" in statement
        for statement, _params in control.cursor_instance.executions
    )
    assert any(
        "market.market_bars" in statement
        for statement, _params in market.cursor_instance.executions
    )


def test_ready_fails_closed_without_exposing_database_details(monkeypatch):
    monkeypatch.setattr(
        market_api,
        "_deep_readiness",
        lambda: (_ for _ in ()).throw(RuntimeError("secret dsn")),
    )

    with pytest.raises(market_api.HTTPException) as exc_info:
        market_api.ready()

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == {
        "status": "not_ready",
        "error_code": "RuntimeError",
    }


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


def test_market_api_uses_only_transaction_local_read_only_on_port_6543(monkeypatch):
    connection = _Connection()
    connect_calls = []
    pool_dsn = (
        "postgresql://user:secret@aws-1-ap-northeast-2.pooler.supabase.com:6543/market"
    )
    monkeypatch.setattr(
        market_api, "load_project_env", lambda: {"TIMESCALE_DATABASE_URL": pool_dsn}
    )
    monkeypatch.setitem(
        sys.modules,
        "psycopg2",
        SimpleNamespace(
            connect=lambda *args, **kwargs: (
                connect_calls.append((args, kwargs)) or connection
            )
        ),
    )
    monkeypatch.setattr(market_api, "_ts", None)

    rows = market_api._query(
        "select value from market.sample where instrument_id=%s", ("iid",)
    )

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
            if (
                not isinstance(node, ast.Call)
                or not node.args
                or not isinstance(node.func, ast.Attribute)
                or node.func.attr != "execute"
                or not isinstance(node.args[0], ast.Constant)
                or not isinstance(node.args[0].value, str)
            ):
                continue
            executed_literals.append(" ".join(node.args[0].value.lower().split()))

    assert executed_literals.count("set transaction read only") == 4
    assert not any(
        "default_transaction_read_only" in statement for statement in executed_literals
    )


def _capture_bars_query(monkeypatch):
    captured = {}
    monkeypatch.setattr(market_api, "_iid_or_404", lambda _symbol: "instrument-1")
    monkeypatch.setattr(
        market_api,
        "_query",
        lambda statement, params: (
            captured.update(
                {"statement": " ".join(statement.split()), "params": params}
            )
            or []
        ),
    )
    return captured


def test_higher_intraday_bars_are_canonically_derived_from_final_one_minute_rows(
    monkeypatch,
):
    captured = _capture_bars_query(monkeypatch)

    assert (
        market_api.bars("005930", interval="5M", limit=30, source=None, to=None) == []
    )

    assert "time_bucket(interval '5 minutes'" in captured["statement"]
    assert "count(*) = count(distinct bucket_time)" in captured["statement"]
    assert "max(bucket_time) - min(bucket_time)" in captured["statement"]
    assert "'consolidated_1m'::text as source" in captured["statement"]
    assert captured["params"] == ("instrument-1", 30)


def test_intraday_bars_never_read_the_nightly_backfill_table(monkeypatch):
    """Regression: 장중 프레임이 market_bars(ls_chart) 로 돌아가면 안 된다.

    chart-minute-universe 잡이 배포 이미지에 등록돼 있지 않아 그 테이블의 1M 이
    영구 0행이었고, /bars?interval=1M 이 항상 [] 를 돌려줘 조건주문 워커가
    INSUFFICIENT_HISTORY 로만 backoff 했다(2026-08-26). 야간 백필은 원리상
    당일 장중 규칙을 만족시킬 수 없으므로, 장중 프레임은 실시간 연속집계를
    읽어야 한다.
    """
    for interval in ("1M", "5M", "15M", "1H"):
        captured = _capture_bars_query(monkeypatch)

        assert (
            market_api.bars("005930", interval=interval, limit=30, source=None, to=None)
            == []
        )

        statement = captured["statement"]
        assert "market.bars_1m_consolidated" in statement, interval
        assert "market.market_bars" not in statement, interval
        assert "interval_code" not in statement, interval
        assert "ls_chart" not in statement, interval


def test_one_minute_bars_mark_only_elapsed_buckets_final(monkeypatch):
    """부분봉이 확정봉으로 새어 나가면 지표가 진행 중인 분을 먹는다."""
    captured = _capture_bars_query(monkeypatch)

    assert (
        market_api.bars("005930", interval="1M", limit=122, source=None, to=None) == []
    )

    statement = captured["statement"]
    assert "(bucket_time + interval '1 minute' <= now()) as is_final" in statement
    assert captured["params"] == ("instrument-1", 122)


def test_explicit_ls_chart_source_still_reads_the_backfill_table(monkeypatch):
    """과거 백필을 명시적으로 요구한 호출자는 기존 경로를 유지한다."""
    captured = _capture_bars_query(monkeypatch)

    assert (
        market_api.bars("005930", interval="1M", limit=30, source="ls_chart", to=None)
        == []
    )

    assert "market.market_bars" in captured["statement"]
    assert captured["params"] == ("instrument-1", "1M", "ls_chart", 30)


def test_three_minute_bars_are_rejected_instead_of_derived(monkeypatch):
    import pytest

    monkeypatch.setattr(market_api, "_iid_or_404", lambda _symbol: "instrument-1")

    with pytest.raises(market_api.HTTPException) as exc_info:
        market_api.bars("005930", interval="3M", limit=62, source=None, to=None)

    assert exc_info.value.status_code == 422
    assert "use 5M" in str(exc_info.value.detail)


def test_one_minute_bar_reads_remain_direct(monkeypatch):
    captured = {}
    monkeypatch.setattr(market_api, "_iid_or_404", lambda _symbol: "instrument-1")
    monkeypatch.setattr(
        market_api,
        "_query",
        lambda statement, params: (
            captured.update(
                {"statement": " ".join(statement.split()), "params": params}
            )
            or []
        ),
    )

    market_api.bars("005930", interval="1M", limit=10, source=None, to=None)

    # 1M 은 파생 프레임과 달리 집계를 거치지 않고 연속집계를 그대로 읽는다.
    # interval_code 는 더 이상 파라미터가 아니다 - 소스가 1분봉 전용 뷰다.
    assert "time_bucket" not in captured["statement"]
    assert captured["params"] == ("instrument-1", 10)
