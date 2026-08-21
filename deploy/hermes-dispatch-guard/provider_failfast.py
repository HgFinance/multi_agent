"""Provider fail-fast hooks for dispatcher-owned Hermes workers.

The department workers run the Hermes binary from the dispatcher container.
That container already propagates this directory through ``PYTHONPATH`` via
``sitecustomize``.  Keeping this hook here lets the repository pin the small
provider policy without copying or vendoring the upstream Hermes runtime.

Only the three provider-backed analysis profiles are enabled.  The helper is
deliberately defensive: if an upstream Hermes contract moves, the hook fails
open and the native retry behaviour remains in charge.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, replace
from typing import Any

LOG = logging.getLogger(__name__)

TARGET_PROFILES = frozenset({
    "research-department",
    "quant-backtest-department",
    "risk-management",
})

PROVIDER_QUOTA = "PROVIDER_QUOTA"
PROVIDER_AUTH = "PROVIDER_AUTH"
PROVIDER_TRANSIENT = "PROVIDER_TRANSIENT"
PROVIDER_PROTOCOL_FAILURE = "PROVIDER_PROTOCOL_FAILURE"
UNKNOWN_PROVIDER_FAILURE = "UNKNOWN_PROVIDER_FAILURE"

_HARD_QUOTA_CODES = frozenset({
    "insufficient_quota",
    "insufficient_credits",
    "no_usable_credits",
    "balance_depleted",
    "billing_not_active",
    "payment_required",
    "member_spend_cap_exceeded",
    "quota_exhausted",
})
_TRANSIENT_QUOTA_CODES = frozenset({
    "rate_limit",
    "rate_limit_exceeded",
    "too_many_requests",
    "throttled",
})
_HARD_QUOTA_MARKERS = (
    "insufficient_quota",
    "insufficient quota",
    "quota exhausted",
    "credits exhausted",
    "no usable credits",
    "balance depleted",
    "billing hard limit",
    "payment required",
    "exceeded your current quota",
    "out of extra usage",
    "account is deactivated",
    "spending limit",
    "spend cap exceeded",
)
_TRANSIENT_QUOTA_MARKERS = (
    "try again",
    "retry after",
    "resets at",
    "reset in",
    "wait ",
    "temporarily",
    "per minute",
    "per day",
    "rate limit",
    "too many requests",
    "throttl",
)


@dataclass
class _Metrics:
    started_at: float
    retryable_errors: int = 0
    hard_detection_ms: float | None = None
    token_refresh_attempts: int = 0
    failure_code: str | None = None
    fail_fast: bool = False
    resolution_codes: list[str] | None = None


_metrics_local = threading.local()


def _current_metrics() -> _Metrics | None:
    return getattr(_metrics_local, "value", None)


def _classify_runtime_failure(error: Any) -> str | None:
    """Classify provider-resolution failures using typed fields first.

    ``resolve_runtime_provider`` can fail before ``AIAgent.run_conversation``
    starts (for example, Codex refresh-token quota/auth failures).  Those
    errors have no HTTP response object, so the normal API classifier never
    sees them.  Keep this classifier deliberately narrow: a structured
    ``AuthError`` field/code is authoritative; opaque text alone is not.
    """
    code = _normalise(
        getattr(error, "code", None)
        or getattr(error, "error_code", None)
        or getattr(error, "reason", None)
    )
    status = getattr(error, "status_code", None)
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = None

    if code in {"codex_rate_limited", "rate_limit", "rate_limit_exceeded", "too_many_requests"}:
        return PROVIDER_QUOTA if code == "codex_rate_limited" else PROVIDER_TRANSIENT
    if code in _HARD_QUOTA_CODES:
        return PROVIDER_QUOTA
    if bool(getattr(error, "relogin_required", False)):
        return PROVIDER_AUTH
    if code in {
        "invalid_grant",
        "invalid_token",
        "invalid_api_key",
        "authentication_error",
        "auth_failed",
        "codex_auth_missing",
        "codex_auth_missing_access_token",
        "codex_auth_missing_refresh_token",
        "codex_auth_invalid_shape",
    } or status in {401, 403}:
        return PROVIDER_AUTH
    if code in _TRANSIENT_QUOTA_CODES or (status is not None and status >= 500):
        return PROVIDER_TRANSIENT
    if code in {"invalid_provider", "model_not_found", "unsupported_model", "unsupported_provider"}:
        return PROVIDER_PROTOCOL_FAILURE
    return None


def _resolution_fail_fast_code(codes: list[str] | None) -> str | None:
    """Return a terminal code only when every resolution failure is terminal."""
    if not codes or any(code not in {
        PROVIDER_QUOTA,
        PROVIDER_AUTH,
        PROVIDER_PROTOCOL_FAILURE,
    } for code in codes):
        return None
    # Preserve the most specific cause when a primary provider and its
    # configured fallback both failed. Quota/auth are availability causes;
    # protocol is a configuration cause and should not mask either.
    for preferred in (PROVIDER_QUOTA, PROVIDER_AUTH, PROVIDER_PROTOCOL_FAILURE):
        if preferred in codes:
            return preferred
    return None


def is_target_worker() -> bool:
    """Return true only for a dispatcher-owned Research/Quant/Risk worker."""
    return bool(os.environ.get("HERMES_KANBAN_TASK")) and (
        os.environ.get("HERMES_PROFILE", "").strip() in TARGET_PROFILES
    )


def _normalise(value: Any) -> str:
    return str(value or "").strip().lower()


def _body_code(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    candidates = [body.get("code"), body.get("error_code"), body.get("type")]
    error = body.get("error")
    if isinstance(error, dict):
        candidates.extend((error.get("code"), error.get("type")))
    for candidate in candidates:
        value = _normalise(candidate)
        if value:
            return value
    return ""


def _body_text(body: Any) -> str:
    if not body:
        return ""
    try:
        return json.dumps(body, sort_keys=True, ensure_ascii=False).lower()
    except (TypeError, ValueError):
        return _normalise(body)


def is_hard_quota_429(
    *,
    status_code: Any,
    error_code: Any = "",
    message: Any = "",
    body: Any = None,
) -> bool:
    """Recognise account/quota exhaustion without misclassifying throttling.

    Structured error codes take precedence.  Text fallback is intentionally
    narrow and requires an explicit exhaustion/billing marker; a bare
    ``quota`` or ``rate limit`` remains transient.
    """
    code = _normalise(error_code) or _body_code(body)
    try:
        status = int(status_code)
    except (TypeError, ValueError):
        status = None
    # A structured billing code is authoritative only when the transport is
    # absent or itself consistent with an account/billing response. A 5xx
    # must remain transient even if a provider echoed a stale quota code.
    if code in _HARD_QUOTA_CODES and status in {None, 400, 402, 403, 429}:
        return True
    # A structured transient code must win over a misleading prose message.
    # Some providers describe a temporary window as an exceeded quota while
    # still returning a retryable rate-limit code.
    if code in _TRANSIENT_QUOTA_CODES:
        return False
    if status != 429:
        return False

    text = " ".join((_normalise(message), _body_text(body))).strip()
    if not any(marker in text for marker in _HARD_QUOTA_MARKERS):
        return False
    return not any(marker in text for marker in _TRANSIENT_QUOTA_MARKERS)


def _classify_failure_code(classified: Any) -> str | None:
    """Map Hermes' structured enum to the repository handoff vocabulary."""
    reason = _normalise(getattr(getattr(classified, "reason", None), "value", ""))
    if reason in {"auth", "auth_permanent"}:
        return PROVIDER_AUTH
    if reason == "billing":
        return PROVIDER_QUOTA
    if reason in {"model_not_found", "provider_policy_blocked", "format_error"}:
        return PROVIDER_PROTOCOL_FAILURE
    if reason in {
        "rate_limit", "upstream_rate_limit", "overloaded", "server_error", "timeout"
    }:
        return PROVIDER_TRANSIENT
    if getattr(classified, "retryable", True):
        return PROVIDER_TRANSIENT
    return UNKNOWN_PROVIDER_FAILURE


