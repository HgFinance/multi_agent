from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _portfolio_environment(relative_path: str) -> dict[str, object]:
    compose = yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))
    return compose["services"]["portfolio-bff"]["environment"]


def test_local_bff_is_fixture_only_and_external_broker_reads_are_disabled() -> None:
    environment = _portfolio_environment("docker-compose.yml")
    assert environment["APP_ENV"] == "local"
    assert environment["PORTFOLIO_AUTH_MODE"] == "fixture"
    assert environment["PORTFOLIO_AUTH_REQUIRED"] == "false"
    assert environment["PORTFOLIO_LIVE_MODE"] == "fixture"


def test_legacy_deployment_bundle_does_not_enable_user_login() -> None:
    environment = _portfolio_environment("deploy/eb/docker-compose.yml")
    assert environment["PORTFOLIO_AUTH_MODE"] == "fixture"
    assert environment["PORTFOLIO_AUTH_REQUIRED"] == "false"


def test_fixture_only_contract_is_explicit_in_source_and_package_metadata() -> None:
    current_user = (ROOT / "apps/api/current_user.py").read_text(encoding="utf-8").casefold()
    package = json.loads((ROOT / "ai-office/package.json").read_text(encoding="utf-8"))
    assert "verify_" not in current_user
    assert "fixture_only_portfolio_identity" in current_user
    assert not any(str(name).startswith("@") and "auth" in str(name).casefold() for name in package["dependencies"])


def test_root_scripts_pin_the_local_mock_stack() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert "PORTFOLIO_AUTH_MODE=fixture" in package["scripts"]["dev"]
    assert "PORTFOLIO_AUTH_MODE=fixture" in package["scripts"]["bff"]
    assert "PORTFOLIO_AUTH_REQUIRED=false" in package["scripts"]["bff"]
    assert "PORTFOLIO_LIVE_MODE=fixture" in package["scripts"]["bff"]
