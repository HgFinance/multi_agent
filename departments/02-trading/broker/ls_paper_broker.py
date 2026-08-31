"""Fail-closed LS Securities PAPER broker adapter.

Only the separately issued PAPER application key is accepted.  The adapter
never falls back to LIVE credentials and refuses to start unless ``LS_ENV`` is
exactly ``PAPER``.  Network ambiguity is surfaced to the directive state
machine; callers must not retry a placement without first reconciling the
broker order history.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import hashlib
from pathlib import Path

# The LS HTTP quirk repair is shared with the Risk adapter and apps/api, which
# already treat this directory as the LS integration path.  Duplicating it here
# would mean one copy gets fixed and the order lane keeps the broken parser.
_LS_INTEGRATIONS = (
    Path(__file__).resolve().parents[2] / "03-risk" / "integrations"
)
if str(_LS_INTEGRATIONS) not in sys.path:
    sys.path.append(str(_LS_INTEGRATIONS))

from ls_http import ls_client  # noqa: E402 - sys.path 조정 뒤


KST = ZoneInfo("Asia/Seoul")
_DEFAULT_SUCCESS_CODES = frozenset({"0000", "00000"})
# LS's own CSPAQ12300 example and the production CSPAQ13700 response use
# 00136 with complete OutBlocks and the success message "조회가 완료되었습니다."
# Keep this exception scoped to the observed account-query TR; accepting it
# globally could turn an order rejection into a false acknowledgement.
_TR_SUCCESS_CODES = {"CSPAQ13700": frozenset({"00136"})}
_ORDER_HISTORY_CACHE_SECONDS = 1.1



_SHARED_TOKEN_TTL_SECONDS = 3600


def _shared_token_cache_path(app_key):
    """Cross-process shared token cache (opt-in via LS_TOKEN_CACHE_DIR).

    Protocol identical to the research collector cache: the file
    ls_token_{ENV}_{sha256(app_key)[:12]}.json holds token + expires_at.
    LS keeps ONE active token per app key, so independent issuers
    invalidate each other (measured 2026-08-24: ~1 websocket kick/min
    while several processes each re-issued on short private TTLs).
    """
    base = os.environ.get("LS_TOKEN_CACHE_DIR", "").strip()
    if not base:
        return None
    mode = os.environ.get("LS_ENV", "PAPER").strip().upper() or "PAPER"
    key_id = hashlib.sha256(app_key.encode()).hexdigest()[:12]
    return Path(base) / f"ls_token_{mode}_{key_id}.json"


def _read_shared_token(path):
    if path is None:
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        expires = datetime.fromisoformat(payload["expires_at"])
        now = datetime.now(timezone.utc)
        if now + timedelta(seconds=60) < expires and payload.get("token"):
            return str(payload["token"]), expires
    except (OSError, KeyError, ValueError):
        return None
    return None


def _write_shared_token(path, token, expires_at):
    if path is None:
        return
    try:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"token": token,
                        "expires_at": expires_at.isoformat()}),
            encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


class LSPaperBrokerError(RuntimeError):
    """Stable broker error that does not expose credentials or raw payloads."""

    def __init__(self, code: str, message: str, *, ambiguous: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class LSPaperBrokerConfig:
    base_url: str
    app_key: str
    app_secret_key: str
    mac_address: str | None = None
    scope: str = "oob"
    timeout_seconds: float = 8.0

    @classmethod
    def from_env(cls) -> "LSPaperBrokerConfig":
        if os.environ.get("LS_ENV", "").strip().upper() != "PAPER":
            raise LSPaperBrokerError(
                "LS_PAPER_ENV_REQUIRED",
                "LS PAPER adapter requires LS_ENV=PAPER",
            )
        # Deliberately no fallback to LS_APP_KEY / LS_APP_SECRET_KEY.  Those
        # names are the LIVE credentials in this deployment.
        app_key = os.environ.get("LS_APP_KEY_PAPER", "").strip()
        app_secret = os.environ.get("LS_APP_SECRET_KEY_PAPER", "").strip()
        base_url = (
            os.environ.get("LS_REST_BASE_URL_PAPER", "").strip()
            or os.environ.get("LS_REST_BASE_URL", "").strip()
            or "https://openapi.ls-sec.co.kr:8080"
        )
        if not app_key or not app_secret:
            raise LSPaperBrokerError(
                "LS_PAPER_CREDENTIALS_REQUIRED",
                "LS PAPER AppKey and SecretKey are required",
            )
        try:
            timeout = float(os.environ.get("LS_PAPER_ORDER_TIMEOUT_SECONDS", "8"))
        except ValueError as exc:
            raise LSPaperBrokerError(
                "LS_PAPER_CONFIG_INVALID", "LS PAPER order timeout is invalid"
            ) from exc
        if not 1 <= timeout <= 30:
            raise LSPaperBrokerError(
                "LS_PAPER_CONFIG_INVALID", "LS PAPER order timeout is outside bounds"
            )
        raw_mac = os.environ.get("LS_MAC_ADDRESS", "").strip()
        mac_address = raw_mac.replace(":", "").replace("-", "").upper()
        if len(mac_address) != 12 or any(
            character not in "0123456789ABCDEF" for character in mac_address
        ):
            raise LSPaperBrokerError(
                "LS_PAPER_MAC_REQUIRED",
                "LS PAPER order adapter requires a 12-digit MAC address",
            )
        return cls(
            base_url=base_url.rstrip("/"),
            app_key=app_key,
            app_secret_key=app_secret,
            mac_address=mac_address,
            scope=os.environ.get("LS_OAUTH_SCOPE", "oob").strip() or "oob",
            timeout_seconds=timeout,
        )


@dataclass(frozen=True)
class LSPaperOrderAck:
    broker_order_id: str
    order_time: str | None
    symbol: str


@dataclass(frozen=True)
class LSPaperHolding:
    """One holding from the current LS PAPER account snapshot."""

    symbol: str
    name: str | None
    quantity: Decimal
    sellable_quantity: Decimal


@dataclass(frozen=True)
class LSPaperOrderStatus:
    broker_order_id: str
    state: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    fill_price: Decimal | None
    order_date: date
    last_execution_at: datetime | None = None

    @property
    def leaves_quantity(self) -> Decimal:
        return max(Decimal(0), self.requested_quantity - self.filled_quantity)


def _decimal(value: Any, field: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise LSPaperBrokerError(
            "LS_PAPER_RESPONSE_INVALID", f"LS response {field} is invalid"
        ) from exc
    if not parsed.is_finite():
        raise LSPaperBrokerError(
            "LS_PAPER_RESPONSE_INVALID", f"LS response {field} is invalid"
        )
    return parsed


def _object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LSPaperBrokerError(
            "LS_PAPER_RESPONSE_INVALID", f"LS response {field} is missing"
        )
    return value


class LSPaperBroker:
    """Synchronous adapter used by the deterministic directive service."""

    def __init__(
        self,
        config: LSPaperBrokerConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        # Not a bare httpx.Client: LS pads `tr_cont_key` with NUL and h11 throws
        # the whole response away.  Rationale lives in `ls_http`'s docstring.
        self._client = client or ls_client(timeout=config.timeout_seconds)
        self._token: str | None = None
        self._token_expires_at = datetime.min.replace(tzinfo=timezone.utc)
        self._history_cache_lock = threading.Lock()
        self._history_cache_date: date | None = None
        self._history_cache_at = 0.0
        self._history_cache_rows: tuple[dict[str, Any], ...] = ()

    @classmethod
    def from_env(cls) -> "LSPaperBroker":
        return cls(LSPaperBrokerConfig.from_env())

    def close(self) -> None:
        self._client.close()

    def _access_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and now + timedelta(seconds=30) < self._token_expires_at:
            return self._token
        shared = _read_shared_token(
            _shared_token_cache_path(self.config.app_key))
        if shared is not None:
            self._token, self._token_expires_at = shared
            return self._token
        try:
            response = self._client.post(
                self.config.base_url + "/oauth2/token",
                data={
                    "grant_type": "client_credentials",
                    "appkey": self.config.app_key,
                    "appsecretkey": self.config.app_secret_key,
                    "scope": self.config.scope,
                },
                headers={"content-type": "application/x-www-form-urlencoded"},
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise LSPaperBrokerError(
                "LS_PAPER_AUTH_REJECTED", "LS rejected PAPER authentication"
            ) from exc
        except httpx.HTTPError as exc:
            raise LSPaperBrokerError(
                "LS_PAPER_AUTH_UNAVAILABLE",
                "LS PAPER authentication transport failed",
            ) from exc
        try:
            body = response.json()
        except ValueError as exc:
            raise LSPaperBrokerError(
                "LS_PAPER_RESPONSE_INVALID", "LS OAuth response was not JSON"
            ) from exc
        token = body.get("access_token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token:
            raise LSPaperBrokerError(
                "LS_PAPER_RESPONSE_INVALID", "LS OAuth response has no access token"
            )
        # unified 1h TTL - see _shared_token_cache_path docstring
        self._token = token
        self._token_expires_at = now + timedelta(
            seconds=_SHARED_TOKEN_TTL_SECONDS)
        _write_shared_token(
            _shared_token_cache_path(self.config.app_key),
            token, self._token_expires_at)
        return token

    def _post_tr(
        self,
        tr_code: str,
        payload: dict[str, Any],
        *,
        path: str,
        placement: bool = False,
    ) -> dict[str, Any]:
        headers = {
            "content-type": "application/json; charset=UTF-8",
            "authorization": "Bearer " + self._access_token(),
            "tr_cd": tr_code,
            "tr_cont": "N",
            "tr_cont_key": "",
        }
        if self.config.mac_address:
            headers["mac_address"] = self.config.mac_address
        try:
            response = self._client.post(
                self.config.base_url + path,
                json=payload,
                headers=headers,
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            # A response status proves the broker answered.  4xx is a
            # deterministic rejection; 5xx can occur after submission.
            ambiguous = placement and (
                exc.response.status_code >= 500 or exc.response.status_code == 408
            )
            code = "LS_PAPER_ORDER_AMBIGUOUS" if ambiguous else "LS_PAPER_ORDER_REJECTED"
            raise LSPaperBrokerError(
                code,
                f"LS PAPER request returned HTTP {exc.response.status_code}",
                ambiguous=ambiguous,
            ) from exc
        except httpx.HTTPError as exc:
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_AMBIGUOUS" if placement else "LS_PAPER_QUERY_UNAVAILABLE",
                "LS PAPER request transport failed",
                ambiguous=placement,
            ) from exc
        try:
            body = json.loads(response.text, parse_float=Decimal)
        except (ValueError, TypeError) as exc:
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_AMBIGUOUS" if placement else "LS_PAPER_RESPONSE_INVALID",
                "LS PAPER response was not valid JSON",
                ambiguous=placement,
            ) from exc
        if not isinstance(body, dict):
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_AMBIGUOUS" if placement else "LS_PAPER_RESPONSE_INVALID",
                "LS PAPER response must be an object",
                ambiguous=placement,
            )
        rsp_cd = str(body.get("rsp_cd") or "").strip()
        success_codes = _DEFAULT_SUCCESS_CODES | _TR_SUCCESS_CODES.get(
            tr_code, frozenset()
        )
        if rsp_cd and rsp_cd not in success_codes:
            # LS PAPER has been observed returning rsp_cd=00039 after it had
            # already accepted and filled CSPAT00601. A non-success body after
            # the mutating request crossed the broker boundary is therefore
            # not proof of rejection. If LS still supplied a non-zero order
            # number it is authoritative; otherwise the outcome is ambiguous
            # and must be reconciled read-only before any new placement.
            confirmation = body.get(f"{tr_code}OutBlock2")
            confirmed_order_no = (
                str(confirmation.get("OrdNo") or "").strip()
                if isinstance(confirmation, dict)
                else ""
            )
            if placement and confirmed_order_no not in {"", "0"}:
                return body
            # Do not include rsp_msg: broker messages may echo account data.
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_AMBIGUOUS" if placement else "LS_PAPER_QUERY_REJECTED",
                f"LS PAPER request was rejected (code {rsp_cd})",
                ambiguous=placement,
            )
        return body

    def get_quote(self, symbol: str) -> dict[str, Any]:
        """Read a fresh PAPER L1 quote for order admission fallback."""
        normalized = str(symbol or "").strip()
        if not normalized:
            raise LSPaperBrokerError("LS_PAPER_QUOTE_INVALID", "symbol is required")
        body = self._post_tr(
            "t1101",
            {"t1101InBlock": {"shcode": normalized}},
            path="/stock/market-data",
        )
        block = _object(body.get("t1101OutBlock"), "t1101OutBlock")
        return {
            "symbol": normalized,
            "observed_at": datetime.now(timezone.utc),
            "bid": _decimal(block.get("bidho1"), "bidho1"),
            "ask": _decimal(block.get("offerho1"), "offerho1"),
            "bid_size": _decimal(block.get("bidrem1"), "bidrem1"),
            "ask_size": _decimal(block.get("offerrem1"), "offerrem1"),
        }

    def get_holdings(self) -> tuple[LSPaperHolding, ...]:
        """Read executed and sellable holdings from the LS PAPER account."""
        body = self._post_tr(
            "t0424",
            {
                "t0424InBlock": {
                    "prcgb": "1",
                    "chegb": "2",
                    "dangb": "0",
                    "charge": "1",
                    "cts_expcode": "",
                }
            },
            path="/stock/accno",
        )
        raw_rows = body.get("t0424OutBlock1")
        if not isinstance(raw_rows, list) or any(
            not isinstance(row, dict) for row in raw_rows
        ):
            raise LSPaperBrokerError(
                "LS_PAPER_RESPONSE_INVALID",
                "LS PAPER holdings rows are invalid",
            )

        holdings: list[LSPaperHolding] = []
        seen_symbols: set[str] = set()
        for row in raw_rows:
            raw_symbol = str(row.get("expcode") or row.get("symbol") or "")
            symbol = raw_symbol.strip().removeprefix("A")
            if len(symbol) != 6 or not symbol.isdigit():
                raise LSPaperBrokerError(
                    "LS_PAPER_RESPONSE_INVALID",
                    "LS PAPER holdings contain an invalid instrument symbol",
                )
            if symbol in seen_symbols:
                raise LSPaperBrokerError(
                    "LS_PAPER_RESPONSE_INVALID",
                    "LS PAPER holdings contain a duplicate instrument symbol",
                )
            seen_symbols.add(symbol)
            quantity = _decimal(row.get("janqty"), "janqty")
            sellable_quantity = _decimal(row.get("mdposqt"), "mdposqt")
            if (
                quantity < 0
                or sellable_quantity < 0
                or quantity != quantity.to_integral_value()
                or sellable_quantity != sellable_quantity.to_integral_value()
                or sellable_quantity > quantity
            ):
                raise LSPaperBrokerError(
                    "LS_PAPER_RESPONSE_INVALID",
                    "LS PAPER holdings contain an invalid quantity",
                )
            raw_name = row.get("hname")
            name = str(raw_name).strip() if raw_name is not None else None
            holdings.append(
                LSPaperHolding(
                    symbol=symbol,
                    name=name or None,
                    quantity=quantity,
                    sellable_quantity=sellable_quantity,
                )
            )
        return tuple(holdings)

    @staticmethod
    def _row_order_datetime(row: dict[str, Any], order_day: date) -> datetime | None:
        return LSPaperBroker._row_datetime(row.get("OrdTime"), order_day)

    @staticmethod
    def _row_datetime(value: Any, order_day: date) -> datetime | None:
        digits = "".join(
            character
            for character in str(value or "")
            if character.isdigit()
        )
        if len(digits) < 6:
            return None
        try:
            return datetime(
                order_day.year,
                order_day.month,
                order_day.day,
                int(digits[0:2]),
                int(digits[2:4]),
                int(digits[4:6]),
                tzinfo=KST,
            )
        except ValueError:
            return None

    def _recover_recent_placement(
        self,
        *,
        submitted_after: datetime,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> LSPaperOrderAck | None:
        """Match one exact recent order via CSPAQ13700 without resubmitting.

        A match is accepted only when it is unique by symbol, side, quantity,
        price, and a bounded broker timestamp. Simultaneous identical HTS/API
        orders deliberately remain ambiguous rather than being misattributed.
        """

        observed_until = datetime.now(KST) + timedelta(seconds=3)
        observed_after = submitted_after.astimezone(KST) - timedelta(seconds=3)
        expected_side = "2" if side == "BUY" else "1"
        order_day = submitted_after.astimezone(KST).date()
        for attempt in range(2):
            if attempt:
                # CSPAQ13700 is rate-limited to one request per second. This
                # second read handles broker-history propagation lag and is
                # still reconciliation only; CSPAT00601 is never called again.
                time.sleep(_ORDER_HISTORY_CACHE_SECONDS)
            try:
                rows = self._order_history_rows(order_day, refresh=True)
            except LSPaperBrokerError:
                return None

            matches: list[tuple[str, str | None]] = []
            for row in rows:
                order_no = str(row.get("OrdNo") or "").strip()
                row_symbol = str(row.get("IsuNo") or "").strip().removeprefix("A")
                row_time = self._row_order_datetime(row, order_day)
                try:
                    row_quantity = _decimal(row.get("OrdQty") or 0, "OrdQty")
                    row_price = _decimal(row.get("OrdPrc") or 0, "OrdPrc")
                except LSPaperBrokerError:
                    continue
                if (
                    order_no in {"", "0"}
                    or row_symbol != symbol
                    or str(row.get("BnsTpCode") or "").strip() != expected_side
                    or row_quantity != quantity
                    or row_price != price
                    or row_time is None
                    or not observed_after <= row_time <= observed_until
                ):
                    continue
                matches.append(
                    (order_no, str(row.get("OrdTime") or "").strip() or None)
                )
            if len(matches) == 1:
                return LSPaperOrderAck(
                    broker_order_id=matches[0][0],
                    order_time=matches[0][1],
                    symbol=symbol,
                )
            if len(matches) > 1:
                return None
        return None

    def place_order(
        self,
        *,
        symbol: str,
        side: str,
        order_type: str,
        quantity: Decimal,
        limit_price: Decimal | None,
    ) -> LSPaperOrderAck:
        canonical_symbol = symbol.strip().removeprefix("A")
        if len(canonical_symbol) != 6 or not canonical_symbol.isdigit():
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_INVALID", "LS PAPER order symbol must be six digits"
            )
        if quantity <= 0 or quantity != quantity.to_integral_value():
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_INVALID", "LS PAPER order quantity must be a positive integer"
            )
        normalized_side = side.strip().upper()
        if normalized_side not in {"BUY", "SELL"}:
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_INVALID", "LS PAPER order side is invalid"
            )
        normalized_type = order_type.strip().upper()
        if normalized_type not in {"MARKET", "LIMIT"}:
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_INVALID", "LS PAPER order type is invalid"
            )
        if normalized_type == "LIMIT" and (limit_price is None or limit_price <= 0):
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_INVALID", "LS PAPER limit order requires a positive price"
            )
        price = Decimal(0) if normalized_type == "MARKET" else Decimal(limit_price)
        # CSPAT00601 declares OrdPrc as a JSON Number (13.2).  Sending the
        # textual form (for example ``"0"``) is tolerated by mocks but the LS
        # PAPER order gateway can answer HTTP 500 before producing an order.
        # Preserve integral prices as JSON integers and use a JSON float only
        # for the contract's two-decimal fractional form.
        wire_price: int | float = (
            int(price) if price == price.to_integral_value() else float(price)
        )
        submitted_after = datetime.now(KST)
        try:
            body = self._post_tr(
                "CSPAT00601",
                {
                    "CSPAT00601InBlock1": {
                        "IsuNo": "A" + canonical_symbol,
                        "OrdQty": int(quantity),
                        "OrdPrc": wire_price,
                        "BnsTpCode": "2" if normalized_side == "BUY" else "1",
                        "OrdprcPtnCode": "03" if normalized_type == "MARKET" else "00",
                        "MgntrnCode": "000",
                        "LoanDt": "",
                        "OrdCndiTpCode": "0",
                        "MbrNo": "",
                    }
                },
                path="/stock/order",
                placement=True,
            )
        except LSPaperBrokerError as exc:
            if not exc.ambiguous:
                raise
            recovered = self._recover_recent_placement(
                submitted_after=submitted_after,
                symbol=canonical_symbol,
                side=normalized_side,
                quantity=quantity,
                price=price,
            )
            if recovered is not None:
                return recovered
            raise
        raw_result = body.get("CSPAT00601OutBlock2")
        if not isinstance(raw_result, dict):
            recovered = self._recover_recent_placement(
                submitted_after=submitted_after,
                symbol=canonical_symbol,
                side=normalized_side,
                quantity=quantity,
                price=price,
            )
            if recovered is not None:
                return recovered
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_AMBIGUOUS",
                "LS PAPER success response has no order confirmation block",
                ambiguous=True,
            )
        result = raw_result
        order_no = str(result.get("OrdNo") or "").strip()
        if not order_no or order_no == "0":
            recovered = self._recover_recent_placement(
                submitted_after=submitted_after,
                symbol=canonical_symbol,
                side=normalized_side,
                quantity=quantity,
                price=price,
            )
            if recovered is not None:
                return recovered
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_AMBIGUOUS",
                "LS PAPER order response has no broker order number",
                ambiguous=True,
            )
        return LSPaperOrderAck(
            broker_order_id=order_no,
            order_time=str(result.get("OrdTime") or "").strip() or None,
            symbol=canonical_symbol,
        )

    def order_status(
        self,
        broker_order_id: str,
        *,
        order_date: date | None = None,
    ) -> LSPaperOrderStatus | None:
        target_date = order_date or datetime.now(KST).date()
        rows = self._order_history_rows(target_date)
        wanted = str(broker_order_id).lstrip("0") or "0"
        for candidate in rows:
            if not isinstance(candidate, dict):
                continue
            current = str(candidate.get("OrdNo") or "").strip()
            if (current.lstrip("0") or "0") != wanted:
                continue
            requested = _decimal(candidate.get("OrdQty") or 0, "OrdQty")
            filled = _decimal(
                candidate.get("AllExecQty") or candidate.get("ExecQty") or 0,
                "AllExecQty",
            )
            price_raw = candidate.get("ExecPrc")
            fill_price = _decimal(price_raw, "ExecPrc") if price_raw not in (None, "", 0, "0") else None
            description = " ".join(
                str(candidate.get(name) or "")
                for name in ("OrdTrxPtnNm", "MrcTpNm", "OrdPtnNm")
            )
            if "거부" in description:
                state = "REJECTED"
            elif "취소" in description and filled < requested:
                state = "CANCELLED"
            elif requested > 0 and filled >= requested:
                state = "FILLED"
            elif filled > 0:
                state = "PARTIALLY_FILLED"
            else:
                state = "ACKNOWLEDGED"
            return LSPaperOrderStatus(
                broker_order_id=current,
                state=state,
                requested_quantity=requested,
                filled_quantity=filled,
                fill_price=fill_price,
                order_date=target_date,
                last_execution_at=(
                    self._row_datetime(
                        candidate.get("LastExecTime")
                        or candidate.get("ExecTrxTime")
                        or candidate.get("OrdTime"),
                        target_date,
                    )
                    if filled > 0
                    else None
                ),
            )
        return None

    def _order_history_rows(
        self, target_date: date, *, refresh: bool = False
    ) -> tuple[dict[str, Any], ...]:
        """Read account order history at most once per LS rate-limit window.

        CSPAQ13700 returns the account-wide order list and is limited to one
        request per second.  Caching the one snapshot lets a worker reconcile
        every active directive without starving all but the first order in a
        batch or hammering the broker.
        """

        now = time.monotonic()
        with self._history_cache_lock:
            if (
                not refresh
                and self._history_cache_date == target_date
                and now - self._history_cache_at < _ORDER_HISTORY_CACHE_SECONDS
            ):
                return self._history_cache_rows
            body = self._post_tr(
                "CSPAQ13700",
                {
                    "CSPAQ13700InBlock1": {
                        "OrdMktCode": "00",
                        "BnsTpCode": "0",
                        "IsuNo": "",
                        "ExecYn": "0",
                        "OrdDt": target_date.strftime("%Y%m%d"),
                        "SrtOrdNo2": 0,
                        "BkseqTpCode": "0",
                        "OrdPtnCode": "00",
                    }
                },
                path="/stock/accno",
            )
            raw_rows = body.get("CSPAQ13700OutBlock3")
            if isinstance(raw_rows, dict):
                raw_rows = [raw_rows]
            if not isinstance(raw_rows, list) or any(
                not isinstance(row, dict) for row in raw_rows
            ):
                raise LSPaperBrokerError(
                    "LS_PAPER_RESPONSE_INVALID", "LS order history rows are invalid"
                )
            rows = tuple(dict(row) for row in raw_rows)
            self._history_cache_date = target_date
            self._history_cache_at = time.monotonic()
            self._history_cache_rows = rows
            return rows

    def cancel_order(
        self,
        *,
        broker_order_id: str,
        symbol: str,
        quantity: Decimal,
    ) -> LSPaperOrderAck:
        if quantity <= 0 or quantity != quantity.to_integral_value():
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_INVALID", "LS PAPER cancel quantity must be a positive integer"
            )
        canonical_symbol = symbol.strip().removeprefix("A")
        body = self._post_tr(
            "CSPAT00801",
            {
                "CSPAT00801InBlock1": {
                    "OrgOrdNo": int(broker_order_id),
                    "IsuNo": "A" + canonical_symbol,
                    "OrdQty": int(quantity),
                }
            },
            path="/stock/order",
            placement=True,
        )
        result = _object(body.get("CSPAT00801OutBlock2"), "CSPAT00801OutBlock2")
        order_no = str(result.get("OrdNo") or "").strip()
        if not order_no or order_no == "0":
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_AMBIGUOUS",
                "LS PAPER cancel response has no broker order number",
                ambiguous=True,
            )
        return LSPaperOrderAck(
            broker_order_id=order_no,
            order_time=str(result.get("OrdTime") or "").strip() or None,
            symbol=canonical_symbol,
        )


__all__ = [
    "LSPaperBroker",
    "LSPaperBrokerConfig",
    "LSPaperBrokerError",
    "LSPaperHolding",
    "LSPaperOrderAck",
    "LSPaperOrderStatus",
]
