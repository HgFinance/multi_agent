from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest
from pydantic import ValidationError


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "departments" / "01-research" / "employee_workers.py"
SPEC = importlib.util.spec_from_file_location(
    "research_employee_workers_contract_test", MODULE_PATH)
assert SPEC and SPEC.loader
research_workers = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = research_workers
SPEC.loader.exec_module(research_workers)


def _review(**overrides):
    item = {
        "title": "Deep-book OFI",
        "competing_explanation": "The result may be a liquidity premium.",
        "competing_codes": ["LIQUIDITY_PREMIUM"],
        "verdict": "PROCEED",
        "falsification_test": "Neutralize spread and depth buckets.",
    }
    item.update(overrides)
    return item


def test_research_worker_emits_pydantic_validated_skeptic_reviews() -> None:
    def llm(_system: str, _prompt: str) -> str:
        return json.dumps({
            "summary": "Independent review completed.",
            "confidence": 0.8,
            "evidence_refs": ["proposal:draft"],
            "escalate": False,
            "skeptic_reviews": [_review(title="Renamed by the small model")],
        })

    result = research_workers.run_employee_workers(
        {"proposal_draft": "TITLE: Deep-book OFI"}, llm=llm)

    assert result["executed"] == ["competing-explanation-worker"]
    assert result["failed"] == []
    review = result["workers"][0]["output"]["skeptic_reviews"][0]
    assert review["title"] == "Deep-book OFI"
    assert review["competing_codes"] == ["LIQUIDITY_PREMIUM"]


def test_skeptic_review_forbids_unknown_fields_and_codes() -> None:
    with pytest.raises(ValidationError):
        research_workers.SkepticReviewV1.model_validate(
            _review(invented_field="not part of the contract"))
    with pytest.raises(ValidationError):
        research_workers.SkepticReviewV1.model_validate(
            _review(competing_codes=["UNKNOWN_CODE"]))


def test_skeptic_review_rejects_ambiguous_extra_reviews() -> None:
    with pytest.raises(ValueError, match="exactly 1 item"):
        research_workers._validate_skeptic_reviews_against_input(
            [_review(), _review(title="Invented second proposal")],
            {"proposal_draft": "TITLE: Deep-book OFI"},
        )
