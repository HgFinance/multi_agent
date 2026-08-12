"""Deterministic internal-audit checks for QA operational traces."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

CALCULATION_VERSION = "qa-internal-audit-v1"
FORBIDDEN_CROSS_DOMAIN_ACTIONS = frozenset(
    {
        "oms.submit",
        "ledger.write",
        "risk.limit.write",
        "risk.trading_state.write",
        "risk.trading_state.clear",
    }
)


class InternalAuditDecision(StrEnum):
    PASS = "PASS"
    ESCALATE = "ESCALATE"


@dataclass(frozen=True)
class InternalAuditAssessment:
    decision: InternalAuditDecision
    findings: tuple[str, ...]
    calculation_version: str
    input_hash: str


class InternalAuditEngine:
    """Check traces and separation of duties without trusting narrative output."""

    def evaluate(
        self, *, events: Sequence[Mapping[str, object]], expected_department: str = "qa"
    ) -> InternalAuditAssessment:
        findings: list[str] = []
        normalised: list[dict[str, object]] = []
        for index, event in enumerate(events):
            action = str(event.get("action", "")).strip()
            department = str(event.get("department", "")).strip().lower()
            trace_id = str(event.get("trace_id", "")).strip()
            profile_status = str(event.get("profile_status", "")).strip().upper()
            authorized = event.get("authorized") is True
            if not action:
                findings.append(f"event_{index}:action_missing")
            if not trace_id:
                findings.append(f"event_{index}:trace_id_missing")
            if department and department != expected_department.lower():
                findings.append(f"event_{index}:cross_department_actor")
            if profile_status and profile_status != "ACTIVE":
                findings.append(f"event_{index}:profile_not_active")
            if not authorized:
                findings.append(f"event_{index}:authorization_not_proven")
            if action in FORBIDDEN_CROSS_DOMAIN_ACTIONS:
                findings.append(f"event_{index}:forbidden_cross_domain_action")
            normalised.append(
                {
                    "action": action,
                    "department": department,
                    "trace_id": trace_id,
                    "profile_status": profile_status,
                    "authorized": authorized,
                }
            )
        payload = {
            "events": normalised,
            "expected_department": expected_department,
            "calculation_version": CALCULATION_VERSION,
        }
        input_hash = hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        decision = (
            InternalAuditDecision.PASS
            if not findings
            else InternalAuditDecision.ESCALATE
        )
        return InternalAuditAssessment(
            decision, tuple(findings), CALCULATION_VERSION, input_hash
        )