def _error_details(error: Any, classifier: Any, classified: Any) -> tuple[Any, str, str, Any]:
    status = getattr(classified, "status_code", None)
    if status is None:
        try:
            status = classifier._extract_status_code(error)
        except Exception:  # noqa: BLE001 - upstream helper is optional
            status = getattr(error, "status_code", None)
    try:
        body = classifier._extract_error_body(error)
    except Exception:  # noqa: BLE001 - upstream helper is optional
        body = getattr(error, "body", None) or {}
    try:
        code = classifier._extract_error_code(body)
    except Exception:  # noqa: BLE001 - upstream helper is optional
        code = _body_code(body)
    message = " ".join(
        part for part in (
            str(getattr(classified, "message", "") or ""),
            str(error or ""),
        ) if part
    )
    return status, _normalise(code), message, body


def _install_classifier() -> None:
    import agent.error_classifier as classifier

    original = getattr(classifier, "classify_api_error", None)
    if not callable(original) or getattr(original, "_hgfinance_provider_hook", False):
        return

    def classify_api_error(error: Any, *args: Any, **kwargs: Any) -> Any:
        classified = original(error, *args, **kwargs)
        metrics = _current_metrics()
        status, error_code, message, body = _error_details(
            error, classifier, classified
        )
        detection_started = time.monotonic()
        hard_quota = is_hard_quota_429(
            status_code=status,
            error_code=error_code,
            message=message,
            body=body,
        )
        detection_ms = (time.monotonic() - detection_started) * 1000.0
        if hard_quota:
            if metrics is not None:
                metrics.failure_code = PROVIDER_QUOTA
                metrics.fail_fast = True
                metrics.hard_detection_ms = detection_ms
            # Do not rotate a credential or activate another configured
            # fallback: an account quota wall is not fixed by retrying the
            # same provider and must reach the department availability gate.
            context = dict(getattr(classified, "error_context", {}) or {})
            context.update({
                "hgfinance_provider_failure_code": PROVIDER_QUOTA,
                "hgfinance_fail_fast": True,
            })
            return replace(
                classified,
                reason=classifier.FailoverReason.billing,
                retryable=False,
                should_rotate_credential=False,
                should_fallback=False,
                error_context=context,
            )

        if metrics is not None:
            if getattr(classified, "retryable", True):
                metrics.retryable_errors += 1
            code = _classify_failure_code(classified)
            if code and code != PROVIDER_TRANSIENT:
                metrics.failure_code = code
                if code in {
                    PROVIDER_QUOTA,
                    PROVIDER_AUTH,
                    PROVIDER_PROTOCOL_FAILURE,
                }:
                    metrics.fail_fast = True
                    metrics.hard_detection_ms = detection_ms
            elif code == PROVIDER_TRANSIENT:
                # Do not carry a previous hard state into a later transient
                # call after a successful credential refresh.
                if not metrics.fail_fast:
                    metrics.failure_code = PROVIDER_TRANSIENT
        return classified

    classify_api_error._hgfinance_provider_hook = True
    classify_api_error._hgfinance_provider_original = original
    classifier.classify_api_error = classify_api_error


