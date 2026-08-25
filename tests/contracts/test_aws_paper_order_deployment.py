from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
OVERLAY_PATH = ROOT / "deploy" / "aws" / "docker-compose.paper-order.yml"
PROFILE_INSTALLER = ROOT / "scripts" / "aws_install_hermes_profiles.py"
DOCKERIGNORE = ROOT / ".dockerignore"
AWS_RUNBOOK = ROOT / "deploy" / "aws" / "README.md"


class _ComposeLoader(yaml.SafeLoader):
    pass


def _construct_override(loader: yaml.SafeLoader, node: yaml.Node) -> object:
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    if isinstance(node, yaml.MappingNode):
        return loader.construct_mapping(node)
    return loader.construct_scalar(node)


_ComposeLoader.add_constructor("!override", _construct_override)


def _yaml(path: Path) -> dict:
    return yaml.load(path.read_text(encoding="utf-8"), Loader=_ComposeLoader)


def test_overlay_separates_private_control_and_market_databases() -> None:
    overlay = _yaml(OVERLAY_PATH)
    services = overlay["services"]
    control = overlay["x-control-database-url"]
    market = overlay["x-market-database-url"]
    admin_control = overlay["x-admin-control-database-url"]
    admin_market = overlay["x-admin-market-database-url"]

    assert control.endswith("/${HEDGEFUND_CONTROL_DB_NAME:-control}")
    assert market.endswith("/market")
    assert "postgresql://hgfinance_runtime:" in control
    assert "postgresql://hgfinance_runtime:" in market
    assert "HEDGEFUND_RUNTIME_DB_PASSWORD" in control
    assert "HEDGEFUND_RUNTIME_DB_PASSWORD" in market
    assert "postgresql://postgres:" in admin_control
    assert "postgresql://postgres:" in admin_market
    assert "timescaledb:5432" in control
    assert "timescaledb:5432" in market
    assert "SUPABASE" not in control.upper()
    assert services["timescaledb"]["cpuset"] == ""
    assert "@sha256:" in services["timescaledb"]["image"]
    assert services["timescaledb"]["ports"] == []

    bootstrap = services["database-bootstrap"]
    assert bootstrap["profiles"] == ["deployment"]
    assert bootstrap["environment"]["CONTROL_DATABASE_URL"] == admin_control
    assert bootstrap["environment"]["MARKET_DATABASE_URL"] == admin_market
    for key in (
        "HEDGEFUND_RUNTIME_DB_PASSWORD",
        "HEDGEFUND_ORDER_DB_PASSWORD",
        "HEDGEFUND_TRADING_DB_PASSWORD",
        "HEDGEFUND_ACCOUNTING_DB_PASSWORD",
        "HEDGEFUND_CONDITIONAL_ORCHESTRATOR_DB_PASSWORD",
        "HEDGEFUND_CONDITIONAL_WORKER_DB_PASSWORD",
    ):
        assert key in bootstrap["environment"]
    assert bootstrap["command"][-1] == "--seed-paper-principal"
    assert "ports" not in bootstrap
    assert "volumes" not in bootstrap
    assert not bootstrap.get("privileged", False)

    reference = services["reference-bootstrap"]
    assert reference["profiles"] == ["deployment"]
    assert reference["environment"]["DATABASE_URL"] == admin_control
    assert reference["environment"]["CONTROL_DATABASE_URL"] == admin_control
    assert reference["environment"]["MARKET_DATABASE_URL"] == admin_market
    assert reference["command"] == [
        "python",
        "scripts/aws_reference_bootstrap.py",
    ]
    assert "ports" not in reference
    assert "volumes" not in reference
    assert "TRADING_BROKER_ADAPTER" not in reference["environment"]


