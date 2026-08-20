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
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

import httpx


KST = ZoneInfo("Asia/Seoul")
_DEFAULT_SUCCESS_CODES = frozenset({"0000", "00000"})
# LS's own CSPAQ12300 example and the production CSPAQ13700 response use
# 00136 with complete OutBlocks and the success message "조회가 완료되었습니다."
# Keep this exception scoped to the observed account-query TR; accepting it
# globally could turn an order rejection into a false acknowledgement.
_TR_SUCCESS_CODES = {"CSPAQ13700": frozenset({"00136"})}
_ORDER_HISTORY_CACHE_SECONDS = 1.1


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
class LSPaperOrderStatus:
    broker_order_id: str
    state: str
    requested_quantity: Decimal
    filled_quantity: Decimal
    fill_price: Decimal | None
    order_date: date

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
        self._client = client or httpx.Client(timeout=config.timeout_seconds)
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
        raw_expiry = body.get("expires_in", body.get("expire_in", 300))
        try:
            expires_in = max(60, int(raw_expiry))
        except (TypeError, ValueError) as exc:
            raise LSPaperBrokerError(
                "LS_PAPER_RESPONSE_INVALID", "LS OAuth expiry is invalid"
            ) from exc
        self._token = token
        self._token_expires_at = now + timedelta(seconds=expires_in)
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
            # Do not include rsp_msg: broker messages may echo account data.
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_REJECTED" if placement else "LS_PAPER_QUERY_REJECTED",
                f"LS PAPER request was rejected (code {rsp_cd})",
            )
        return body

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
        body = self._post_tr(
            "CSPAT00601",
            {
                "CSPAT00601InBlock1": {
                    "IsuNo": "A" + canonical_symbol,
                    "OrdQty": int(quantity),
                    "OrdPrc": str(price),
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
        raw_result = body.get("CSPAT00601OutBlock2")
        if not isinstance(raw_result, dict):
            raise LSPaperBrokerError(
                "LS_PAPER_ORDER_AMBIGUOUS",
                "LS PAPER success response has no order confirmation block",
                ambiguous=True,
            )
        result = raw_result
        order_no = str(result.get("OrdNo") or "").strip()
        if not order_no or order_no == "0":
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
            )
        return None

    def _order_history_rows(self, target_date: date) -> tuple[dict[str, Any], ...]:
        """Read account order history at most once per LS rate-limit window.

        CSPAQ13700 returns the account-wide order list and is limited to one
        request per second.  Caching the one snapshot lets a worker reconcile
        every active directive without starving all but the first order in a
        batch or hammering the broker.
        """

        now = time.monotonic()
        with self._history_cache_lock:
            if (
                self._history_cache_date == target_date
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
    "LSPaperOrderAck",
    "LSPaperOrderStatus",
]
