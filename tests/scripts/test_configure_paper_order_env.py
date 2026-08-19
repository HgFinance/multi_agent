"""Tests for the non-disclosing PAPER-order dotenv configurator."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "configure_paper_order_env.py"
SPEC = importlib.util.spec_from_file_location("configure_paper_order_env", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
cli = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = cli
SPEC.loader.exec_module(cli)


def _values(path: Path) -> dict[str, str]:
    return {
        key: assignment.value
        for key, assignment in cli._parse_assignments(
            path.read_text(encoding="utf-8")
        ).items()
    }


def _generator(*values: str):
    iterator = iter(values)
    return lambda: next(iterator)


def test_repository_uses_one_duplicate_free_environment_template() -> None:
    template = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert cli._duplicate_assignment_keys(template) == ()
    assert "저장소의 유일한 환경 변수 템플릿" in template
    assert ".env.aws.template" in template


def test_local_configures_exact_authenticated_fixture_grant(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    preserved = "m" * 48
    env_file.write_text(
        "# keep this comment\n"
        "UNMANAGED=value\n"
        f"MCP_TRADING_ORDER_API_KEY={preserved}\n",
        encoding="utf-8",
    )

    result = cli.configure_environment(
        runtime="local",
        env_file=env_file,
        generator=_generator("s" * 48, "i" * 48, "d" * 48),
    )
    values = _values(env_file)

    assert result.generated_secret_keys == (
        "TRADING_SERVICE_AUTH_SECRET",
        "TRADING_INTERNAL_SERVICE_AUTH_SECRET",
        "CEO_DISCORD_INGRESS_API_KEY",
    )
    assert values["APP_ENV"] == "local"
    assert values["PORTFOLIO_AUTH_MODE"] == "fixture"
    assert values["PORTFOLIO_AUTH_REQUIRED"] == "true"
    assert values["USER_PAPER_ORDER_WORKFLOW_ENABLED"] == "true"
    assert values["LS_ENV"] == "PAPER"
    assert values["TRADING_EXECUTION_MODE"] == "PAPER"
    assert values["TRADING_BROKER_ADAPTER"] == "paper"
    assert values["MCP_TRADING_ORDER_API_KEY"] == preserved
    grant = json.loads(values["PORTFOLIO_FIXTURE_TRADING_BOOKS_JSON"])
    assert grant == [
        {
            "user_id": "00000000-0000-4000-8000-00000000cec0",
            "fund_id": "5c26db42-ce83-4daf-b1dc-c81680c13a6c",
            "book_id": "07d913de-9a5b-4cf5-b893-31a625445761",
            "name": "MAIN",
            "role": "TRADER",
            "fund_status": "ACTIVE",
            "book_status": "ACTIVE",
        }
    ]
    output = env_file.read_text(encoding="utf-8")
    assert "# keep this comment" in output
    assert "UNMANAGED=value" in output


def test_preserves_four_distinct_valid_service_secrets_and_is_idempotent(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    secrets_by_key = {
        "MCP_TRADING_ORDER_API_KEY": "a" * 40,
        "TRADING_SERVICE_AUTH_SECRET": "b" * 40,
        "TRADING_INTERNAL_SERVICE_AUTH_SECRET": "c" * 40,
        "CEO_DISCORD_INGRESS_API_KEY": "d" * 40,
    }
    env_file.write_text(
        "\n".join(f"{key}={value}" for key, value in secrets_by_key.items()) + "\n",
        encoding="utf-8",
    )

    def must_not_generate() -> str:
        raise AssertionError("valid credentials must be preserved")

    first = cli.configure_environment(
        runtime="local", env_file=env_file, generator=must_not_generate
    )
    first_text = env_file.read_text(encoding="utf-8")
    second = cli.configure_environment(
        runtime="local", env_file=env_file, generator=must_not_generate
    )

    assert first.changed is True
    assert second.changed is False
    assert second.generated_secret_keys == ()
    assert env_file.read_text(encoding="utf-8") == first_text
    assert all(
        _values(env_file)[key] == value for key, value in secrets_by_key.items()
    )


def test_explicit_rotation_replaces_only_selected_mcp_credentials(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    old_research = "r" * 40
    old_order = "o" * 40
    preserved = {
        "TRADING_SERVICE_AUTH_SECRET": "s" * 40,
        "TRADING_INTERNAL_SERVICE_AUTH_SECRET": "i" * 40,
        "CEO_DISCORD_INGRESS_API_KEY": "d" * 40,
    }
    env_file.write_text(
        f"MCP_RESEARCH_API_KEY={old_research}\n"
        f"MCP_TRADING_ORDER_API_KEY={old_order}\n"
        + "".join(f"{key}={value}\n" for key, value in preserved.items()),
        encoding="utf-8",
    )

    result = cli.configure_environment(
        runtime="local",
        env_file=env_file,
        generator=_generator("n" * 48, "q" * 48),
        rotate_keys=("MCP_RESEARCH_API_KEY", "MCP_TRADING_ORDER_API_KEY"),
    )
    values = _values(env_file)

    assert result.generated_secret_keys == (
        "MCP_TRADING_ORDER_API_KEY", "MCP_RESEARCH_API_KEY")
    assert values["MCP_TRADING_ORDER_API_KEY"] == "n" * 48
    assert values["MCP_RESEARCH_API_KEY"] == "q" * 48
    assert all(values[key] == value for key, value in preserved.items())
    assert old_order not in env_file.read_text(encoding="utf-8")
    assert old_research not in env_file.read_text(encoding="utf-8")


def test_rotation_rejects_unknown_key_without_touching_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    original = "UNMANAGED=keep\n"
    env_file.write_text(original, encoding="utf-8")

    with pytest.raises(cli.ConfigurationError, match="unsupported credential"):
        cli.configure_environment(
            runtime="local",
            env_file=env_file,
            rotate_keys=("DATABASE_URL",),
        )

    assert env_file.read_text(encoding="utf-8") == original


def test_rotates_short_placeholder_and_duplicate_secrets_and_deduplicates_keys(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    duplicate = "d" * 40
    env_file.write_text(
        "MCP_TRADING_ORDER_API_KEY=short\n"
        "APP_ENV=stale\n"
        "APP_ENV=other-stale\n"
        f"TRADING_SERVICE_AUTH_SECRET={duplicate}\n"
        f"TRADING_INTERNAL_SERVICE_AUTH_SECRET={duplicate}\n"
        "TRADING_INTERNAL_SERVICE_AUTH_SECRET=CHANGE_ME_INTERNAL_SERVICE_SECRET_123456\n"
        f"CEO_DISCORD_INGRESS_API_KEY={duplicate}\n",
        encoding="utf-8",
    )

    cli.configure_environment(
        runtime="local",
        env_file=env_file,
        generator=_generator("x" * 48, "y" * 48, "z" * 48),
    )
    text = env_file.read_text(encoding="utf-8")
    values = _values(env_file)
    secret_values = [values[key] for key in cli.SERVICE_SECRET_KEYS]

    assert text.count("APP_ENV=") == 1
    assert all(text.count(f"{key}=") == 1 for key in cli.SERVICE_SECRET_KEYS)
    assert values["TRADING_SERVICE_AUTH_SECRET"] == duplicate
    assert values["CEO_DISCORD_INGRESS_API_KEY"] == "z" * 48
    assert len(set(secret_values)) == 4
    assert all(len(value) >= 32 for value in secret_values)
    assert not any("change_me" in value.casefold() for value in secret_values)


def test_collapses_unmanaged_duplicates_without_changing_effective_value(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "# Keep the documented location.\n"
        "UNMANAGED=first\n"
        "MIDDLE=value\n"
        "export UNMANAGED='last value'\n",
        encoding="utf-8",
    )

    result = cli.configure_environment(
        runtime="local",
        env_file=env_file,
        generator=_generator("a" * 48, "b" * 48, "c" * 48, "d" * 48),
    )
    text = env_file.read_text(encoding="utf-8")

    assert result.deduplicated_keys == ("UNMANAGED",)
    assert text.count("UNMANAGED=") == 1
    assert _values(env_file)["UNMANAGED"] == "last value"
    assert text.index("UNMANAGED=") < text.index("MIDDLE=")


def test_aws_missing_required_values_does_not_touch_file(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.aws"
    original = "UNMANAGED=keep\nSUPABASE_URL=https://project.supabase.co\n"
    env_file.write_text(original, encoding="utf-8")
    generated = False

    def must_not_generate() -> str:
        nonlocal generated
        generated = True
        return "z" * 48

    with pytest.raises(cli.ConfigurationError, match="HEDGEFUND_TSDB_PASSWORD") as error:
        cli.configure_environment(
            runtime="aws", env_file=env_file, generator=must_not_generate
        )

    assert "https://project.supabase.co" not in str(error.value)
    assert env_file.read_text(encoding="utf-8") == original
    assert generated is False


def test_aws_sets_production_contract_without_fixture_grants(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env.aws"
    supabase_url = "https://project.supabase.co"
    timescale_password = "private-timescale-password"
    existing_secrets = ("m" * 48, "s" * 48, "i" * 48, "d" * 48)
    env_file.write_text(
        f"SUPABASE_URL={supabase_url}\n"
        f"HEDGEFUND_TSDB_PASSWORD={timescale_password}\n"
        f"MCP_TRADING_ORDER_API_KEY={existing_secrets[0]}\n"
        f"TRADING_SERVICE_AUTH_SECRET={existing_secrets[1]}\n"
        f"TRADING_INTERNAL_SERVICE_AUTH_SECRET={existing_secrets[2]}\n"
        f"CEO_DISCORD_INGRESS_API_KEY={existing_secrets[3]}\n"
        "PORTFOLIO_FIXTURE_TRADING_BOOKS_JSON=[{\"unsafe\":true}]\n",
        encoding="utf-8",
    )

    exit_code = cli.main(["--runtime", "aws", "--env-file", str(env_file)])
    captured = capsys.readouterr()
    values = _values(env_file)

    assert exit_code == 0
    assert values["APP_ENV"] == "production"
    assert values["PORTFOLIO_AUTH_MODE"] == "supabase_jwt"
    assert values["PORTFOLIO_AUTH_REQUIRED"] == "true"
    assert values["USER_PAPER_ORDER_WORKFLOW_ENABLED"] == "true"
    assert values["PORTFOLIO_FIXTURE_TRADING_BOOKS_JSON"] == "[]"
    assert values["LS_MARKET_ENV"] == "LIVE"
    assert values["HEDGEFUND_CONTROL_DB_NAME"] == "control"
    assert values["SUPABASE_URL"] == supabase_url
    assert values["HEDGEFUND_TSDB_PASSWORD"] == timescale_password
    managed_secrets = [
        values[key]
        for key in (*cli.SERVICE_SECRET_KEYS, *cli.AWS_DATABASE_SECRET_KEYS)
    ]
    assert len(set(managed_secrets)) == 8
    assert all(len(value) >= 32 for value in managed_secrets)
    assert all(
        cli._URL_SAFE_SECRET_RE.fullmatch(values[key])
        for key in cli.AWS_DATABASE_SECRET_KEYS
    )
    output = captured.out + captured.err
    for hidden in (*existing_secrets, supabase_url, timescale_password):
        assert hidden not in output


def test_aws_preserves_eight_distinct_secrets_atomically(tmp_path: Path) -> None:
    env_file = tmp_path / ".env.aws"
    keys = (*cli.SERVICE_SECRET_KEYS, *cli.AWS_DATABASE_SECRET_KEYS)
    preserved = {key: character * 40 for key, character in zip(keys, "abcdefgh")}
    env_file.write_text(
        "SUPABASE_URL=https://project.supabase.co\n"
        "HEDGEFUND_TSDB_PASSWORD=AdminPassword_1234567890\n"
        + "".join(f"{key}={value}\n" for key, value in preserved.items()),
        encoding="utf-8",
    )

    def must_not_generate() -> str:
        raise AssertionError("all eight valid credentials must be preserved")

    first = cli.configure_environment(
        runtime="aws", env_file=env_file, generator=must_not_generate
    )
    first_text = env_file.read_text(encoding="utf-8")
    second = cli.configure_environment(
        runtime="aws", env_file=env_file, generator=must_not_generate
    )

    assert first.generated_secret_keys == ()
    assert second.changed is False
    assert env_file.read_text(encoding="utf-8") == first_text
    assert all(_values(env_file)[key] == value for key, value in preserved.items())


def test_aws_rotates_duplicate_or_non_url_safe_database_passwords(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env.aws"
    duplicate = "d" * 40
    env_file.write_text(
        "SUPABASE_URL=https://project.supabase.co\n"
        "HEDGEFUND_TSDB_PASSWORD=AdminPassword_1234567890\n"
        f"MCP_TRADING_ORDER_API_KEY={duplicate}\n"
        f"TRADING_SERVICE_AUTH_SECRET={'s' * 40}\n"
        f"TRADING_INTERNAL_SERVICE_AUTH_SECRET={'i' * 40}\n"
        f"CEO_DISCORD_INGRESS_API_KEY={'s' * 40}\n"
        f"HEDGEFUND_RUNTIME_DB_PASSWORD={duplicate}\n"
        "HEDGEFUND_ORDER_DB_PASSWORD=contains/a/reserved/slash/xxxxxxxxxx\n"
        f"HEDGEFUND_TRADING_DB_PASSWORD={'t' * 40}\n"
        f"HEDGEFUND_ACCOUNTING_DB_PASSWORD={'a' * 40}\n",
        encoding="utf-8",
    )

    cli.configure_environment(
        runtime="aws",
        env_file=env_file,
        generator=_generator("c" * 48, "r" * 48, "o" * 48),
    )
    values = _values(env_file)
    managed = [values[key] for key in (*cli.SERVICE_SECRET_KEYS, *cli.AWS_DATABASE_SECRET_KEYS)]

    assert values["MCP_TRADING_ORDER_API_KEY"] == duplicate
    assert values["CEO_DISCORD_INGRESS_API_KEY"] == "c" * 48
    assert values["HEDGEFUND_RUNTIME_DB_PASSWORD"] == "r" * 48
    assert values["HEDGEFUND_ORDER_DB_PASSWORD"] == "o" * 48
    assert len(set(managed)) == 8
    assert all(
        cli._URL_SAFE_SECRET_RE.fullmatch(values[key])
        for key in cli.AWS_DATABASE_SECRET_KEYS
    )


def test_cli_never_prints_generated_or_preserved_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    env_file = tmp_path / ".env"
    preserved = "p" * 48
    generated_values = iter(("q" * 48, "r" * 48, "s" * 48))
    env_file.write_text(
        f"MCP_TRADING_ORDER_API_KEY={preserved}\n", encoding="utf-8"
    )
    monkeypatch.setattr(cli, "_new_secret", lambda: next(generated_values))

    assert cli.main(["--runtime", "local", "--env-file", str(env_file)]) == 0

    output = capsys.readouterr().out
    assert preserved not in output
    assert "q" * 48 not in output
    assert "r" * 48 not in output
    assert "s" * 48 not in output
    assert "generated 3 secret(s)" in output


def test_atomic_replace_failure_preserves_original_and_removes_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_file = tmp_path / ".env"
    original = "UNMANAGED=original\n"
    env_file.write_text(original, encoding="utf-8")

    def fail_replace(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        assert Path(source).parent == env_file.parent
        assert Path(destination) == env_file
        raise OSError("simulated OneDrive lock")

    monkeypatch.setattr(cli.os, "replace", fail_replace)
    with pytest.raises(cli.ConfigurationError, match="atomically replace") as error:
        cli.configure_environment(
            runtime="local",
            env_file=env_file,
            generator=_generator("a" * 48, "b" * 48, "c" * 48, "d" * 48),
        )

    assert "simulated OneDrive lock" not in str(error.value)
    assert env_file.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob("..env.*.tmp")) == []
