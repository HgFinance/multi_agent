from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
AUTH_ENVIRONMENT_KEYS = {
    "APP_ENV",
    "PORTFOLIO_AUTH_MODE",
    "PORTFOLIO_AUTH_REQUIRED",
    "PORTFOLIO_CORS_ALLOW_ORIGINS",
    "SUPABASE_URL",
    "SUPABASE_AUTH_ISSUER",
    "SUPABASE_AUTH_JWKS_URL",
    "SUPABASE_AUTH_AUDIENCE",
    "SUPABASE_PUBLISHABLE_KEY",
    "SUPABASE_ANON_KEY",
}


def _portfolio_environment(relative_path: str) -> dict[str, object]:
    compose = yaml.safe_load((ROOT / relative_path).read_text(encoding="utf-8"))
    return compose["services"]["portfolio-bff"]["environment"]


def test_local_and_aws_bff_receive_identity_only_supabase_settings() -> None:
    for compose_path in ("docker-compose.yml", "deploy/eb/docker-compose.yml"):
        environment = _portfolio_environment(compose_path)
        assert AUTH_ENVIRONMENT_KEYS <= environment.keys()
        assert "SUPABASE_SERVICE_ROLE_KEY" not in environment
        assert environment["PORTFOLIO_AUTH_MODE"] == "${PORTFOLIO_AUTH_MODE:-supabase_jwt}"


def test_aws_auth_configuration_is_fail_fast_and_documented() -> None:
    environment = _portfolio_environment("deploy/eb/docker-compose.yml")
    assert str(environment["SUPABASE_URL"]).startswith("${SUPABASE_URL:?")
    assert str(environment["SUPABASE_PUBLISHABLE_KEY"]).startswith(
        "${SUPABASE_PUBLISHABLE_KEY:?"
    )
    assert str(environment["PORTFOLIO_CORS_ALLOW_ORIGINS"]).startswith(
        "${PORTFOLIO_CORS_ALLOW_ORIGINS:?"
    )

    runbook = (ROOT / "deploy/eb/README.md").read_text(encoding="utf-8")
    for setting in (
        "APP_ENV=production",
        "PORTFOLIO_AUTH_MODE=supabase_jwt",
        "PORTFOLIO_CORS_ALLOW_ORIGINS=",
        "SUPABASE_URL=",
        "SUPABASE_PUBLISHABLE_KEY=",
        "SUPABASE_AUTH_AUDIENCE=authenticated",
    ):
        assert setting in runbook
    assert "Never provide\n`SUPABASE_SERVICE_ROLE_KEY`" in runbook


def test_aws_bff_reaches_internal_accounting_api() -> None:
    environment = _portfolio_environment("deploy/eb/docker-compose.yml")
    assert environment["PORTFOLIO_API_URL"] == "http://accounting-api:8000"


def test_aws_compose_ci_supplies_non_secret_contract_placeholders() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/runtime-aws-contract.yml").read_text(
            encoding="utf-8"
        )
    )
    steps = workflow["jobs"]["elastic-beanstalk-bundle"]["steps"]
    compose_step = next(
        step for step in steps
        if step.get("name") == "Validate Elastic Beanstalk Compose"
    )
    assert {
        "CONTROL_DATABASE_URL",
        "PORTFOLIO_CORS_ALLOW_ORIGINS",
        "SUPABASE_URL",
        "SUPABASE_PUBLISHABLE_KEY",
    } <= compose_step["env"].keys()


def test_legacy_ui_bff_has_no_supabase_privileged_key() -> None:
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    environment = compose["services"]["ui-bff"].get("environment") or {}
    assert "SUPABASE_SERVICE_ROLE_KEY" not in environment


def test_environment_template_requires_explicit_fixture_mode() -> None:
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "APP_ENV=local" in template
    assert "PORTFOLIO_AUTH_MODE=fixture" in template
    assert "SUPABASE_AUTH_AUDIENCE=authenticated" in template