def _install_runtime_resolution_hooks() -> None:
    """Cover provider failures that happen before the conversation loop."""
    if not is_target_worker():
        return

    from hermes_cli import runtime_provider

    # ``runtime_provider`` imports these auth functions directly, so wrap the
    # module-local aliases rather than the auth module after import. Counting
    # the call is intentionally scoped to this preflight metrics context; the
    # conversation-loop refresh counter remains responsible for mid-turn 401s.
    for name in (
        "resolve_codex_runtime_credentials",
        "resolve_xai_oauth_runtime_credentials",
    ):
        original_credentials = getattr(runtime_provider, name, None)
        if not callable(original_credentials) or getattr(
            original_credentials, "_hgfinance_provider_hook", False
        ):
            continue

        def resolve_credentials(
            *args: Any,
            _original: Any = original_credentials,
            **kwargs: Any,
        ) -> Any:
            metrics = _current_metrics()
            if metrics is not None:
                metrics.token_refresh_attempts += 1
            return _original(*args, **kwargs)

        resolve_credentials._hgfinance_provider_hook = True
        resolve_credentials._hgfinance_provider_original = original_credentials
        setattr(runtime_provider, name, resolve_credentials)

    original_resolve = getattr(runtime_provider, "resolve_runtime_provider", None)
    if callable(original_resolve) and not getattr(
        original_resolve, "_hgfinance_provider_hook", False
    ):
        def resolve(*args: Any, **kwargs: Any) -> Any:
            try:
                return original_resolve(*args, **kwargs)
            except Exception as error:  # noqa: BLE001 - preserve native error path
                metrics = _current_metrics()
                code = _classify_runtime_failure(error)
                if metrics is not None:
                    if metrics.resolution_codes is None:
                        metrics.resolution_codes = []
                    metrics.resolution_codes.append(
                        code or UNKNOWN_PROVIDER_FAILURE
                    )
                    if code in {
                        PROVIDER_QUOTA,
                        PROVIDER_AUTH,
                        PROVIDER_PROTOCOL_FAILURE,
                    }:
                        metrics.failure_code = code
                        metrics.fail_fast = True
                        if metrics.hard_detection_ms is None:
                            metrics.hard_detection_ms = 0.0
                raise

        resolve._hgfinance_provider_hook = True
        resolve._hgfinance_provider_original = original_resolve
        runtime_provider.resolve_runtime_provider = resolve

    from hermes_cli.cli_agent_setup_mixin import CLIAgentSetupMixin

    original_ensure = getattr(CLIAgentSetupMixin, "_ensure_runtime_credentials", None)
    if not callable(original_ensure) or getattr(
        original_ensure, "_hgfinance_provider_hook", False
    ):
        return

    def ensure(self: Any, *args: Any, **kwargs: Any) -> Any:
        previous = getattr(_metrics_local, "value", None)
        metrics = _Metrics(started_at=time.monotonic(), resolution_codes=[])
        _metrics_local.value = metrics
        try:
            ready = original_ensure(self, *args, **kwargs)
            if not ready:
                code = _resolution_fail_fast_code(metrics.resolution_codes)
                if code:
                    error_text = "provider resolution failed"
                    if metrics.resolution_codes:
                        error_text += f" ({metrics.resolution_codes[-1]})"
                    result = {
                        "failed": True,
                        "error": error_text,
                        "failure_reason": code,
                        "api_calls": 0,
                        "provider_metrics": {
                            "provider_call_count": 0,
                            "retry_count": 0,
                            "token_refresh_attempts": metrics.token_refresh_attempts,
                            "hard_failure_detection_ms": metrics.hard_detection_ms,
                            "worker_total_ms": (
                                time.monotonic() - metrics.started_at
                            ) * 1000.0,
                        },
                    }
                    _block_provider_task(result, code)
            return ready
        finally:
            _metrics_local.value = previous

    ensure._hgfinance_provider_hook = True
    ensure._hgfinance_provider_original = original_ensure
    CLIAgentSetupMixin._ensure_runtime_credentials = ensure


