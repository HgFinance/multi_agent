from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _compose(path: str) -> dict:
    return yaml.safe_load((ROOT / path).read_text(encoding="utf-8"))


def _environment(compose: dict, service: str) -> dict[str, object]:
    return compose["services"][service]["environment"]


def test_local_bff_separates_paper_orders_from_read_only_broker_projection() -> None:
    root = _compose("docker-compose.yml")
    bff = _environment(root, "portfolio-bff")
    assert bff["TRADING_API_URL"] == "${TRADING_API_URL:-http://trading-api:8000}"
    assert "TRADING_SERVICE_AUTH_SECRET" in bff
    assert bff["TRADING_SERVICE_AUTH_ISSUER"] == "${TRADING_SERVICE_AUTH_ISSUER:-portfolio-bff}"
    assert bff["TRADING_SERVICE_AUTH_AUDIENCE"] == "${TRADING_SERVICE_AUTH_AUDIENCE:-trading-api}"
    assert bff["USER_PAPER_ORDER_WORKFLOW_ENABLED"] == "true"
    assert bff["ENABLE_LS_ORDER_EVENTS"] == "${ENABLE_LS_ORDER_EVENTS:-false}"
    assert bff["ENABLE_BROKER_SNAPSHOT"] == "${ENABLE_BROKER_SNAPSHOT:-false}"
    assert bff["BROKER_SNAPSHOT_CACHE_SECONDS"] == "${BROKER_SNAPSHOT_CACHE_SECONDS:-10}"
    assert bff["LS_ENV"] == "${LS_ENV:-PAPER}"
    assert "LS_ACCOUNT_NO_PAPER" in bff
    assert not any("LIVE" in key for key in bff)


def test_eb_keeps_broker_projection_out_of_strict_paper_trading_api() -> None:
    eb = _compose("deploy/eb/docker-compose.yml")
    bff = _environment(eb, "portfolio-bff")
    trading = _environment(eb, "trading-api")
    for key in (
        "TRADING_SERVICE_AUTH_SECRET",
        "TRADING_SERVICE_AUTH_ISSUER",
        "TRADING_SERVICE_AUTH_AUDIENCE",
    ):
        assert bff[key] == trading[key]
    assert bff["TRADING_API_URL"] == "http://trading-api:8000"
    assert bff["USER_PAPER_ORDER_WORKFLOW_ENABLED"] == "false"
    assert bff["ENABLE_LS_ORDER_EVENTS"] == "${ENABLE_LS_ORDER_EVENTS:-false}"
    assert bff["LS_ENV"] == "${LS_ENV:-PAPER}"
    assert "LS_ACCOUNT_NO_PAPER" in bff
    assert trading["PAPER_DB"] == "${PAPER_DB:-true}"
    assert not any("LIVE" in key or key.startswith("LS_") for key in trading)
    assert not any("LIVE" in key for key in bff)


def test_example_env_exposes_only_internal_proof_not_user_or_broker_secrets() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    section = example.split("# Browser -> portfolio-bff -> trading-api", 1)[1].split(
        "# ----------------------------------------------------------------------------", 1
    )[0]
    assert "TRADING_API_URL=http://trading-api:8000" in section
    assert "TRADING_SERVICE_AUTH_SECRET=" in section
    assert "SUPABASE_SERVICE_ROLE" not in section
    assert "LS_ACCOUNT" not in section
    assert "LIVE" not in section
