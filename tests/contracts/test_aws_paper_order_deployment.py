from __future__ import annotations

import ast
import base64
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import yaml


ROOT = Path(__file__).resolve().parents[2]
OVERLAY_PATH = ROOT / "deploy" / "aws" / "docker-compose.paper-order.yml"
DEPLOY_SCRIPT = ROOT / "scripts" / "aws_deploy_paper_order_release.sh"
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


def _preflight_validation_functions() -> dict[str, object]:
    """Compile the pure validators directly from the deployer's heredoc."""

    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")
    marker = "if ! python3 - \"$RUNTIME_ENV\" <<'PY'\n"
    heredoc = script.split(marker, 1)[1].split("\nPY\nthen", 1)[0]
    parsed = ast.parse(heredoc)
    wanted = {
        "_base64url_bytes",
        "_valid_asymmetric_signing_jwk",
        "_validate_jwks_document",
        "_valid_cors_allowlist",
        "_actor_map_contains_seed_binding",
    }
    functions = [
        node
        for node in parsed.body
        if isinstance(node, ast.FunctionDef) and node.name in wanted
    ]
    assert {node.name for node in functions} == wanted
    namespace: dict[str, object] = {
        "base64": base64,
        "re": __import__("re"),
        "urlsplit": urlsplit,
        "UUID": UUID,
    }
    exec(  # noqa: S102 - executes reviewed repository-owned function AST only
        compile(ast.Module(body=functions, type_ignores=[]), str(DEPLOY_SCRIPT), "exec"),
        namespace,
    )
    return namespace


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


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


def test_production_bff_uses_supabase_jwt_but_private_operational_data() -> None:
    services = _yaml(OVERLAY_PATH)["services"]
    environment = services["portfolio-bff"]["environment"]

    assert environment["APP_ENV"] == "production"
    assert environment["PORTFOLIO_DATA_MODE"] == "production"
    assert environment["PORTFOLIO_AUTH_MODE"] == "supabase_jwt"
    assert environment["PORTFOLIO_AUTH_REQUIRED"] == "true"
    assert environment["USER_PAPER_ORDER_WORKFLOW_ENABLED"] == "true"
    assert environment["PORTFOLIO_FIXTURE_TRADING_BOOKS_JSON"] == "[]"
    assert environment["DATABASE_URL"].endswith(
        "/${HEDGEFUND_CONTROL_DB_NAME:-control}"
    )
    assert "postgresql://hgfinance_runtime:" in environment["DATABASE_URL"]
    assert "postgresql://hgfinance_order_runtime:" in environment[
        "ORDER_ORCHESTRATOR_DATABASE_URL"
    ]
    assert "SUPABASE_URL" in environment
    assert environment["SUPABASE_PUBLISHABLE_KEY"] == "${SUPABASE_PUBLISHABLE_KEY:-}"
    assert environment["SUPABASE_ANON_KEY"] == "${SUPABASE_ANON_KEY:-}"
    assert environment["PORTFOLIO_CORS_ALLOW_ORIGINS"] == (
        "${PORTFOLIO_CORS_ALLOW_ORIGINS:-}"
    )
    assert environment["CEO_DISCORD_INGRESS_API_KEY"] == (
        "${CEO_DISCORD_INGRESS_API_KEY:?CEO_DISCORD_INGRESS_API_KEY is required}"
    )
    assert environment["DISCORD_ACTOR_MAP"] == (
        "${DISCORD_ACTOR_MAP:?DISCORD_ACTOR_MAP is required}"
    )
    assert "SUPABASE_SERVICE_ROLE_KEY" not in environment