def test_database_bootstrap_image_receives_canonical_migration_trees() -> None:
    dockerignore = DOCKERIGNORE.read_text(encoding="utf-8")

    assert "timescaledb/*" in dockerignore
    assert "!timescaledb/migrations/" in dockerignore
    assert "!timescaledb/migrations/**" in dockerignore
    assert "\ntimescaledb/\n" not in dockerignore
    assert "\nsupabase\n" not in dockerignore
    assert "\nsupabase/\n" not in dockerignore

    dockerfile = (ROOT / "apps" / "api" / "Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "COPY . ." in dockerfile


def test_portfolio_bff_stays_fixture_only_beside_private_operational_data() -> None:
    services = _yaml(OVERLAY_PATH)["services"]
    environment = services["portfolio-bff"]["environment"]

    assert environment["APP_ENV"] == "production"
    assert environment["PORTFOLIO_DATA_MODE"] == "production"
    assert environment["PORTFOLIO_AUTH_MODE"] == "fixture"
    assert environment["PORTFOLIO_AUTH_REQUIRED"] == "false"
    assert environment["USER_PAPER_ORDER_WORKFLOW_ENABLED"] == "true"
    assert environment["USER_PAPER_ORDER_DETERMINISTIC_FAST_PATH_ENABLED"] == "true"
    assert environment["PORTFOLIO_FIXTURE_TRADING_BOOKS_JSON"].startswith(
        "${PORTFOLIO_FIXTURE_TRADING_BOOKS_JSON:"
    )
    assert environment["DATABASE_URL"].endswith(
        "/${HEDGEFUND_CONTROL_DB_NAME:-control}"
    )
    assert "postgresql://hgfinance_runtime:" in environment["DATABASE_URL"]
    assert "postgresql://hgfinance_order_runtime:" in environment[
        "ORDER_ORCHESTRATOR_DATABASE_URL"
    ]
    assert environment["PORTFOLIO_CORS_ALLOW_ORIGINS"] == (
        "${PORTFOLIO_CORS_ALLOW_ORIGINS:-}"
    )
    assert environment["CEO_DISCORD_INGRESS_API_KEY"] == (
        "${CEO_DISCORD_INGRESS_API_KEY:?CEO_DISCORD_INGRESS_API_KEY is required}"
    )
    assert environment["DISCORD_ACTOR_MAP"] == (
        "${DISCORD_ACTOR_MAP:?DISCORD_ACTOR_MAP is required}"
    )


def test_conditional_rule_runtime_uses_two_dedicated_logins() -> None:
    overlay = _yaml(OVERLAY_PATH)
    services = overlay["services"]
    bff = services["portfolio-bff"]["environment"]
    mcp = services["paper-order-orchestrator-mcp"]["environment"]
    worker = services["conditional-rule-worker"]["environment"]

    assert "postgresql://hgfinance_conditional_orchestrator:" in bff[
        "CONDITIONAL_RULE_DATABASE_URL"
    ]
    assert bff["CONDITIONAL_RULE_DATABASE_ROLE"] == (
        "svc_conditional_rule_orchestrator"
    )
    assert "postgresql://hgfinance_conditional_orchestrator:" in mcp[
        "CONDITIONAL_RULE_DATABASE_URL"
    ]
    assert mcp["CONDITIONAL_RULE_DATABASE_ROLE"] == (
        "svc_conditional_rule_orchestrator"
    )
    assert mcp["PORTFOLIO_FIXTURE_TRADING_BOOKS_JSON"].startswith(
        "${PORTFOLIO_FIXTURE_TRADING_BOOKS_JSON:"
    )
    assert "postgresql://hgfinance_conditional_worker:" in worker[
        "CONDITIONAL_RULE_DATABASE_URL"
    ]
    assert worker["CONDITIONAL_RULE_WORKER_DATABASE_ROLE"] == (
        "svc_conditional_rule_worker"
    )
    for service_name, database_key in {
        "conditional-rule-worker": "CONDITIONAL_RULE_DATABASE_URL",
        "conditional-rule-retention-worker": "CONDITIONAL_RULE_DATABASE_URL",
        "conditional-rule-outbox-relay": "CONDITIONAL_RULE_DATABASE_URL",
        "conditional-rule-notification-consumer": "CONDITIONAL_RULE_DATABASE_URL",
    }.items():
        environment = services[service_name]["environment"]
        assert "DATABASE_URL" not in environment
        assert "postgresql://hgfinance_conditional_worker:" in environment[database_key]
        assert environment["CONDITIONAL_RULE_WORKER_DATABASE_ROLE"] == (
            "svc_conditional_rule_worker"
        )
    assert worker["TRADING_API_URL"] == "http://trading-api:8000"
    assert worker["MARKET_API_URL"] == "http://market-api:8036"
    assert "TRADING_SERVICE_AUTH_SECRET" not in worker
    assert "MCP_TRADING_ORDER_API_KEY" not in worker
    root = _yaml(ROOT / "docker-compose.yml")
    assert root["services"]["conditional-rule-worker"]["build"]["dockerfile"] == (
        "Dockerfile.conditional-rule-worker"
    )
    for service_name in (
        "conditional-rule-worker",
        "conditional-rule-retention-worker",
        "conditional-rule-outbox-relay",
        "conditional-rule-notification-consumer",
    ):
        environment = root["services"][service_name]["environment"]
        assert "postgresql://hgfinance_conditional_worker:" in environment[
            "CONDITIONAL_RULE_DATABASE_URL"
        ]
        assert "role%3Dsvc_conditional_rule_worker" in environment[
            "CONDITIONAL_RULE_DATABASE_URL"
        ]
    worker_image = (ROOT / "Dockerfile.conditional-rule-worker").read_text(
        encoding="utf-8"
    )
    assert "hermes" not in worker_image.casefold()
    assert "USER 65532:65532" in worker_image


def test_realtime_and_paper_execution_share_the_existing_ls_token_cache() -> None:
    root = _yaml(ROOT / "docker-compose.yml")
    trading = _yaml(ROOT / "departments" / "02-trading" / "compose.yaml")
    realtime = root["services"]["ls-realtime"]
    execution = trading["services"]["trading-api"]

    assert realtime["environment"]["LS_TOKEN_CACHE_DIR"] == (
        "/var/lib/ls-token-cache"
    )
    assert execution["environment"]["LS_TOKEN_CACHE_DIR"] == (
        "/var/lib/ls-token-cache"
    )
    mount = "ls_token_cache:/var/lib/ls-token-cache"
    assert mount in realtime["volumes"]
    assert mount in execution["volumes"]
    assert "ls_token_cache" in root["volumes"]


def test_discord_ingress_secret_is_scoped_to_bff_and_order_gateways() -> None:
    services = _yaml(OVERLAY_PATH)["services"]
    secret_key = "CEO_DISCORD_INGRESS_API_KEY"
    required_value = (
        "${CEO_DISCORD_INGRESS_API_KEY:?CEO_DISCORD_INGRESS_API_KEY is required}"
    )

    recipients = {
        service_name
        for service_name, service in services.items()
        if secret_key in (service.get("environment") or {})
    }
    assert recipients == {"portfolio-bff", "ceo-hermes", "trading-hermes"}
    assert services["portfolio-bff"]["environment"][secret_key] == required_value
    ceo_environment = services["ceo-hermes"]["environment"]
    assert ceo_environment[secret_key] == required_value
    assert ceo_environment["HGFINANCE_DISCORD_INGRESS_URL"] == (
        "http://portfolio-bff:8000/ui/ceo/ingress"
    )
    trading_environment = services["trading-hermes"]["environment"]
    assert trading_environment[secret_key] == required_value
    assert trading_environment["HGFINANCE_DISCORD_INGRESS_URL"] == (
        "http://portfolio-bff:8000/ui/ceo/ingress"
    )


def test_app_containers_never_receive_postgres_superuser_dsns() -> None:
    services = _yaml(OVERLAY_PATH)["services"]
    admin_jobs = {"database-bootstrap", "reference-bootstrap"}
    for service_name, service in services.items():
        environment = service.get("environment") or {}
        postgres_dsns = [
            value
            for value in environment.values()
            if isinstance(value, str) and "postgresql://postgres:" in value
        ]
        if service_name in admin_jobs:
            assert postgres_dsns
        else:
            assert postgres_dsns == [], service_name


def test_critical_services_use_one_control_only_login_and_exact_role() -> None:
    overlay = _yaml(OVERLAY_PATH)
    services = overlay["services"]
    order = overlay["x-order-database-url"]
    trading = overlay["x-trading-database-url"]
    relay = overlay["x-trading-outbox-database-url"]
    accounting = overlay["x-accounting-database-url"]

    assert order.endswith("/${HEDGEFUND_CONTROL_DB_NAME:-control}")
    assert "/${HEDGEFUND_CONTROL_DB_NAME:-control}?" in trading
    assert "/${HEDGEFUND_CONTROL_DB_NAME:-control}?" in accounting
    assert "options=-c%20role%3Dsvc_trading_api" in trading
    assert "options=-c%20role%3Dsvc_trading_outbox_relay" in relay
    assert "options=-c%20role%3Dsvc_accounting_ledger" in accounting
    assert all("/market" not in dsn for dsn in (order, trading, accounting))
    order_environment = services["paper-order-orchestrator-mcp"]["environment"]
    assert order_environment["DATABASE_URL"] == overlay["x-control-database-url"]
    assert (
        order_environment["CONTROL_DATABASE_URL"]
        == overlay["x-control-database-url"]
    )
    assert order_environment["ORDER_ORCHESTRATOR_DATABASE_URL"] == order
    assert (
        order_environment["ORDER_ORCHESTRATOR_DATABASE_ROLE"]
        == "svc_order_orchestrator"
    )
    trading_api = services["trading-api"]["environment"]
    assert trading_api["DATABASE_URL"] == trading
    assert "TRADING_DIRECTIVE_DATABASE_URL" not in trading_api
    assert trading_api["TRADING_DATABASE_ROLE"] == "svc_trading_api"
    trading_worker = services["trading-directive-worker"]["environment"]
    assert trading_worker["DATABASE_URL"] == trading
    assert "TRADING_DIRECTIVE_DATABASE_URL" not in trading_worker
    assert trading_worker["TRADING_DATABASE_ROLE"] == "svc_trading_api"
    relay_environment = services["trading-outbox-relay"]["environment"]
    assert relay_environment["DATABASE_URL"] == trading
    assert relay_environment["TRADING_OUTBOX_DATABASE_URL"] == relay
    assert "TRADING_OMS_DATABASE_URL" not in relay_environment
    for service_name in (
        "accounting-api",
        "accounting-ledger-consumer",
        "accounting-close-scheduler",
    ):
        environment = services[service_name]["environment"]
        assert environment["DATABASE_URL"] == accounting
        assert environment["ACCOUNTING_DATABASE_ROLE"] == "svc_accounting_ledger"


def test_all_order_and_accounting_mutation_services_are_hard_paper() -> None:
    services = _yaml(OVERLAY_PATH)["services"]
    trading = services["trading-api"]["environment"]
    worker = services["trading-directive-worker"]["environment"]
    accounting = services["accounting-api"]["environment"]

    assert trading["PAPER_DB"] == "true"
    assert trading["TRADING_EXECUTION_MODE"] == "PAPER"
    assert trading["TRADING_BROKER_ADAPTER"] == "${TRADING_BROKER_ADAPTER:-ls-paper}"
    assert trading["TRADING_DIRECTIVE_REPOSITORY"] == "postgres"
    assert trading["LS_ENV"] == "PAPER"
    assert "LS_APP_KEY_PAPER" in trading
    assert "LS_APP_SECRET_KEY_PAPER" in trading
    assert "LS_APP_KEY" not in trading
    assert "LS_APP_SECRET_KEY" not in trading
    assert worker["TRADING_EXECUTION_MODE"] == "PAPER"
    assert worker["TRADING_BROKER_ADAPTER"] == "${TRADING_BROKER_ADAPTER:-ls-paper}"
    assert worker["LS_ENV"] == "PAPER"
    assert "LS_APP_KEY" not in worker
    assert "LS_APP_SECRET_KEY" not in worker
    assert accounting["ACCOUNTING_MODE"] == "PAPER_DB"
    assert accounting["PAPER_DB"] == "true"


def test_every_active_database_consumer_is_overridden_by_aws_overlay() -> None:
    root = _yaml(ROOT / "docker-compose.yml")
    source_files = [
        ROOT / "departments" / "00-ceo-office" / "compose.yaml",
        ROOT / "departments" / "02-trading" / "compose.yaml",
        ROOT / "departments" / "05-accounting-portfolio" / "compose.yaml",
        ROOT / "departments" / "07-agent-workforce" / "compose.yaml",
    ]
    source_services = dict(root["services"])
    for source_file in source_files:
        source_services.update(_yaml(source_file)["services"])
    overlay_services = _yaml(OVERLAY_PATH)["services"]

    consumers: set[str] = set()
    for service_name, service in source_services.items():
        if service.get("profiles"):
            continue
        environment = service.get("environment") or {}
        if any("DATABASE" in key for key in environment):
            consumers.add(service_name)

    assert consumers <= set(overlay_services)
    dedicated_only = {
        "conditional-rule-worker": ("CONDITIONAL_RULE_DATABASE_URL",),
        "conditional-rule-retention-worker": ("CONDITIONAL_RULE_DATABASE_URL",),
        "conditional-rule-outbox-relay": ("CONDITIONAL_RULE_DATABASE_URL",),
        "conditional-rule-notification-consumer": (
            "ORDER_ORCHESTRATOR_DATABASE_URL",
            "CONDITIONAL_RULE_DATABASE_URL",
        ),
    }
    for service_name in consumers:
        environment = overlay_services[service_name].get("environment") or {}
        if service_name in dedicated_only:
            assert "DATABASE_URL" not in environment, service_name
            for key in dedicated_only[service_name]:
                assert "timescaledb:5432" in environment[key], (service_name, key)
            continue
        assert "DATABASE_URL" in environment, service_name
        assert "timescaledb:5432" in environment["DATABASE_URL"], service_name
        assert not environment["DATABASE_URL"].endswith("/market"), service_name


def test_market_consumers_receive_only_the_market_database_for_timeseries() -> None:
    services = _yaml(OVERLAY_PATH)["services"]
    for service_name, key in (
        ("ls-realtime", "TIMESCALE_DATABASE_URL"),
        ("batch-collectors", "TIMESCALE_DATABASE_URL"),
        ("market-api", "TIMESCALE_DATABASE_URL"),
        ("factory-autopilot", "TIMESCALE_DATABASE_URL"),
        ("factory-experiment-worker", "TIMESCALE_DATABASE_URL"),
        ("qa-reproduction-worker", "QA_REPRODUCTION_TIMESCALE_DATABASE_URL"),
    ):
        assert services[service_name]["environment"][key].endswith("/market")


def test_factory_autopilot_scopes_only_its_planning_connection() -> None:
    environment = _yaml(OVERLAY_PATH)["services"]["factory-autopilot"]["environment"]
    source = (
        ROOT / "departments/01-research/factory/factory_autopilot.py"
    ).read_text(encoding="utf-8")

    assert environment["DATABASE_SESSION_URL"] == environment["DATABASE_URL"]
    assert "DATABASE_RUNTIME_ROLE" not in environment
    assert 'runtime_role="svc_quant"' in source
