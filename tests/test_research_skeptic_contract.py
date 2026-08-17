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


def test_single_proposal_conservatively_merges_surplus_valid_reviews() -> None:
    reviews = research_workers._validate_skeptic_reviews_against_input(
        [
            _review(title="Invented first proposal"),
            _review(title="Invented second proposal",
                    competing_explanation="The edge may be unaccounted spread cost.",
                    competing_codes=["COST_UNACCOUNTED"], verdict="STOP",
                    falsification_test="Double the spread and fee assumptions."),
        ],
        {"proposal_draft": "TITLE: Deep-book OFI"},
    )

    assert len(reviews) == 1
    assert reviews[0]["title"] == "Deep-book OFI"
    assert reviews[0]["verdict"] == "STOP"
    assert reviews[0]["competing_codes"] == [
        "LIQUIDITY_PREMIUM", "COST_UNACCOUNTED"]
    assert "Neutralize spread" in reviews[0]["falsification_test"]
    assert "Double the spread" in reviews[0]["falsification_test"]


def test_skeptic_review_discards_exactly_identifiable_context_echoes() -> None:
    reviews = research_workers._validate_skeptic_reviews_against_input(
        [
            _review(title="Unrelated retrieved proposal A"),
            _review(title="Deep-book OFI"),
            _review(title="Unrelated retrieved proposal B"),
        ],
        {"proposal_draft": "TITLE: Deep-book OFI"},
    )

    assert len(reviews) == 1
    assert reviews[0]["title"] == "Deep-book OFI"


def test_skeptic_review_merges_duplicate_active_title_conservatively() -> None:
    reviews = research_workers._validate_skeptic_reviews_against_input(
        [
            _review(title="Deep-book OFI"),
            _review(title="Deep-book OFI", verdict="STOP"),
            _review(title="Unrelated retrieved proposal"),
        ],
        {"proposal_draft": "TITLE: Deep-book OFI"},
    )
    assert reviews[0]["verdict"] == "STOP"


def test_multiple_proposals_still_require_exact_one_to_one_binding() -> None:
    with pytest.raises(ValueError, match="exactly 2 item"):
        research_workers._validate_skeptic_reviews_against_input(
            [_review(title="A"), _review(title="A"), _review(title="B")],
            {"proposal_draft": "TITLE: A\nTITLE: B"},
        )
