#!/usr/bin/env python3
"""회계 원장 줄의 durable 저장소.

소유: 도현 (트레이딩/회계본부)

▶ 왜 필요한가 — 브로커 조회만으로는 장부가 유지되지 않는다

  화면에 뜨는 오늘 거래는 전부 **"오늘"에만 대답하는 조회**에서 온다.

      당일 매매일지      오늘만 준다. 날짜가 바뀌면 전일 TR로 넘어간다
      당일 체결내역      주문일=오늘로 조회한다. 내일이면 안 나온다
      확정 거래내역      결제(T+2)가 끝난 뒤에야 채워진다

  즉 **체결일(D)과 결제일(D+2) 사이에는 어느 조회로도 그 거래를 다시 못 가져오는
  구간**이 생긴다. 우리가 아무것도 남기지 않으면 날짜가 바뀌는 순간 장부가
  통째로 빈다 - "API로 끌어오는데도 다시 열면 사라진다"의 정체다.

  그래서 본 것을 우리가 적어 둔다. 회계 장부의 기본이기도 하다 - 원장은
  조회 결과가 아니라 **기록**이다.

▶ 결제되면 덮어쓴다, 지우지 않는다
  같은 거래가 미결제로 먼저 들어오고 나중에 확정본으로 온다. 확정본이 도착하면
  같은 (거래일·종목·매매구분)의 미결제 줄을 치우고 확정 줄로 갈음한다. 둘을
  같이 두면 같은 거래가 두 번 세어진다.

▶ 이것은 공식 원장이 아니다
  브로커가 말해 준 값을 시간순으로 적어 둔 것일 뿐이고, 복식부기로 확정하는
  공식 원장은 회계본부(`accounting.*`)다. 응답의 `authoritative: false`가 그 경계다.

자체 점검:
    python apps/api/ledger_store.py
"""
from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
# 비워 두면 저장하지 않는다(테스트·일회성 실행). 기본은 저장소 안의 var/.
DEFAULT_PATH = ROOT / "var" / "accounting_ledger.sqlite3"


def _default_path() -> str:
    configured = os.getenv("ACCOUNTING_LEDGER_DB")
    if configured is not None:
        return configured.strip()
    return str(DEFAULT_PATH)


def row_key(account: str, row: Mapping[str, Any]) -> str:
    """줄의 신원.

    확정분은 브로커 거래번호가 유일 키다. 미결제분은 매매일지가 종목·매매구분
    단위로 합쳐서 주므로 그 조합이 키다 - 같은 날 같은 종목을 또 사면 매매일지가
    한 줄로 합쳐 오기 때문에 그 줄을 갱신하는 것이 맞다.
    """
    trade_date = str(row.get("trade_date") or "")
    if row.get("settlement") == "SETTLED":
        return "|".join([account, "S", trade_date, str(row.get("trade_no") or ""),
                         str(row.get("symbol") or "")])
    return "|".join([account, "U", trade_date, str(row.get("symbol") or ""),
                     str(row.get("category") or "")])


def _unsettled_key(account: str, row: Mapping[str, Any]) -> str:
    """확정본이 갈음할 미결제 줄의 키."""
    return "|".join([account, "U", str(row.get("trade_date") or ""),
                     str(row.get("symbol") or ""), str(row.get("category") or "")])


class LedgerStore:
    """원장 줄을 날짜 범위로 적고 읽는다."""

    def __init__(self, path: str | None = None) -> None:
        self.path = _default_path() if path is None else path.strip()
        if not self.path:
            return
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._session() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS ledger_rows ("
                "row_key TEXT PRIMARY KEY, account TEXT NOT NULL DEFAULT '', "
                "trade_date TEXT NOT NULL, settlement TEXT NOT NULL, "
                "payload TEXT NOT NULL, observed_at TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS ledger_rows_account_date "
                "ON ledger_rows(account, trade_date)"
            )

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, check_same_thread=False)
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def _session(self) -> Iterator[sqlite3.Connection]:
        """트랜잭션 **그리고** close.

        `with sqlite3.connect(...) as conn`은 commit/rollback만 하고 닫지
        않는다(`Connection.__exit__` 계약). 장기 실행 BFF에서 그대로 두면 fd가
        샌다 - `portfolio_store.py`가 같은 이유로 이 형태를 쓴다.
        """
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def record(self, account: str, rows: Iterable[Mapping[str, Any]]) -> int:
        """본 줄을 적어 둔다. 확정본은 같은 거래의 미결제 줄을 갈음한다."""
        if not self.enabled:
            return 0
        observed_at = datetime.now(timezone.utc).isoformat()
        written = 0
        with self._session() as connection:
            for row in rows:
                trade_date = str(row.get("trade_date") or "")
                if not trade_date:
                    continue  # 날짜 없는 줄은 기간 조회에 걸리지 않아 적을 의미가 없다
                settlement = str(row.get("settlement") or "UNSETTLED")
                if settlement == "SETTLED":
                    # 확정본이 왔으면 미결제 줄은 치운다. 남기면 두 번 세어진다.
                    connection.execute(
                        "DELETE FROM ledger_rows WHERE row_key = ?",
                        (_unsettled_key(account, row),),
                    )
                connection.execute(
                    "INSERT INTO ledger_rows"
                    " (row_key, account, trade_date, settlement, payload, observed_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(row_key) DO UPDATE SET"
                    " settlement = excluded.settlement, payload = excluded.payload,"
                    " observed_at = excluded.observed_at",
                    (row_key(account, row), account, trade_date, settlement,
                     json.dumps(dict(row), ensure_ascii=False), observed_at),
                )
                written += 1
        return written

    def read(self, account: str, start: str, end: str) -> list[dict[str, Any]]:
        """기간 안의 줄을 최신순으로 읽는다."""
        if not self.enabled:
            return []
        with self._session() as connection:
            found = connection.execute(
                "SELECT payload FROM ledger_rows"
                " WHERE account = ? AND trade_date BETWEEN ? AND ?"
                " ORDER BY trade_date DESC",
                (account, start, end),
            ).fetchall()
        rows = []
        for record in found:
            try:
                rows.append(json.loads(record["payload"]))
            except (ValueError, TypeError):
                continue  # 못 읽는 줄 하나가 장부 전체를 막지 않는다
        rows.sort(
            key=lambda item: (
                str(item.get("trade_date") or ""),
                str(item.get("trade_time") or ""),
                str(item.get("trade_no") or ""),
            ),
            reverse=True,
        )
        return rows


