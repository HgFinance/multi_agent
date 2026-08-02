"""Risk skill registry.  Names mirror the Hermes tool boundary."""

from __future__ import annotations

from .core import SkillSpec

_FORBIDDEN = frozenset({"oms.submit", "ledger.write", "risk.trading_state.write", "risk.trading_state.clear"})

RISK_SKILLS = (
    SkillSpec("risk.pre_trade.check", "deterministic", frozenset({"risk.case.check"}), _FORBIDDEN),
    SkillSpec("risk.p1.snapshot", "deterministic", frozenset({"portfolio-api", "market-api"}), _FORBIDDEN),
    SkillSpec("risk.trading_state.read", "deterministic", frozenset({"risk.trading_state.read"}), _FORBIDDEN),
    SkillSpec("risk.compliance.check", "agentic_rag", frozenset({"risk.compliance.check"}), _FORBIDDEN, True),
    SkillSpec("risk.qa.handoff", "event", frozenset({"risk.decision.publish"}), _FORBIDDEN),
)