def _install_agent_hooks() -> None:
    if not is_target_worker():
        return
    _install_runtime_resolution_hooks()
    # AIAgent lives in the Hermes repository root (``run_agent.py``), not in
    # the ``agent`` package. Keep this import lazy because the dispatcher
    # itself does not need to construct an agent.
    from run_agent import AIAgent

    if getattr(AIAgent.run_conversation, "_hgfinance_provider_hook", False):
        return

    original_run = AIAgent.run_conversation
    original_refresh = getattr(AIAgent, "_try_refresh_codex_client_credentials", None)
    original_pool = getattr(AIAgent, "_recover_with_credential_pool", None)
    original_fallback = getattr(AIAgent, "_try_activate_fallback", None)

    if callable(original_refresh):
        def refresh(self: Any, *args: Any, **kwargs: Any) -> Any:
            metrics = _current_metrics()
            if metrics is not None:
                metrics.token_refresh_attempts += 1
            result = original_refresh(self, *args, **kwargs)
            if result and metrics is not None:
                metrics.fail_fast = False
                metrics.failure_code = None
            return result

        AIAgent._try_refresh_codex_client_credentials = refresh

    if callable(original_pool):
        def recover_pool(self: Any, *args: Any, **kwargs: Any) -> Any:
            metrics = _current_metrics()
            if metrics is not None and metrics.fail_fast:
                current = kwargs.get("has_retried_429", False)
                return False, current
            return original_pool(self, *args, **kwargs)

        AIAgent._recover_with_credential_pool = recover_pool

    if callable(original_fallback):
        def activate_fallback(self: Any, *args: Any, **kwargs: Any) -> Any:
            metrics = _current_metrics()
            if metrics is not None and metrics.fail_fast:
                return False
            return original_fallback(self, *args, **kwargs)

        AIAgent._try_activate_fallback = activate_fallback

    def run(self: Any, *args: Any, **kwargs: Any) -> Any:
        previous = getattr(_metrics_local, "value", None)
        metrics = _Metrics(started_at=time.monotonic())
        _metrics_local.value = metrics
        try:
            result = original_run(self, *args, **kwargs)
            if isinstance(result, dict) and result.get("failed"):
                code = metrics.failure_code
                if not code:
                    code = _classify_failure_code_from_result(result)
                if code:
                    result["provider_failure_code"] = code
                    # Keep the subprocess on Hermes' ordinary failure exit
                    # path after we have durably blocked the task.  The
                    # native ``billing``/``rate_limit`` values are mapped to
                    # EX_TEMPFAIL by the CLI, which is correct for transient
                    # throttling but misleading for a terminal hard quota or
                    # auth block.
                    if code in {
                        PROVIDER_QUOTA,
                        PROVIDER_AUTH,
                        PROVIDER_PROTOCOL_FAILURE,
                    }:
                        result["failure_reason"] = code
                calls = int(result.get("api_calls") or 0)
                result["provider_metrics"] = {
                    "provider_call_count": calls,
                    "retry_count": max(0, min(metrics.retryable_errors, calls - 1)),
                    "token_refresh_attempts": metrics.token_refresh_attempts,
                    "hard_failure_detection_ms": metrics.hard_detection_ms,
                    "worker_total_ms": (time.monotonic() - metrics.started_at) * 1000.0,
                }
                if code in {PROVIDER_QUOTA, PROVIDER_AUTH, PROVIDER_PROTOCOL_FAILURE}:
                    _block_provider_task(result, code)
            return result
        finally:
            _metrics_local.value = previous

    run._hgfinance_provider_hook = True
    AIAgent.run_conversation = run


