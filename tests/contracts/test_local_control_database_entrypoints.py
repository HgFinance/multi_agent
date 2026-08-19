"""Host-side tools must never infer a write target from hosted DATABASE_URL."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_run_local_stack_uses_only_explicit_local_control_database() -> None:
    source = (ROOT / "scripts/run_local_stack.sh").read_text(encoding="utf-8")
    bootstrap = source.split('mkdir -p "$LOG_DIR"', 1)[0]

    assert "LOCAL_CONTROL_DATABASE_URL" in bootstrap
    assert "127.0.0.1:54322" in bootstrap
    assert "startswith('DATABASE_URL=')" not in bootstrap
    assert 'export DATABASE_URL="$LOCAL_CONTROL_DATABASE_URL"' in bootstrap
    assert 'export CONTROL_DATABASE_URL="$LOCAL_CONTROL_DATABASE_URL"' in bootstrap
    assert 'export PORTFOLIO_AUTH_MODE="fixture"' in bootstrap


def test_factory_e2e_uses_only_explicit_local_control_database() -> None:
    source = (
        ROOT / "departments/04-quant-backtest/pipeline/factory_e2e.py"
    ).read_text(encoding="utf-8")
    bootstrap = source.split("def _say", 1)[0]

    assert "LOCAL_CONTROL_DATABASE_URL" in bootstrap
    assert "127.0.0.1:54322" in bootstrap
    assert 'line.startswith("DATABASE_URL=")' not in bootstrap
    assert 'os.environ["DATABASE_URL"]' not in bootstrap
    assert 'hostname.endswith(".supabase.com")' in bootstrap


def test_environment_template_names_the_host_local_contract() -> None:
    template = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert (
        "LOCAL_CONTROL_DATABASE_URL="
        "postgresql://postgres:postgres@127.0.0.1:54322/postgres"
    ) in template
