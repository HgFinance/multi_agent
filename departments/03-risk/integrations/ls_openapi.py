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
import hashlib
import json
from pathlib import Path

try:  # imported as a package by the Risk runtime, flat by apps/api's sys.path
    from .ls_http import ls_client
except ImportError:  # pragma: no cover - flat import path
    from ls_http import ls_client  # type: ignore[no-redef]


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
        environment = os.environ.get("LS_ENV", "LIVE").strip().upper()
        if environment not in {"PAPER", "LIVE"}:
            raise LSOpenAPIConfigurationError("LS environment must be PAPER or LIVE")
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
            timeout_seconds=float(
                os.environ.get("RISK_EXTERNAL_API_TIMEOUT_SECONDS", "5")
            ),
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
    requested = env.get("LS_ENV", "LIVE").strip().upper()
    suffix = "_PAPER" if requested == "PAPER" else ""
    app_key_name = f"LS_APP_KEY{suffix}"
    app_secret_name = f"LS_APP_SECRET_KEY{suffix}"
    rest_name = f"LS_REST_BASE_URL{suffix}"
    rest_present = bool(env.get(rest_name, "").strip())
    # LS issues PAPER app-key tokens on the shared :8080 REST domain.
    generic_rest_present = bool(env.get("LS_REST_BASE_URL", "").strip())
    present = {
        app_key_name: bool(env.get(app_key_name, "").strip()),
        app_secret_name: bool(env.get(app_secret_name, "").strip()),
        rest_name: rest_present,
    }
    if suffix and not rest_present:
        present["LS_REST_BASE_URL"] = generic_rest_present
    return {
        "provider": "ls-openapi",
        "environment": requested,
        "configured": present[app_key_name]
        and present[app_secret_name]
        and (rest_present or generic_rest_present),
        "present": present,
        "secret_values_exposed": False,
    }



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


class LSOpenAPIClient:
    """Small read-only client for documented LS market/account TRs."""

    def __init__(
        self,
        config: LSOpenAPIConfig,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        # Not a bare httpx.Client: LS pads `tr_cont_key` with NUL and h11 throws
        # the whole response away.  Rationale lives in `ls_http`'s docstring.
        self._client = client or ls_client(timeout=config.timeout_seconds)
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
        shared = _read_shared_token(
            _shared_token_cache_path(self.config.app_key))
        if shared is not None:
            self._token, self._token_expires_at = shared
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
        # unified 1h TTL: the response carries no reliable expiry
        # field and short per-process TTLs caused cross-process
        # token thrashing - see _shared_token_cache_path
        self._token = token
        self._token_expires_at = now + timedelta(
            seconds=_SHARED_TOKEN_TTL_SECONDS)
        _write_shared_token(
            _shared_token_cache_path(self.config.app_key),
            token, self._token_expires_at)
        return token

    def _post_tr(
        self, path: str, tr_code: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        headers = {
            "content-type": "application/json; charset=UTF-8",
            "authorization": f"Bearer {self._access_token()}",
            "tr_cd": tr_code,
            "tr_cont": "N",
            "tr_cont_key": "",
        }
        if self.config.mac_address:
            headers["mac_address"] = self.config.mac_address
        response = self._client.post(
            f"{self.config.base_url}{path}", json=payload, headers=headers
        )
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
        balance = _object(
            balance_body.get("CSPAQ12200OutBlock2"), "CSPAQ12200OutBlock2"
        )
        balance_request = _object(
            balance_body.get("CSPAQ12200OutBlock1"), "CSPAQ12200OutBlock1"
        )
        response_account_no = balance_request.get("AcntNo")
        if response_account_no is not None and not isinstance(response_account_no, str):
            raise LSOpenAPIError("LS CSPAQ12200 AcntNo must be a string")
        account_no = (
            response_account_no.strip() if isinstance(response_account_no, str) else ""
        ) or self.config.account_no
        observed_at = datetime.now(timezone.utc)
        return PortfolioSnapshot(
            # OAuth identifies the PAPER account and CSPAQ12200 echoes its
            # account number.  Prefer that authoritative response over a
            # manually duplicated env value, while retaining the env fallback
            # for broker-compatible test doubles.
            account_no=account_no,
            cash=_decimal(balance.get("Dps"), "Dps"),
            buying_power=_decimal(balance.get("MnyOrdAbleAmt"), "MnyOrdAbleAmt"),
            equity=_decimal(
                positions_body.get("t0424OutBlock", {}).get("sunamt"), "sunamt"
            ),
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
