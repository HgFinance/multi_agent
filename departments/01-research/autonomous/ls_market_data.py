"""Ephemeral, read-only LS market-data access for Strategy Hermes.

This module is intentionally a narrow boundary around the existing LS REST
transport.  It is not a collector, scheduler, database writer, universe
builder, broker client, or research planner.  A Hermes experiment may query
the allow-listed chart/investor/ranking TRs for the current research turn,
keep the rows in memory (or in the turn's temporary directory), and persist
only the receipt/fingerprint in its lab.

The TR contracts are documented in
``docs/06-integrations/ls-openapi/03-stock/12-12320341.md``.  Raw responses
must never be written under a persistent lab or ``quant-data``.  The caller
owns the temporary directory lifetime; ``StrategyHermesAgent`` removes it in
its ``finally`` path after the Hermes process exits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Mapping


HERE = Path(__file__).resolve().parent
COLLECTORS = HERE.parent / "collectors"
if str(COLLECTORS) not in sys.path:
    sys.path.insert(0, str(COLLECTORS))

from ls_client import LsEnvironment, LsRestClient  # noqa: E402  (local transport boundary)


CHART_PATH = "/stock/chart"
PAGE_SIZE = 500
RATE_LIMIT_PER_SECOND = 1.0
DEFAULT_MAX_PAGES = 200
ALLOWED_TR_CODES = frozenset({
    "t1665", "t8410", "t8411", "t8412", "t8451", "t8452", "t8453",
    "t1441", "t1444", "t1452", "t1463", "t1466", "t1481", "t1482",
    "t1489", "t1492",
})
RANKING_TR_CODES = frozenset({
    "t1441", "t1444", "t1452", "t1463", "t1466", "t1481", "t1482",
    "t1489", "t1492",
})

_DATE_RE = re.compile(r"^\d{8}$")
_SYMBOL_RE = re.compile(r"^[A-Za-z0-9]{6}$")
_CHART_TR = {
    ("daily", False): "t8410",
    ("weekly", False): "t8410",
    ("monthly", False): "t8410",
    ("yearly", False): "t8410",
    ("tick", False): "t8411",
    ("minute", False): "t8412",
    ("daily", True): "t8451",
    ("weekly", True): "t8451",
    ("monthly", True): "t8451",
    ("yearly", True): "t8451",
    ("tick", True): "t8453",
    ("minute", True): "t8452",
}
_PERIOD_GUBUN = {"daily": "2", "weekly": "3", "monthly": "4", "yearly": "5"}


def _date_text(value: str | date, name: str) -> str:
    if isinstance(value, date):
        result = value.strftime("%Y%m%d")
    else:
        result = str(value).strip()
    if not _DATE_RE.fullmatch(result):
        raise ValueError(f"{name} must be YYYYMMDD")
    try:
        date(int(result[:4]), int(result[4:6]), int(result[6:8]))
    except ValueError as exc:
        raise ValueError(f"{name} is not a calendar date") from exc
    return result


def _symbol(value: object) -> str:
    result = str(value or "").strip()
    if not _SYMBOL_RE.fullmatch(result):
        raise ValueError("symbol must be a six-character LS stock code")
    return result


def _positive_int(value: object, name: str, *, maximum: int = 10000) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if result <= 0 or result > maximum:
        raise ValueError(f"{name} must be between 1 and {maximum}")
    return result


def _text(value: object) -> str:
    return str(value or "").strip()


def _rows(response: Mapping[str, Any], tr_code: str) -> list[dict[str, Any]]:
    value = response.get(f"{tr_code}OutBlock1")
    if value is None:
        raise ValueError(f"{tr_code}OutBlock1 is missing from LS response")
    if not isinstance(value, list):
        raise ValueError(f"{tr_code}OutBlock1 must be an array")
    return [dict(row) for row in value if isinstance(row, Mapping)]


def _out_block(response: Mapping[str, Any], tr_code: str) -> Mapping[str, Any]:
    value = response.get(f"{tr_code}OutBlock")
    return value if isinstance(value, Mapping) else {}


def _row_key(row: Mapping[str, Any]) -> str:
    """Stable identity for de-duplication across a repeated continuation page."""

    date_value = _text(row.get("date"))
    time_value = _text(row.get("time"))
    if date_value or time_value:
        return f"{date_value}:{time_value}"
    return hashlib.sha256(
        json.dumps(dict(row), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _row_sort_key(row: Mapping[str, Any]) -> tuple[str, str]:
    return (_text(row.get("date")), _text(row.get("time")))


def _canonical_hash(rows: list[Mapping[str, Any]]) -> str:
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class DataReceipt:
    """Persistable metadata; it deliberately contains no raw market rows."""

    tr_code: str
    symbol: str | None
    start_date: str
    end_date: str
    pages: int
    row_count: int
    data_sha256: str
    first_row_date: str | None
    last_row_date: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": "ls-openapi",
            "path": CHART_PATH,
            "tr_code": self.tr_code,
            "symbol": self.symbol,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "pages": self.pages,
            "row_count": self.row_count,
            "data_sha256": self.data_sha256,
            "first_row_date": self.first_row_date,
            "last_row_date": self.last_row_date,
            "raw_data_persisted": False,
        }


@dataclass(frozen=True)
class MarketDataBatch:
    """Rows for one research turn plus a safe lineage receipt."""

    rows: tuple[dict[str, Any], ...]
    receipt: DataReceipt


@dataclass(frozen=True)
class RankingReceipt:
    """Safe lineage metadata for a point-in-time ranking snapshot."""

    tr_code: str
    as_of: str
    pages: int
    row_count: int
    data_sha256: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "source": "ls-openapi",
            "path": "/stock/high-item",
            "tr_code": self.tr_code,
            "as_of": self.as_of,
            "pages": self.pages,
            "row_count": self.row_count,
            "data_sha256": self.data_sha256,
            "raw_data_persisted": False,
        }


@dataclass(frozen=True)
class RankingBatch:
    """Rows for one ranking snapshot plus a safe lineage receipt."""

    rows: tuple[dict[str, Any], ...]
    receipt: RankingReceipt


class OnDemandMarketDataClient:
    """Allow-listed, read-only LS market-data client for one Hermes turn."""

    def __init__(self, client: Any | None = None, *, max_pages: int = DEFAULT_MAX_PAGES) -> None:
        if client is None:
            if os.environ.get("LS_DATA_ACCESS_MODE", "readonly").strip().lower() != "readonly":
                raise RuntimeError("Strategy Hermes LS access must be readonly")
            # Do not let this adapter load the repository's broad .env. The
            # dedicated service injects only LS credentials/base URLs; keeping
            # this explicit prevents an accidental DB/order credential read.
            ls_env = {
                key: os.environ.get(key, "")
                for key in (
                    "LS_ENV", "LS_APP_KEY", "LS_APP_SECRET_KEY",
                    "LS_APP_KEY_PAPER", "LS_APP_SECRET_KEY_PAPER",
                    "LS_REST_BASE_URL", "LS_REST_BASE_URL_PAPER",
                )
            }
            self._client = LsRestClient(LsEnvironment.from_env(ls_env))
        else:
            self._client = client
        self.max_pages = _positive_int(max_pages, "max_pages", maximum=1000)
        self.last_receipt: DataReceipt | None = None

    def fetch_chart(
        self,
        symbol: str,
        start_date: str | date,
        end_date: str | date,
        *,
        timeframe: str = "daily",
        integrated: bool = True,
        interval: int = 1,
        exchange: str = "U",
        adjusted: bool = True,
    ) -> MarketDataBatch:
        """Fetch daily/weekly/monthly/yearly, minute, or tick bars on demand.

        ``integrated=True`` selects the KRX+NXT ``t8451/t8452/t8453`` family;
        ``False`` selects ``t8410/t8412/t8411``.  ``interval`` is N-minute or
        N-tick for intraday requests.  The response is sorted oldest-first for
        backtesting, while the receipt is the only object intended for lab
        persistence.
        """

        symbol_text = _symbol(symbol)
        start = _date_text(start_date, "start_date")
        end = _date_text(end_date, "end_date")
        if start > end:
            raise ValueError("start_date must not be after end_date")
        timeframe_text = str(timeframe).strip().lower()
        if timeframe_text not in {"daily", "weekly", "monthly", "yearly", "minute", "tick"}:
            raise ValueError("timeframe must be daily, weekly, monthly, yearly, minute, or tick")
        interval_value = _positive_int(interval, "interval", maximum=10000)
        exchange_text = str(exchange or "U").strip().upper()
        if exchange_text not in {"U", "K", "N"}:
            raise ValueError("exchange must be U, K, or N")
        tr_code = _CHART_TR[(timeframe_text, bool(integrated))]
        block: dict[str, Any] = {
            "shcode": symbol_text,
            "qrycnt": PAGE_SIZE,
            "sdate": start,
            "edate": end,
            "cts_date": "",
            "comp_yn": "N",
        }
        if timeframe_text in _PERIOD_GUBUN:
            block["gubun"] = _PERIOD_GUBUN[timeframe_text]
            if tr_code == "t8410":
                block["sujung"] = "Y" if adjusted else "N"
            else:
                block["sujung"] = "Y" if adjusted else "N"
        else:
            block.update({
                "ncnt": interval_value,
                "nday": "0",
                "stime": "000000",
                "etime": "235959",
                "cts_time": "",
            })
        if integrated:
            block["exchgubun"] = exchange_text
        return self._fetch_pages(
            tr_code=tr_code,
            request_block=block,
            start_date=start,
            end_date=end,
            symbol=symbol_text,
            has_time=timeframe_text in {"minute", "tick"},
        )

    def fetch_investor_trend(
        self,
        *,
        market: str,
        upcode: str,
        start_date: str | date,
        end_date: str | date,
        value_mode: str = "1",
        unit: str = "1",
        exchange: str = "U",
    ) -> MarketDataBatch:
        """Fetch t1665 investor-flow chart data without persisting raw rows."""

        start = _date_text(start_date, "start_date")
        end = _date_text(end_date, "end_date")
        if start > end:
            raise ValueError("start_date must not be after end_date")
        market_text = str(market or "").strip()
        upcode_text = str(upcode or "").strip()
        if not market_text or len(market_text) > 1:
            raise ValueError("market must be one LS market code")
        if not upcode_text or len(upcode_text) > 3:
            raise ValueError("upcode must be a one-to-three-character industry code")
        if str(value_mode) not in {"1", "2"}:
            raise ValueError("value_mode must be 1 or 2")
        if str(unit) not in {"1", "2", "3"}:
            raise ValueError("unit must be 1, 2, or 3")
        exchange_text = str(exchange or "U").strip().upper()
        if exchange_text not in {"U", "K", "N"}:
            raise ValueError("exchange must be U, K, or N")
        block = {
            "market": market_text,
            "upcode": upcode_text,
            "gubun2": str(value_mode),
            "gubun3": str(unit),
            "from_date": start,
            "to_date": end,
            "exchgubun": exchange_text,
        }
        return self._fetch_pages(
            tr_code="t1665",
            request_block=block,
            start_date=start,
            end_date=end,
            symbol=None,
            has_time=False,
        )

    def fetch_ranking(
        self,
        tr_code: str,
        request_block: Mapping[str, Any],
        *,
        as_of: str | date | None = None,
    ) -> RankingBatch:
        """Fetch one allow-listed LS market-ranking snapshot on demand.

        ``request_block`` must match the documented ``<TR>InBlock`` fields
        for the selected ranking TR.  Keeping it explicit avoids silently
        choosing a market, exclusion set, or session.  ``idx`` is managed by
        this adapter for continuation pages.  No raw ranking rows are written
        to a persistent research or market-data store.
        """

        code = str(tr_code or "").strip().lower()
        if code not in RANKING_TR_CODES:
            raise ValueError(f"ranking TR code is not allow-listed: {tr_code}")
        if not isinstance(request_block, Mapping):
            raise ValueError("request_block must be a mapping")
        block_name = f"{code}InBlock"
        request = dict(request_block)
        request["idx"] = 0
        as_of_text = _date_text(as_of or date.today(), "as_of")
        rows: list[dict[str, Any]] = []
        seen_rows: set[str] = set()
        seen_idx: set[int] = set()
        tr_cont = "N"
        tr_cont_key = ""
        pages = 0
        for _ in range(self.max_pages):
            pages += 1
            response, headers = self._client.call_tr(
                path="/stock/high-item",
                tr_cd=code,
                in_block={block_name: dict(request)},
                rate_limit_per_sec=RATE_LIMIT_PER_SECOND,
                tr_cont=tr_cont,
                tr_cont_key=tr_cont_key,
                return_headers=True,
            )
            if not isinstance(response, Mapping) or not isinstance(headers, Mapping):
                raise ValueError("LS transport must return (mapping, headers) for continuation")
            page_rows = _rows(response, code)
            for row in page_rows:
                identity = _row_key(row)
                if identity not in seen_rows:
                    seen_rows.add(identity)
                    rows.append(row)
            out_block = _out_block(response, code)
            try:
                next_idx = int(out_block.get("idx") or 0)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{code} response idx is invalid") from exc
            header_more = _text(headers.get("tr_cont")).upper() == "Y"
            next_key = _text(headers.get("tr_cont_key"))
            if not page_rows or not header_more or next_idx <= 0:
                break
            if next_idx in seen_idx or next_idx == int(request.get("idx") or 0):
                raise ValueError(f"LS ranking continuation cursor repeated for {code}")
            seen_idx.add(next_idx)
            request["idx"] = next_idx
            tr_cont, tr_cont_key = "Y", next_key
        else:
            raise ValueError(f"LS ranking continuation exceeded max_pages={self.max_pages}")

        receipt = RankingReceipt(
            tr_code=code,
            as_of=as_of_text,
            pages=pages,
            row_count=len(rows),
            data_sha256=_canonical_hash(rows),
        )
        return RankingBatch(tuple(rows), receipt)

    def _fetch_pages(
        self,
        *,
        tr_code: str,
        request_block: Mapping[str, Any],
        start_date: str,
        end_date: str,
        symbol: str | None,
        has_time: bool,
    ) -> MarketDataBatch:
        if tr_code not in ALLOWED_TR_CODES:
            raise ValueError(f"TR code is not allow-listed: {tr_code}")

        rows: list[dict[str, Any]] = []
        seen_rows: set[str] = set()
        cursor_seen: set[tuple[str, str, str]] = set()
        tr_cont = "N"
        tr_cont_key = ""
        cts_date = ""
        cts_time = ""
        pages = 0
        for _ in range(self.max_pages):
            pages += 1
            block = dict(request_block)
            # t1665 has no cts_date/cts_time request fields. Chart TRs do.
            if tr_code != "t1665":
                block["cts_date"] = cts_date
            if has_time and tr_code != "t1665":
                block["cts_time"] = cts_time
            response, headers = self._client.call_tr(
                path=CHART_PATH,
                tr_cd=tr_code,
                in_block={f"{tr_code}InBlock": block},
                rate_limit_per_sec=RATE_LIMIT_PER_SECOND,
                tr_cont=tr_cont,
                tr_cont_key=tr_cont_key,
                return_headers=True,
            )
            if not isinstance(response, Mapping) or not isinstance(headers, Mapping):
                raise ValueError("LS transport must return (mapping, headers) for continuation")
            page_rows = _rows(response, tr_code)
            for row in page_rows:
                identity = _row_key(row)
                if identity not in seen_rows:
                    seen_rows.add(identity)
                    rows.append(row)

            out_block = _out_block(response, tr_code)
            next_date = _text(out_block.get("cts_date"))
            next_time = _text(out_block.get("cts_time")) if has_time else ""
            header_more = _text(headers.get("tr_cont")).upper() == "Y"
            next_key = _text(headers.get("tr_cont_key"))
            cursor = (next_date, next_time, next_key)
            if not page_rows or not header_more or not (next_date or next_time):
                break
            if cursor in cursor_seen:
                raise ValueError(f"LS continuation cursor repeated for {tr_code}")
            cursor_seen.add(cursor)
            cts_date, cts_time = next_date, next_time
            tr_cont, tr_cont_key = "Y", next_key
        else:
            raise ValueError(f"LS continuation exceeded max_pages={self.max_pages}")

        rows.sort(key=_row_sort_key)
        first = _text(rows[0].get("date")) if rows else None
        last = _text(rows[-1].get("date")) if rows else None
        receipt = DataReceipt(
            tr_code=tr_code,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            pages=pages,
            row_count=len(rows),
            data_sha256=_canonical_hash(rows),
            first_row_date=first or None,
            last_row_date=last or None,
        )
        self.last_receipt = receipt
        return MarketDataBatch(tuple(rows), receipt)


def write_temp_json(batch: MarketDataBatch, filename: str) -> Path:
    """Write raw rows only below the per-turn temporary root.

    This helper refuses to guess a persistent fallback.  The Strategy Hermes
    runtime sets ``STRATEGY_MARKET_DATA_DIR`` to a unique directory and removes
    it after the turn.  Experiments should prefer ``batch.rows`` in memory.
    """

    root_text = os.environ.get("STRATEGY_MARKET_DATA_DIR", "").strip()
    if not root_text:
        raise RuntimeError("STRATEGY_MARKET_DATA_DIR is required for temporary raw data")
    root = Path(root_text).resolve()
    if not root.is_absolute() or root == Path("/") or not str(root).startswith("/tmp/"):
        raise RuntimeError("STRATEGY_MARKET_DATA_DIR must be an isolated directory under /tmp")
    name = Path(filename)
    if name.name != filename or name.suffix.lower() not in {".json", ".jsonl"}:
        raise ValueError("filename must be a simple .json or .jsonl name")
    root.mkdir(parents=True, exist_ok=True)
    target = (root / name.name).resolve()
    if target.parent != root:
        raise ValueError("temporary filename escaped the data root")
    payload = [dict(row) for row in batch.rows]
    target.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return target


__all__ = [
    "ALLOWED_TR_CODES",
    "RANKING_TR_CODES",
    "CHART_PATH",
    "DataReceipt",
    "MarketDataBatch",
    "RankingBatch",
    "RankingReceipt",
    "OnDemandMarketDataClient",
    "write_temp_json",
]
