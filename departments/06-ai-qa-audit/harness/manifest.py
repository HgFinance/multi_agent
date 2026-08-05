"""QA skill registry.  Names mirror the QA tool boundary."""

from __future__ import annotations

from .core import SkillSpec

_FORBIDDEN = frozenset(
    {
        "oms.submit",
        "ledger.write",
        "risk.limit.write",
        "risk.trading_state.write",
        "risk.trading_state.clear",
    }
)

QA_SKILLS = (
    SkillSpec(
        "qa.evidence.check",
        "deterministic",
        frozenset({"qa.evidence.check", "qa.case.check"}),
        _FORBIDDEN,
    ),
    SkillSpec(
        "qa.evidence.rag",
        "agentic_rag",
        frozenset({"qa.evidence.check"}),
        _FORBIDDEN,
        True,
    ),
    SkillSpec(
        "qa.model_risk.evaluate",
        "deterministic",
        frozenset({"qa.model_risk.evaluate"}),
        _FORBIDDEN,
    ),
    SkillSpec(
        "qa.internal_audit.evaluate",
        "deterministic",
        frozenset({"qa.internal_audit.evaluate"}),
        _FORBIDDEN,
    ),
    SkillSpec(
        "qa.tool_permission.check",
        "deterministic",
        frozenset({"qa.tool_permission.check"}),
        _FORBIDDEN,
    ),
    SkillSpec(
        "qa.tool_permission.unauthorized_count.read",
        "deterministic",
        frozenset({"qa.tool_permission.unauthorized_count.read"}),
        _FORBIDDEN,
    ),
    SkillSpec(
        "qa.ops.evaluate", "deterministic", frozenset({"qa.ops.evaluate"}), _FORBIDDEN
    ),
    SkillSpec("qa.trace.record", "audit", frozenset({"qa.trace.record"}), _FORBIDDEN),
    SkillSpec(
        "qa.incident.record",
        "audit",
        frozenset({"qa.incident.event.write"}),
        _FORBIDDEN,
    ),
)