def test_discord_ingress_secret_is_scoped_to_bff_and_ceo_hermes() -> None:
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
    assert recipients == {"portfolio-bff", "ceo-hermes"}
    assert services["portfolio-bff"]["environment"][secret_key] == required_value
    ceo_environment = services["ceo-hermes"]["environment"]
    assert ceo_environment[secret_key] == required_value
    assert ceo_environment["HGFINANCE_DISCORD_INGRESS_URL"] == (
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
    accounting = overlay["x-accounting-database-url"]

    assert order.endswith("/${HEDGEFUND_CONTROL_DB_NAME:-control}")
    assert "/${HEDGEFUND_CONTROL_DB_NAME:-control}?" in trading
    assert "/${HEDGEFUND_CONTROL_DB_NAME:-control}?" in accounting
    assert "options=-c%20role%3Dsvc_trading_api" in trading
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
    assert services["trading-outbox-relay"]["environment"]["DATABASE_URL"] == trading
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
    assert trading["TRADING_BROKER_ADAPTER"] == "paper"
    assert trading["TRADING_DIRECTIVE_REPOSITORY"] == "postgres"
    assert worker["TRADING_EXECUTION_MODE"] == "PAPER"
    assert worker["TRADING_BROKER_ADAPTER"] == "paper"
    assert accounting["ACCOUNTING_MODE"] == "PAPER_DB"
    assert accounting["PAPER_DB"] == "true"


def test_jwks_preflight_accepts_only_public_es256_or_rs256_verification_keys() -> None:
    functions = _preflight_validation_functions()
    validate = functions["_validate_jwks_document"]
    ec = {
        "kid": "ec-1",
        "use": "sig",
        "key_ops": ["verify"],
        "kty": "EC",
        "alg": "ES256",
        "crv": "P-256",
        "x": _b64url(b"x" * 32),
        "y": _b64url(b"y" * 32),
    }
    rsa = {
        "kid": "rsa-1",
        "use": "sig",
        "kty": "RSA",
        "alg": "RS256",
        "n": _b64url(b"n" * 256),
        "e": _b64url((65537).to_bytes(3, "big")),
    }

    assert validate({"keys": [ec]}) is True
    assert validate({"keys": [rsa]}) is True
    assert validate({"keys": [{"kid": "shared", "kty": "oct", "alg": "HS256"}]}) is False
    assert validate({"keys": [{**ec, "alg": "EdDSA"}]}) is False
    assert validate({"keys": [{**ec, "d": _b64url(b"private")}]}) is False
    assert validate({"keys": [{**rsa, "n": _b64url(b"short")}]}) is False
    assert validate({"keys": [{**rsa, "key_ops": ["sign", "verify"]}]}) is False
    assert validate({"keys": []}) is False


def test_backend_only_cors_preflight_accepts_only_empty_or_exact_https_origins() -> None:
    validate = _preflight_validation_functions()["_valid_cors_allowlist"]

    assert validate("") is True
    assert validate("   ") is True
    assert validate("https://app.example.com") is True
    assert validate("https://app.example.com, https://ops.example.com/") is True
    for invalid in (
        "*",
        "https://*.example.com",
        "http://app.example.com",
        "https://user:password@app.example.com",
        "https://app.example.com/path",
        "https://app.example.com?query=1",
        "https://app.example.com#fragment",
        "https://app.example.com:99999",
        "https://app.example.com,",
        "https://app.example.com,,https://ops.example.com",
        "not-an-origin",
    ):
        assert validate(invalid) is False


def test_actor_map_preflight_requires_effective_seed_user_and_fund_binding() -> None:
    validate = _preflight_validation_functions()["_actor_map_contains_seed_binding"]
    user_id = UUID("00000000-0000-4000-8000-00000000cec0")
    fund_id = UUID("5c26db42-ce83-4daf-b1dc-c81680c13a6c")
    discord_id = "123456789012345678"
    matching = f"{discord_id}:{user_id}:{fund_id}"

    assert validate(matching, user_id, fund_id) is True
    multiple = f"999999999999999999:{UUID(int=1)}:{UUID(int=2)}, {matching}"
    assert validate(multiple, user_id, fund_id) is True
    assert validate(f"{discord_id}:{user_id}", user_id, fund_id) is False
    assert validate(f"{discord_id}:{user_id}:{UUID(int=2)}", user_id, fund_id) is False
    assert validate("not-a-discord-id:not-a-user:not-a-fund", user_id, fund_id) is False
    # Runtime keeps the first valid binding for a Discord id, so a shadowed
    # matching entry must not make deployment pass.
    shadowed = (
        f"{discord_id}:{UUID(int=1)}:{UUID(int=2)},"
        f"{discord_id}:{user_id}:{fund_id}"
    )
    assert validate(shadowed, user_id, fund_id) is False


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
    for service_name in consumers:
        environment = overlay_services[service_name].get("environment") or {}
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


def test_release_script_is_worktree_only_and_fail_closed() -> None:
    script = DEPLOY_SCRIPT.read_text(encoding="utf-8")

    assert 'PROJECT_NAME="hedgefund"' in script
    assert "git clone --bare" in script
    assert "worktree add --quiet --detach" in script
    assert '--project-name "$PROJECT_NAME"' in script
    assert '-f "$release_path/docker-compose.yml"' in script
    assert '-f "$release_path/deploy/aws/docker-compose.paper-order.yml"' in script
    assert '--env-file "$RUNTIME_ENV"' in script
    assert 'ORIGINAL_ARGS=("$@")' in script
    assert 'TARGET_DEPLOY_SCRIPT="$RELEASE/scripts/aws_deploy_paper_order_release.sh"' in script
    assert 'HGFINANCE_DEPLOY_HANDOFF_COMMIT' in script
    assert 'exec bash "$TARGET_DEPLOY_SCRIPT" "${ORIGINAL_ARGS[@]}"' in script
    assert script.index('exec bash "$TARGET_DEPLOY_SCRIPT"') < script.index(
        'RUNTIME_ENV="$RELEASES_ROOT/state/runtime.env"'
    )
    assert "--allow-first-deploy" in script
    assert "--skip-database-backup" in script
    assert "pg_dump" in script
    database_detection = script.index("DATABASE_CONTAINER_EXISTED=0")
    database_backup = script.index(
        'say "Creating protected pre-migration database backups..."'
    )
    database_reconcile = script.index(
        'compose_release "$RELEASE" up -d --no-deps timescaledb'
    )
    assert database_detection < database_backup < database_reconcile
    assert "docker inspect --format '{{.State.Running}}' hedgefund-timescaledb" in script
    assert "No existing TimescaleDB container" in script
    assert "((market_backed_up == 1))" in script
    assert "reference-bootstrap" in script
    assert "Provisioning and auditing PAPER order reference data" in script
    assert '"$RELEASE/scripts/aws_install_hermes_profiles.py" install' in script
    assert '"$RELEASE/scripts/aws_install_hermes_profiles.py" restore' in script
    assert '--release-root "$RELEASE"' in script
    assert '--runtime-env "$RUNTIME_ENV"' in script
    assert '--runtime-root "$PROFILE_RUNTIME_ROOT"' in script
    assert "PROFILE_INSTALL_ACTIVE=1" in script
    assert script.index("aws_install_hermes_profiles.py\" install") < script.index(
        "SWITCH_STARTED=1"
    )
    assert "smoke_trading_paper_order_mcp" in script
    assert "hermes mcp test user-paper-order" in script
    assert 'grep -Fq "process_user_paper_order"' in script
    assert "Tools discovered:" in script
    assert "hedgefund-trading-hermes" in script
    assert "assert_release_owned_container" in script
    assert 'com.docker.compose.project.working_dir' in script
    assert 'com.docker.compose.project.config_files' in script
    assert 'com.docker.compose.config-hash' in script
    assert 'config --hash "$service_name"' in script
    ownership_gate = script.split("assert_release_owned_container()", 1)[1].split(
        "\n}", 1
    )[0]
    assert ownership_gate.count("|| return 1") >= 7
    activation = script.split("activate_release_services()", 1)[1].split("\n}", 1)[0]
    assert 'stop_order_hermes || return 1' in activation
    assert '"${non_order_services[@]}" || return 1' in activation
    assert 'force-recreate trading-api || return 1' in activation
    assert 'paper-order-orchestrator-mcp portfolio-bff || return 1' in activation
    assert 'ceo-hermes trading-hermes || return 1' in activation
    assert (
        activation.index('force-recreate trading-api')
        < activation.index('wait_container hedgefund-trading-api')
        < activation.index('paper-order-orchestrator-mcp portfolio-bff')
        < activation.index('ceo-hermes trading-hermes')
        < activation.index('wait_hermes_gateway hedgefund-ceo-hermes')
        < activation.index('smoke_ceo_discord_ingress')
    )
    assert 'capture_rollback_images' in script
    assert 'compose_release "$PREVIOUS_RELEASE" build' in script
    assert 'hgfinance-rollback/${container_name#hedgefund-}' in script
    assert 'restore_rollback_images' in script
    rollback = script.split("rollback_release()", 1)[1].split("\n}", 1)[0]
    assert (
        rollback.index("restore_rollback_images")
        < rollback.index('activate_release_services "$PREVIOUS_RELEASE"')
    )
    assert 'mv -Tf -- "$rollback_link" "$CURRENT_LINK"' in rollback
    assert '"$previous_commit" >"$RELEASES_ROOT/state/current-commit"' in rollback
    main_activation = script.rindex('activate_release_services "$RELEASE"')
    assert main_activation < script.index('printf \'%s\\n\' "$RELEASE_COMMIT"')
    assert "smoke_ceo_discord_ingress" in script
    ceo_smoke = script.split("smoke_ceo_discord_ingress()", 1)[1].split("\n}", 1)[0]
    assert 'test -n "${CEO_DISCORD_INGRESS_API_KEY:-}"' in ceo_smoke
    assert 'test "${HGFINANCE_DISCORD_INGRESS_URL:-}" =' in ceo_smoke
    assert 'data=b"{}"' in ceo_smoke
    assert "exc.code == 422" in ceo_smoke
    assert "urllib.request.urlopen" in ceo_smoke
    assert "CEO_DISCORD_INGRESS_API_KEY:-}" not in script.split(
        "smoke_ceo_discord_ingress()", 1
    )[1].split("\n}", 1)[0].replace(
        'test -n "${CEO_DISCORD_INGRESS_API_KEY:-}"', ""
    )
    assert "MCP_TRADING_ORDER_API_KEY" not in script.split(
        "smoke_trading_paper_order_mcp()", 1
    )[1].split("}", 1)[0]
    required = script.split("required = (", 1)[1].split("\n)", 1)[0]
    assert '"SUPABASE_URL"' in required
    assert '"DISCORD_ACTOR_MAP"' in required
    for key in (
        "MCP_TRADING_ORDER_API_KEY",
        "TRADING_SERVICE_AUTH_SECRET",
        "TRADING_INTERNAL_SERVICE_AUTH_SECRET",
        "CEO_DISCORD_INGRESS_API_KEY",
        "HEDGEFUND_RUNTIME_DB_PASSWORD",
        "HEDGEFUND_ORDER_DB_PASSWORD",
        "HEDGEFUND_TRADING_DB_PASSWORD",
        "HEDGEFUND_ACCOUNTING_DB_PASSWORD",
    ):
        assert f'"{key}"' in required
    managed = script.split("managed_secret_keys = (", 1)[1].split("\n)", 1)[0]
    assert managed.count('"') == 16
    assert "set(managed_secrets)" in script
    assert ") != len(managed_secret_keys):" in script
    assert "PAPER managed secrets must be distinct and at least 32 characters" in script
    assert '"PORTFOLIO_CORS_ALLOW_ORIGINS"' not in required
    assert '"SUPABASE_PUBLISHABLE_KEY"' not in required
    assert '"SUPABASE_ANON_KEY"' not in required
    assert 'key.get("kty") == "EC" and key.get("alg") == "ES256"' in script
    assert 'key.get("kty") == "RSA" and key.get("alg") == "RS256"' in script
    assert '"/auth/v1"' in script
    assert '"/.well-known/jwks.json"' in script
    assert 'install -d -m 700 -- "$backup_root"' in script
    assert "rollback_release" in script
    assert "--remove-orphans" in script
    assert "git pull" not in script
    assert "git reset" not in script
    assert "git clean" not in script
    assert "docker compose down" not in script
    assert "--volumes" not in script
    assert "set -x" not in script

    runbook = AWS_RUNBOOK.read_text(encoding="utf-8")
    assert "never run `docker compose up`" in runbook
    assert "`/home/ubuntu/hgfinance`" in runbook
    assert "config hash" in runbook

    build_command = (
        'compose_release "$RELEASE" --profile deployment build --pull'
    )
    pull_command = 'compose_release "$RELEASE" pull --policy missing'
    assert build_command in script
    assert pull_command in script
    assert script.index(build_command) < script.index(pull_command)
    assert "external_pull_service_plan" in script
    assert "locally_built_images" in script
    assert 'image not in locally_built_images' in script
    assert 'config --format json' in script
    assert '"${EXTERNAL_PULL_SERVICES[@]}"' in script
    assert "--ignore-pull-failures" not in script

    installer = PROFILE_INSTALLER.read_text(encoding="utf-8")
    assert "departments/00-ceo-office/hermes" in installer
    assert "departments/02-trading/hermes" in installer
    assert "os.replace" in installer
    assert "auth.json" not in installer
