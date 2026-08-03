"""Non-mutating paper E2E adapters.

These adapters verify that every workflow boundary can reach its Hermes
profile and that the declared handoff chain advances.  They intentionally do
not call a broker, OMS submit endpoint, ledger writer, Notion reporter, or
external database.  A future production adapter must be added separately and
must retain the same fail-closed behavior.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PROFILE_BY_STEP = {
    "research": "research-department",
    "trading": "trading-department",
    "risk": "risk-management",
    "qa": "qa-department",
    "oms-fill-gate": "trading-department",
    "accounting": "accounting-portfolio-department",
    "ceo": "ceo-agent",
}


class HermesSmokeError(RuntimeError):
    """Raised when a Hermes profile cannot complete a non-mutating smoke call."""


def _safe_process_error(process: subprocess.CompletedProcess[str]) -> str:
    """Return a short error without exposing command output or credentials."""

    combined = f"{process.stderr}\n{process.stdout}".lower()
    if "operation not permitted" in combined or "permission" in combined:
        return "hermes_profile_filesystem_permission"
    if "credential" in combined or "api key" in combined or "auth" in combined:
        return "hermes_credential_unavailable"
    return f"hermes_exit_{process.returncode}"


class HermesSmokeAdapter:
    """Call one Hermes profile with an exact, tool-free smoke prompt."""

    def __init__(self, repo_root: Path, *, executable: str | None = None, timeout: float | None = None):
        self.repo_root = repo_root
        self.executable = executable or os.environ.get("HERMES_BIN", "hermes")
        self.timeout = timeout or float(os.environ.get("HERMES_SMOKE_TIMEOUT_SECONDS", "45"))

    def invoke(
        self,
        step_id: str,
        input_contract: str,
        output_contract: str,
        context: Mapping[str, object],
    ) -> str:
        profile = PROFILE_BY_STEP.get(step_id)
        if profile is None:
            raise HermesSmokeError(f"profile_not_registered:{step_id}")

        case = context.get("case_request")
        if not isinstance(case, Mapping) or case.get("stage") != "paper":
            raise HermesSmokeError("paper_stage_required")

        marker = f"HGFINANCE_{step_id.upper().replace('-', '_')}_SMOKE_OK"
        prompt = (
            f"Reply with exactly {marker}. This is a non-mutating paper E2E smoke test. "
            "Do not call tools, place orders, write a ledger, or modify external data."
        )
        try:
            process = subprocess.run(
                [self.executable, "--profile", profile, "-z", prompt],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env=os.environ.copy(),
            )
        except FileNotFoundError as exc:
            raise HermesSmokeError("hermes_executable_not_found") from exc
        except subprocess.TimeoutExpired as exc:
            raise HermesSmokeError("hermes_smoke_timeout") from exc
        except OSError as exc:
            raise HermesSmokeError(f"hermes_os_error:{type(exc).__name__}") from exc

        if process.returncode != 0:
            raise HermesSmokeError(_safe_process_error(process))
        if marker not in process.stdout:
            raise HermesSmokeError("hermes_marker_mismatch")

        return (
            f"hermes_smoke=PASS profile={profile} "
            f"input={input_contract} output={output_contract} paper_no_side_effects=true"
        )


def build_paper_e2e_handlers(
    repo_root: Path,
    *,
    smoke_adapter: HermesSmokeAdapter | None = None,
) -> dict[str, Any]:
    """Build handlers for every realtime investment boundary.

    ``smoke_adapter`` is injectable so tests can validate the complete handler
    registry without invoking Hermes or requiring credentials.
    """

    adapter = smoke_adapter or HermesSmokeAdapter(repo_root)
    handlers: dict[str, Any] = {}
    for step_id in PROFILE_BY_STEP:
        handlers[step_id] = _handler_for(step_id, adapter)
    return handlers


def _handler_for(step_id: str, adapter: HermesSmokeAdapter):
    def handler(
        input_contract: str,
        output_contract: str,
        context: Mapping[str, object],
    ) -> str:
        return adapter.invoke(step_id, input_contract, output_contract, context)

    return handler

