"""Research Director: chooses the next information-gaining move."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Mapping

from models import ExperimentPlan, Hypothesis, Objective, stable_id, utc_now


DIMENSION_ALTERNATIVES = {
    "representation": ("event-study", "cross-sectional-rank", "path-trajectory", "latent-regime"),
    "label": ("forward-return", "triple-barrier", "survival", "path-outcome"),
    "sampling": ("time-bar", "event-bar", "volatility-bar", "cross-sectional-snapshot"),
    "horizon": ("short", "medium", "multi-horizon"),
    "regime": ("unconditional", "volatility-conditional", "trend-conditional", "liquidity-conditional"),
    "model": ("rule-based", "ranker", "tree-model", "state-model"),
}


class ResearchDirector:
    """A deterministic director used as a safe fallback for the Hermes agent."""

    def __init__(self, objective: Objective, events: list[Mapping[str, Any]], plans: list[Mapping[str, Any]], results: list[Any]) -> None:
        self.objective = objective
        self.events = events
        self.plans = plans
        self.results = results

    def next_action(self) -> str:
        if not self.plans:
            return "HYPOTHESIZE"
        if len(self.results) < len(self.plans):
            return "AWAIT_RESULT"
        if self.results and self.results[-1].leakage_detected:
            return "PIVOT"
        if self._same_signature_streak() >= 3:
            return "PIVOT"
        # A good result is challenged before it is allowed to become a
        # candidate.  This prevents exploit-only optimization loops.
        if self.results and all(self.results[-1].robustness.values()):
            return "CHALLENGE"
        return "EXPLORE"

    def seed_hypotheses(self, cycle: int) -> tuple[Hypothesis, ...]:
        goal = self.objective.goal
        templates = (
            ("mechanism", "event-conditioned", "event-study", "forward-return", "event-bar", "short", "unconditional", "rule-based", "explore"),
            ("mechanism", "cross-sectional", "cross-sectional-rank", "triple-barrier", "cross-sectional-snapshot", "medium", "trend-conditional", "ranker", "explore"),
            ("mechanism", "regime-conditioned", "latent-regime", "path-outcome", "volatility-bar", "multi-horizon", "volatility-conditional", "state-model", "challenge"),
        )
        hypotheses: list[Hypothesis] = []
        for index, (_, mechanism, representation, label, sampling, horizon, regime, model, role) in enumerate(templates, 1):
            dimensions = {
                "representation": representation,
                "label": label,
                "sampling": sampling,
                "horizon": horizon,
                "regime": regime,
                "model": model,
            }
            statement = f"For the stated objective, {mechanism} information may improve generalisation; test it rather than assuming it."
            hypothesis_id = stable_id("hyp", goal, cycle, index, dimensions)
            hypotheses.append(Hypothesis(
                hypothesis_id=hypothesis_id,
                statement=statement,
                mechanism=f"The market mechanism is represented through {representation}; the counterparty and failure mode must be established from data.",
                expected_behavior=f"The effect should persist under {label} labels and {sampling} sampling after costs.",
                falsifiers=("No conditional effect exists before costs.", "The effect disappears out of sample.", "A small representation change destroys the effect."),
                dimensions=dimensions,
                role=role,
            ))
        return tuple(hypotheses)

    def choose_hypothesis(self, hypotheses: tuple[Hypothesis, ...], action: str) -> Hypothesis:
        if not hypotheses:
            raise ValueError("no hypotheses available")
        if action == "CHALLENGE":
            return next((item for item in hypotheses if item.role == "challenge"), hypotheses[-1])
        if action == "PIVOT":
            signature = self.plans[-1].get("signature", {}) if self.plans else {}
            dimensions = dict(signature)
            for dimension, alternatives in DIMENSION_ALTERNATIVES.items():
                current = dimensions.get(dimension)
                replacement = next((value for value in alternatives if value != current), alternatives[0])
                if current:
                    dimensions[dimension] = replacement
                    break
            base = hypotheses[0]
            return Hypothesis(
                hypothesis_id=stable_id("hyp-pivot", base.hypothesis_id, dimensions),
                statement=f"Pivot representation after repeated low-information experiments: {base.statement}",
                mechanism=base.mechanism,
                expected_behavior=base.expected_behavior,
                falsifiers=base.falsifiers,
                dimensions=dimensions,
                parent_id=base.hypothesis_id,
                role="pivot",
            )
        return hypotheses[0]

    def make_plan(self, hypothesis: Hypothesis, *, cycle: int, action: str) -> ExperimentPlan:
        signature = dict(hypothesis.dimensions)
        plan_id = stable_id("plan", hypothesis.hypothesis_id, cycle, action, signature)
        payload = {
            "schema": "autonomous-experiment-plan.v1",
            "plan_id": plan_id,
            "hypothesis_id": hypothesis.hypothesis_id,
            "objective": self.objective.goal,
            "method": "Observe the phenomenon, run an event study, then validate with independent OOS and adversarial robustness checks.",
            "data_requirements": ["point-in-time market observations", "execution-cost assumptions", "universe membership as known at the observation time"],
            "splits": ["development", "validation", "out-of-sample", "forward-or-paper observation"],
            "cost_model": "explicit slippage, fees and turnover assumptions",
            "seed": 0,
            "signature": signature,
            "action": action,
        }
        from models import canonical_json
        import hashlib
        digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
        return ExperimentPlan(
            plan_id=plan_id,
            hypothesis_id=hypothesis.hypothesis_id,
            objective=self.objective.goal,
            method=payload["method"],
            data_requirements=tuple(payload["data_requirements"]),
            splits=tuple(payload["splits"]),
            cost_model=payload["cost_model"],
            seed=0,
            signature=signature,
            preregistration_hash=digest,
        )

    def intervention(self, action: str) -> str:
        return {
            "PIVOT": "Recent experiments occupy one representation family. Change representation, label, sampling or regime before changing numeric thresholds.",
            "CHALLENGE": "Try to disprove the best candidate with costs, delayed execution, alternate regimes and leave-one-asset/time-block-out tests.",
            "EXPLORE": "Choose the next experiment by the uncertainty it reduces, not by expected Sharpe.",
            "HYPOTHESIZE": "Create competing mechanisms and record their falsifiers before writing strategy code.",
        }.get(action, "Await the missing experiment artifact; do not infer a result.")

    def _same_signature_streak(self) -> int:
        if not self.plans:
            return 0
        target = self.plans[-1].get("signature", {})
        count = 0
        for plan in reversed(self.plans):
            if plan.get("signature", {}) != target:
                break
            count += 1
        return count
