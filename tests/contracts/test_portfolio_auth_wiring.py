from __future__ import annotations

import json
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def _portfolio_environment(relative_path: str) -> dict[str, object]:
    compose = yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))
    return compose["services"]["portfolio-bff"]["environment"]


def test_local_bff_uses_paper_broker_read_projections() -> None:
    environment = _portfolio_environment("docker-compose.yml")
    assert environment["APP_ENV"] == "local"
    assert environment["PORTFOLIO_AUTH_MODE"] == "fixture"
    assert "PORTFOLIO_AUTH_REQUIRED" not in environment
    assert environment["PORTFOLIO_LIVE_MODE"] == "broker"
    assert environment["ENABLE_LS_ORDER_EVENTS"] == "true"
    assert environment["ENABLE_BROKER_SNAPSHOT"] == "true"
    assert environment["ENABLE_LS_MARKET_DATA"] == "true"
    assert environment["ENABLE_LS_ACCOUNT_DATA"] == "true"
    assert environment["LS_ENV"] == "PAPER"


def test_reference_deployment_bundle_does_not_enable_browser_login() -> None:
    environment = _portfolio_environment("deploy/eb/docker-compose.yml")
    assert environment["PORTFOLIO_AUTH_MODE"] == "fixture"
    assert "PORTFOLIO_AUTH_REQUIRED" not in environment


def test_fixed_identity_contract_is_explicit_in_source_and_package_metadata() -> None:
    current_user = (ROOT / "apps/api/current_user.py").read_text(encoding="utf-8").casefold()
    package = json.loads((ROOT / "ai-office/package.json").read_text(encoding="utf-8"))
    assert "verify_" not in current_user
    assert "fixture_only_portfolio_identity" in current_user
    assert not any(str(name).startswith("@") and "auth" in str(name).casefold() for name in package["dependencies"])


def test_frontend_build_and_proxy_are_pinned_to_one_demo_identity() -> None:
    fixed_user_id = "00000000-0000-4000-8000-00000000cec0"
    fixed_fund_id = "3838f7d6-0c7c-4e54-85f3-316a451e7eeb"
    demo_identity = (ROOT / "ai-office/app/lib/demoIdentity.ts").read_text(encoding="utf-8")
    current_account = (ROOT / "ai-office/app/lib/currentAccount.ts").read_text(encoding="utf-8")
    proxy = (ROOT / "ai-office/worker/bffProxy.ts").read_text(encoding="utf-8")
    vite = (ROOT / "ai-office/vite.config.ts").read_text(encoding="utf-8")
    dockerfile = (ROOT / "ai-office/Dockerfile").read_text(encoding="utf-8")

    assert fixed_user_id in demo_identity
    assert fixed_fund_id in demo_identity
    assert "FIXED_DEMO_USER_ID" in current_account
    assert 'headers.set("x-user-id", FIXED_DEMO_USER_ID)' in proxy
    assert "DISCORD_ACTOR_MAP" not in current_account
    assert "DISCORD_ACTOR_MAP" not in vite
    assert "DISCORD_ACTOR_MAP" not in dockerfile
    assert "authorization" not in proxy.split("FORWARDED_REQUEST_HEADERS", 1)[1].split("] as const", 1)[0]


def test_runtime_sources_do_not_implement_supabase_or_browser_session_auth() -> None:
    runtime_roots = (
        ROOT / "apps",
        ROOT / "ai-office" / "app",
        ROOT / "ai-office" / "worker",
    )
    forbidden = (
        "supabase.auth",
        "sign_in_with",
        "signinwith",
        "createbrowserclient",
        "createserverclient",
        "auth.getsession",
        "auth.getuser",
    )
    for runtime_root in runtime_roots:
        for source in runtime_root.rglob("*"):
            if not source.is_file() or source.suffix not in {
                ".py",
                ".ts",
                ".tsx",
                ".js",
                ".jsx",
            }:
                continue
            contents = source.read_text(encoding="utf-8").casefold()
            assert not any(token in contents for token in forbidden), source


def test_root_scripts_pin_the_local_paper_stack() -> None:
    package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert "PORTFOLIO_AUTH_MODE" not in package["scripts"]["dev"]
    assert "PORTFOLIO_LIVE_MODE=broker" in package["scripts"]["bff"]
    assert "ENABLE_LS_ORDER_EVENTS=true" in package["scripts"]["bff"]
    assert "ENABLE_BROKER_SNAPSHOT=true" in package["scripts"]["bff"]
    assert "ENABLE_LS_MARKET_DATA=true" in package["scripts"]["bff"]
    assert "ENABLE_LS_ACCOUNT_DATA=true" in package["scripts"]["bff"]
    assert "LS_ENV=PAPER" in package["scripts"]["bff"]
    assert "LS_ACCOUNT_NO_PAPER=5601" in package["scripts"]["bff"]
    assert "PORTFOLIO_LIVE_MODE=fixture" not in package["scripts"]["bff"]
