"""Provider fail-fast policy tests without making live provider calls."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

from orchestration.failure_taxonomy import FailureKind, classify_failure

_ROOT = Path(__file__).resolve().parents[2]
_MODULE_PATH = _ROOT / "deploy" / "hermes-dispatch-guard" / "provider_failfast.py"
_SPEC = importlib.util.spec_from_file_location("provider_failfast", _MODULE_PATH)
assert _SPEC and _SPEC.loader
provider_failfast = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = provider_failfast
_SPEC.loader.exec_module(provider_failfast)


def test_structured_hard_quota_429_is_fail_fast() -> None:
    assert provider_failfast.is_hard_quota_429(
        status_code=429,
        error_code="insufficient_quota",
        message="quota exceeded",
    )


def test_structured_hard_quota_code_is_fail_fast_across_billing_statuses() -> None:
    assert provider_failfast.is_hard_quota_429(
        status_code=402,
        error_code="insufficient_quota",
    )
    assert provider_failfast.is_hard_quota_429(
        status_code=403,
        error_code="billing_not_active",
    )
    assert provider_failfast.is_hard_quota_429(
        status_code=None,
        body={"error": {"code": "quota_exhausted"}},
    )


def test_hard_quota_text_requires_explicit_exhaustion_signal() -> None:
    assert provider_failfast.is_hard_quota_429(
        status_code=429,
        message="credits exhausted for this account",
    )
    assert not provider_failfast.is_hard_quota_429(
        status_code=429,
        message="quota exceeded, retry after the window resets",
    )


def test_codex_usage_limit_exhaustion_is_hard_quota() -> None:
    assert provider_failfast.is_hard_quota_429(
        status_code=429,
        message="HTTP 429: The usage limit has been reached",
    )


def test_transient_429_and_server_errors_are_not_hard_quota() -> None:
    assert not provider_failfast.is_hard_quota_429(
        status_code=429,
        error_code="rate_limit_exceeded",
        message="try again in 2 seconds",
    )
    assert not provider_failfast.is_hard_quota_429(
        status_code=429,
        error_code="rate_limit_exceeded",
        message="exceeded your current quota; retry after the window resets",
    )
    assert not provider_failfast.is_hard_quota_429(
        status_code=503,
        error_code="insufficient_quota",
        message="temporarily unavailable",
    )


def test_policy_is_scoped_to_the_three_analysis_profiles() -> None:
    assert provider_failfast.TARGET_PROFILES == {
        "research-department",
        "quant-backtest-department",
        "risk-management",
    }


def test_structured_handoff_codes_are_not_collapsed_to_protocol() -> None:
    assert classify_failure("PROVIDER_QUOTA: quota exhausted").kind is FailureKind.CAPACITY
    assert classify_failure("PROVIDER_AUTH: token refresh failed").kind is FailureKind.CREDENTIALS


def test_unknown_provider_failure_remains_outside_fail_fast_policy() -> None:
    assert classify_failure("UNKNOWN_PROVIDER_FAILURE: opaque SDK error").kind is FailureKind.UNKNOWN


def test_native_reasons_map_to_structured_handoff_codes() -> None:
    assert provider_failfast._classify_failure_code(
        SimpleNamespace(reason=SimpleNamespace(value="auth"), retryable=False)
    ) == provider_failfast.PROVIDER_AUTH
    assert provider_failfast._classify_failure_code(
        SimpleNamespace(reason=SimpleNamespace(value="billing"), retryable=False)
    ) == provider_failfast.PROVIDER_QUOTA
    assert provider_failfast._classify_failure_code(
        SimpleNamespace(reason=SimpleNamespace(value="server_error"), retryable=True)
    ) == provider_failfast.PROVIDER_TRANSIENT
    assert provider_failfast._classify_failure_code(
        SimpleNamespace(reason=SimpleNamespace(value="model_not_found"), retryable=False)
    ) == provider_failfast.PROVIDER_PROTOCOL_FAILURE


def test_runtime_resolution_uses_typed_quota_and_auth_fields() -> None:
    assert provider_failfast._classify_runtime_failure(
        SimpleNamespace(code="codex_rate_limited", relogin_required=False)
    ) == provider_failfast.PROVIDER_QUOTA
    assert provider_failfast._classify_runtime_failure(
        SimpleNamespace(code="invalid_grant", relogin_required=True)
    ) == provider_failfast.PROVIDER_AUTH


def test_runtime_resolution_keeps_transient_and_unknown_failures_native() -> None:
    assert provider_failfast._classify_runtime_failure(
        SimpleNamespace(status_code=503, code="")
    ) == provider_failfast.PROVIDER_TRANSIENT
    assert provider_failfast._classify_runtime_failure(
        SimpleNamespace(code="opaque_sdk_error")
    ) is None


def test_runtime_resolution_requires_all_attempts_to_be_terminal() -> None:
    assert provider_failfast._resolution_fail_fast_code([
        provider_failfast.PROVIDER_QUOTA,
        provider_failfast.PROVIDER_AUTH,
    ]) == provider_failfast.PROVIDER_QUOTA
    assert provider_failfast._resolution_fail_fast_code([
        provider_failfast.PROVIDER_AUTH,
        provider_failfast.PROVIDER_TRANSIENT,
    ]) is None
    assert provider_failfast._resolution_fail_fast_code([]) is None
