"""Fail-closed, read-only LS증권 Open API adapter.

The adapter deliberately exposes no order endpoint. It consumes only market
quote and account/position data, validates the response shape, and leaves the
RiskEngine responsible for all binding decisions.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx


class LSOpenAPIError(RuntimeError):
    """Base error for unavailable or invalid broker data."""


class LSOpenAPIConfigurationError(LSOpenAPIError):
    """Raised when a non-test environment has incomplete credentials."""


@dataclass(frozen=True)
class LSOpenAPIConfig:
    environment: str
    base_url: str
    app_key: str
    app_secret_key: str
    account_no: str | None = None
    account_password: str | None = None
    mac_address: str | None = None
    scope: str = "oob"
    timeout_seconds: float = 5.0

    @classmethod
    def from_env(cls) -> LSOpenAPIConfig:
        environment = os.environ.get("LS_ENV", "PAPER").strip().upper()
        if environment not in {"PAPER", "LIVE"}:
            raise LSOpenAPIConfigurationError("LS_ENV must be PAPER or LIVE")
        suffix = "_PAPER" if environment == "PAPER" else ""
        app_key = os.environ.get(f"LS_APP_KEY{suffix}", "").strip()
        app_secret_key = os.environ.get(f"LS_APP_SECRET_KEY{suffix}", "").strip()
        base_url = os.environ.get(f"LS_REST_BASE_URL{suffix}", "").strip()
        if not base_url:
            base_url = os.environ.get("LS_REST_BASE_URL", "").strip()
        missing = [
            name
            for name, value in {
                "LS_APP_KEY": app_key,
                "LS_APP_SECRET_KEY": app_secret_key,
                "LS_REST_BASE_URL": base_url,
            }.items()
            if not value
        ]
        if missing:
            raise LSOpenAPIConfigurationError(
                f"LS {environment} credential configuration is incomplete: {', '.join(missing)}"
            )
        return cls(
            environment=environment,
            base_url=base_url.rstrip("/"),
            app_key=app_key,
            app_secret_key=app_secret_key,
            account_no=os.environ.get(f"LS_ACCOUNT_NO{suffix}") or None,
            account_password=os.environ.get(f"LS_ACCOUNT_PWD{suffix}") or None,
            mac_address=os.environ.get("LS_MAC_ADDRESS") or None,
            scope=os.environ.get("LS_OAUTH_SCOPE", "oob"),
            timeout_seconds=float(os.environ.get("RISK_EXTERNAL_API_TIMEOUT_SECONDS", "5")),
        )


@dataclass(frozen=True)
class MarketQuote:
    symbol: str
    price: Decimal
    bid: Decimal | None
    ask: Decimal | None
    observed_at: datetime
    source: str = "ls-openapi"


@dataclass(frozen=True)
class PortfolioSnapshot:
    account_no: str | None
    cash: Decimal
    buying_power: Decimal
    equity: Decimal
    positions: tuple[Mapping[str, Any], ...]
    observed_at: datetime
    source: str = "ls-openapi"


def credential_status(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Return key names and presence only; never return secret values."""

    env = os.environ if environ is None else environ
    requested = env.get("LS_ENV", "PAPER").strip().upper()
    suffix = "_PAPER" if requested == "PAPER" else ""
    names = (
        f"LS_APP_KEY{suffix}",
        f"LS_APP_SECRET_KEY{suffix}",
        f"LS_REST_BASE_URL{suffix}",
    )
    present = {name: bool(env.get(name, "").strip()) for name in names}
    return {
        "provider": "ls-openapi",
        "environment": requested,
        "configured": all(present.values()),
        "present": present,
        "secret_values_exposed": False,
    }


