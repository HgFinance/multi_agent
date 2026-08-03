"""CEO paper adapter using the configured Hermes CEO profile.

This adapter is intentionally separate from ``departments/00-ceo-office``
``scripts.py``. The latter is the CEO daily-report prototype and currently
has its own legacy model path. The cross-department paper boundary must use
the profile selected in ``departments/00-ceo-office/hermes/config.yaml``.

The model may write a recommendation and narrative, but it can never turn a
paper run into a binding order, Risk approval, or ledger posting.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path

import yaml


class CeoAdapterError(RuntimeError):
    """Raised when the CEO Hermes adapter cannot produce a valid summary."""


class LunaCeoAdapter:
    """Call the CEO Hermes profile with a bounded, JSON-only paper prompt."""

    def __init__(
        self,
        repo_root: Path,
        *,
        executable: str | None = None,
        profile: str = "ceo-agent",
        timeout: float | None = None,
    ) -> None:
        self.repo_root = repo_root
        self.executable = executable or os.environ.get("HERMES_BIN", "hermes")
        self.profile = profile
        self.timeout = timeout or float(os.environ.get("HERMES_CEO_TIMEOUT_SECONDS", "90"))
        self.model = _load_model_config(repo_root)

    def decide(
        self,
        *,
        case_request: Mapping[str, object],
        department_reports: Mapping[str, object],
    ) -> dict[str, object]:
        bundle = {
            "case_request": dict(case_request),
            "department_reports": _bounded_reports(department_reports),
            "paper_controls": {
                "orders_allowed": False,
                "ledger_writes_allowed": False,
                "risk_override_allowed": False,
                "binding_decision_allowed": False,
            },
        }
        prompt = f"""You are the CEO investment committee summarizer for HgFinance.
This is a PAPER-only cross-department case. Do not call tools, submit orders,
approve Risk limits, modify a ledger, or claim that a paper smoke result is a
live investment approval.

Aggregate the supplied Research, Trading, Risk, QA, OMS/Fill, and Accounting
reports. Preserve deterministic Risk and QA verdicts exactly. Return ONLY one
JSON object with these keys:
recommendation (BUY, RESIZE, HOLD, REJECT, or ESCALATE),
confidence (number 0 to 1), rationale (short string), escalate (boolean).

Input bundle:
{json.dumps(bundle, ensure_ascii=False, sort_keys=True, default=str)}"""
        try:
            process = subprocess.run(
                [self.executable, "--profile", self.profile, "-z", prompt],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                check=False,
                env=os.environ.copy(),
            )
        except FileNotFoundError as exc:
            raise CeoAdapterError("ceo_executable_not_found") from exc
        except subprocess.TimeoutExpired as exc:
            raise CeoAdapterError("ceo_timeout") from exc
        except OSError as exc:
            raise CeoAdapterError(f"ceo_os_error:{type(exc).__name__}") from exc

        if process.returncode != 0:
            raise CeoAdapterError(f"ceo_exit_{process.returncode}")
        try:
            decision = _parse_decision(process.stdout)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise CeoAdapterError("ceo_invalid_json") from exc

        risk = department_reports.get("risk")
        qa = department_reports.get("qa")
        risk_verdict = _value(risk, "verdict")
        qa_verdict = _value(qa, "verdict")
        risk_ok = risk_verdict in {"approve", "APPROVE"}
        qa_ok = qa_verdict in {"PASS", "pass"}
        # The paper adapter never emits a binding action, even if the model
        # recommends BUY. A failed gate always forces the safe direction.
        safe_recommendation = str(decision["recommendation"]).upper()
        if not risk_ok or not qa_ok:
            safe_recommendation = "HOLD"
        return {
            "recommendation": safe_recommendation,
            "model_recommendation": decision["recommendation"],
            "confidence": decision["confidence"],
            "rationale": decision["rationale"],
            "escalate": bool(decision["escalate"] or not risk_ok or not qa_ok),
            "binding_decision": "HOLD / ESCALATE",
            "binding": False,
            "runtime": {
                "profile": self.profile,
                "provider": self.model["provider"],
                "model": self.model["model"],
                "call_status": "succeeded",
            },
            "risk_verdict_preserved": risk_verdict,
            "qa_verdict_preserved": qa_verdict,
        }


def _parse_decision(stdout: str) -> dict[str, object]:
    match = re.search(r"\{.*\}", stdout, re.DOTALL)
    if match is None:
        raise ValueError("CEO response contains no JSON object")
    payload = json.loads(match.group(0))
    if not isinstance(payload, dict):
        raise TypeError("CEO response is not an object")
    recommendation = str(payload.get("recommendation", "")).upper()
    if recommendation not in {"BUY", "RESIZE", "HOLD", "REJECT", "ESCALATE"}:
        raise ValueError("CEO recommendation is outside the allow-list")
    confidence = float(payload.get("confidence", 0.0))
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("CEO confidence is outside 0..1")
    rationale = str(payload.get("rationale", "")).strip()
    if not rationale:
        raise ValueError("CEO rationale is empty")
    return {
        "recommendation": recommendation,
        "confidence": confidence,
        "rationale": rationale,
        "escalate": bool(payload.get("escalate", False)),
    }


def _load_model_config(repo_root: Path) -> dict[str, str]:
    path = repo_root / "departments/00-ceo-office/hermes/config.yaml"
    try:
        config = yaml.safe_load(path.read_text(encoding="utf-8"))
        model = config["model"]
        provider = str(model["provider"]).strip()
        name = str(model.get("default", model.get("model", ""))).strip()
    except (KeyError, OSError, TypeError, yaml.YAMLError) as exc:
        raise CeoAdapterError("ceo_model_config_invalid") from exc
    if not provider or not name:
        raise CeoAdapterError("ceo_model_config_invalid")
    return {"provider": provider, "model": name}


def _bounded_reports(reports: Mapping[str, object]) -> dict[str, object]:
    bounded: dict[str, object] = {}
    for name, report in reports.items():
        if isinstance(report, Mapping):
            bounded[name] = {
                key: value
                for key, value in report.items()
                if key not in {"report_markdown", "raw", "tool_results"}
            }
        else:
            bounded[name] = str(report)[:2000]
    return bounded


def _value(value: object, key: str) -> object:
    return value.get(key) if isinstance(value, Mapping) else None
