from __future__ import annotations

import pytest

from apps.api.portfolio_runtime import portfolio_data_mode


def test_portfolio_data_mode_defaults_to_production_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PORTFOLIO_DATA_MODE", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    assert portfolio_data_mode() == "production"


@pytest.mark.parametrize("app_env", ["local", "test"])
def test_portfolio_test_catalog_requires_explicit_nonproduction_environment(
    monkeypatch: pytest.MonkeyPatch,
    app_env: str,
) -> None:
    monkeypatch.setenv("PORTFOLIO_DATA_MODE", "test")
    monkeypatch.setenv("APP_ENV", app_env)
    assert portfolio_data_mode() == "test"


def test_portfolio_test_catalog_is_forbidden_in_production(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PORTFOLIO_DATA_MODE", "test")
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(RuntimeError, match="portfolio_test_data_forbidden"):
        portfolio_data_mode()


def test_unknown_portfolio_data_mode_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PORTFOLIO_DATA_MODE", "fallback")
    with pytest.raises(RuntimeError, match="unsupported_portfolio_data_mode"):
        portfolio_data_mode()