class LSOpenAPIClient:
    """Small read-only client for documented LS market/account TRs."""

    def __init__(
        self,
        config: LSOpenAPIConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self._client = client or httpx.Client(timeout=config.timeout_seconds)
        self._token: str | None = None
        self._token_expires_at = datetime.min.replace(tzinfo=timezone.utc)

    @classmethod
    def from_env(cls) -> LSOpenAPIClient:
        return cls(LSOpenAPIConfig.from_env())

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def _access_token(self) -> str:
        now = datetime.now(timezone.utc)
        if self._token and now + timedelta(seconds=30) < self._token_expires_at:
            return self._token
        response = self._client.post(
            f"{self.config.base_url}/oauth2/token",
            data={
                "grant_type": "client_credentials",
                "appkey": self.config.app_key,
                "appsecretkey": self.config.app_secret_key,
                "scope": self.config.scope,
            },
            headers={"content-type": "application/x-www-form-urlencoded"},
        )
        response.raise_for_status()
        body = _json_object(response)
        token = body.get("access_token")
        if not isinstance(token, str) or not token:
            raise LSOpenAPIError("LS OAuth response did not contain access_token")
        try:
            expires_in = max(60, int(body.get("expire_in", 300)))
        except (TypeError, ValueError) as exc:
            raise LSOpenAPIError("LS OAuth expire_in is invalid") from exc
        self._token = token
        self._token_expires_at = now + timedelta(seconds=expires_in)
        return token

    def _post_tr(self, path: str, tr_code: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        headers = {
            "content-type": "application/json; charset=UTF-8",
            "authorization": f"Bearer {self._access_token()}",
            "tr_cd": tr_code,
            "tr_cont": "N",
            "tr_cont_key": "",
        }
        if self.config.mac_address:
            headers["mac_address"] = self.config.mac_address
        response = self._client.post(f"{self.config.base_url}{path}", json=payload, headers=headers)
        response.raise_for_status()
        return _json_object(response)

    def get_quote(self, symbol: str) -> MarketQuote:
        symbol = symbol.strip()
        if not symbol:
            raise LSOpenAPIError("symbol is required")
        body = self._post_tr(
            "/stock/market-data",
            "t1102",
            {"t1102InBlock": {"shcode": symbol, "exchgubun": "U"}},
        )
        block = _object(body.get("t1102OutBlock"), "t1102OutBlock")
        price = _decimal(block.get("price"), "price")
        return MarketQuote(
            symbol=symbol,
            price=price,
            bid=_optional_decimal(block.get("bidho1")),
            ask=_optional_decimal(block.get("offerho1")),
            observed_at=datetime.now(timezone.utc),
        )

    def get_portfolio_snapshot(self) -> PortfolioSnapshot:
        positions_body = self._post_tr(
            "/stock/accno",
            "t0424",
            {
                "t0424InBlock": {
                    "prcgb": "1",
                    "chegb": "0",
                    "dangb": "0",
                    "charge": "1",
                    "cts_expcode": "",
                }
            },
        )
        balance_body = self._post_tr(
            "/stock/accno",
            "CSPAQ12200",
            {"CSPAQ12200InBlock1": {"BalCreTp": "0"}},
        )
        position_block = positions_body.get("t0424OutBlock1", [])
        if not isinstance(position_block, list):
            raise LSOpenAPIError("LS t0424OutBlock1 must be an array")
        balance = _object(balance_body.get("CSPAQ12200OutBlock2"), "CSPAQ12200OutBlock2")
        observed_at = datetime.now(timezone.utc)
        return PortfolioSnapshot(
            account_no=self.config.account_no,
            cash=_decimal(balance.get("Dps"), "Dps"),
            buying_power=_decimal(balance.get("MnyOrdAbleAmt"), "MnyOrdAbleAmt"),
            equity=_decimal(positions_body.get("t0424OutBlock", {}).get("sunamt"), "sunamt"),
            positions=tuple(position_block),
            observed_at=observed_at,
        )


def _json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        body = response.json()
    except ValueError as exc:
        raise LSOpenAPIError("LS response was not valid JSON") from exc
    if not isinstance(body, dict):
        raise LSOpenAPIError("LS response must be a JSON object")
    return body


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LSOpenAPIError(f"LS response field {name} is missing or invalid")
    return value


def _decimal(value: Any, name: str) -> Decimal:
    if value is None or value == "":
        raise LSOpenAPIError(f"LS response field {name} is missing")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise LSOpenAPIError(f"LS response field {name} is not numeric") from exc
    if not result.is_finite():
        raise LSOpenAPIError(f"LS response field {name} is not finite")
    return result


def _optional_decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    return _decimal(value, "optional_quote")