STORE = LedgerStore()

__all__ = ["LedgerStore", "STORE", "row_key"]


if __name__ == "__main__":  # 자체 점검 - pytest 미도입(CLAUDE.md)
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = LedgerStore(str(Path(tmp) / "ledger.sqlite3"))
        assert store.enabled

        unsettled = {
            "trade_date": "2026-08-18", "trade_time": "14:18:42", "trade_no": None,
            "category": "매도", "symbol": "000660", "symbol_name": "SK하이닉스",
            "commission": "250", "tax": "3340", "settled_amount": "1666410",
            "settlement": "UNSETTLED",
        }
        assert store.record("****5601", [unsettled]) == 1

        # 날짜가 바뀌어도 남아 있어야 한다 - 이 저장소의 존재 이유다
        kept = store.read("****5601", "2026-08-01", "2026-08-31")
        assert len(kept) == 1 and kept[0]["symbol_name"] == "SK하이닉스"
        assert kept[0]["settlement"] == "UNSETTLED"

        # 같은 줄을 다시 봐도 두 줄이 되지 않는다(매매일지는 하루 종일 누적된다)
        store.record("****5601", [{**unsettled, "settled_amount": "1700000"}])
        again = store.read("****5601", "2026-08-01", "2026-08-31")
        assert len(again) == 1 and again[0]["settled_amount"] == "1700000"

        # 결제가 끝나면 확정본이 미결제 줄을 갈음한다 - 같이 두면 두 번 세어진다
        settled = {
            "trade_date": "2026-08-18", "trade_no": "7", "trade_time": "14:18:42",
            "category": "매도", "symbol": "000660", "symbol_name": "SK하이닉스",
            "commission": "250", "tax": "3340", "settled_amount": "1666410",
            "realized_pnl": "4000", "settlement": "SETTLED",
        }
        store.record("****5601", [settled])
        after = store.read("****5601", "2026-08-01", "2026-08-31")
        assert len(after) == 1, after
        assert after[0]["settlement"] == "SETTLED" and after[0]["realized_pnl"] == "4000"

        # 기간 밖은 안 나온다
        assert store.read("****5601", "2026-07-01", "2026-07-31") == []
        # 다른 계좌의 줄이 섞이지 않는다
        assert store.read("****9999", "2026-08-01", "2026-08-31") == []

        # 최신순 - 같은 날이면 시각이 늦은 것이 위로
        store.record("****5601", [
            {**unsettled, "trade_date": "2026-08-17", "symbol": "005930", "category": "매수"},
            {**unsettled, "trade_time": "09:01:00", "symbol": "005930", "category": "매수"},
        ])
        ordered = store.read("****5601", "2026-08-01", "2026-08-31")
        assert [row["trade_date"] for row in ordered] == ["2026-08-18", "2026-08-18", "2026-08-17"]
        assert ordered[0]["trade_time"] == "14:18:42"

        # 날짜 없는 줄은 기간 조회에 안 걸리므로 적지 않는다
        assert store.record("****5601", [{"symbol": "000660", "settlement": "UNSETTLED"}]) == 0

    # 경로가 비면 저장하지 않고 조용히 통과한다(일회성 실행·테스트)
    disabled = LedgerStore("")
    assert not disabled.enabled
    assert disabled.record("a", [{"trade_date": "2026-08-18"}]) == 0
    assert disabled.read("a", "2026-08-01", "2026-08-31") == []

    print("ledger_store 자체 점검 통과")