def _classify_failure_code_from_result(result: dict[str, Any]) -> str | None:
    reason = _normalise(result.get("failure_reason"))
    if reason in {"auth", "auth_permanent"}:
        return PROVIDER_AUTH
    if reason in {"billing"}:
        return PROVIDER_QUOTA
    if reason in {"rate_limit", "upstream_rate_limit", "overloaded", "server_error", "timeout"}:
        return PROVIDER_TRANSIENT
    return UNKNOWN_PROVIDER_FAILURE if result.get("failed") else None


def _block_provider_task(result: dict[str, Any], code: str) -> None:
    """Use the existing Kanban block transition for hard provider failures."""
    task_id = os.environ.get("HERMES_KANBAN_TASK", "").strip()
    if not task_id:
        return
    try:
        from hermes_cli import kanban_db as kb

        conn = kb.connect()
        try:
            detail = str(result.get("error") or result.get("final_response") or "")
            detail = detail.replace("\x00", " ").strip()[:800]
            try:
                from agent.redact import redact_sensitive_text

                detail = redact_sensitive_text(detail, force=True)
            except Exception:
                LOG.debug("provider error redaction unavailable", exc_info=True)
            reason = f"{code}: {detail}" if detail else code
            ok = kb.block_task(
                conn,
                task_id,
                reason=reason,
                kind="capability",
                expected_run_id=_run_id(),
            )
            if ok:
                LOG.warning(
                    "provider fail-fast blocked task=%s code=%s calls=%s",
                    task_id,
                    code,
                    result.get("api_calls"),
                )
        finally:
            conn.close()
    except Exception:
        # A provider failure must never become a worker crash because the
        # optional handoff annotation could not be written. The dispatcher
        # will retain Hermes' normal bounded crash handling in that case.
        LOG.exception("provider fail-fast Kanban handoff failed task=%s", task_id)


def _run_id() -> int | None:
    raw = os.environ.get("HERMES_KANBAN_RUN_ID", "").strip()
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


def install() -> None:
    """Install hooks; all failures are fail-open to native Hermes behavior."""
    try:
        _install_classifier()
        _install_agent_hooks()
    except Exception:
        LOG.exception("provider fail-fast hook unavailable; using native Hermes retry")


__all__ = [
    "PROVIDER_AUTH",
    "PROVIDER_PROTOCOL_FAILURE",
    "PROVIDER_QUOTA",
    "PROVIDER_TRANSIENT",
    "TARGET_PROFILES",
    "UNKNOWN_PROVIDER_FAILURE",
    "install",
    "is_hard_quota_429",
]
