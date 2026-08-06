#!/usr/bin/env python3
"""P1-1 후보별 OBSERVING Scorecard 계약.

QA는 quality_score와 qa_eval_run_id의 원천 소유자이고, Platform은 비용 원천 소유자다.
이 모듈은 그 값을 새로 판정하지 않고, Promotion 이후 KEPT/ROLLED_BACK 판단에 사용한
비용·품질·안전·회귀 스냅샷을 후보 ID에 append-only로 귀속한다.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CandidateScorecard(BaseModel):
    """`workforce.improvement_candidate_scorecards` 한 행의 앱 계약."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    window_start: datetime
    window_end: datetime
    recorded_by: str = Field(min_length=1)
    input_tokens: int | None = Field(default=None, ge=0)
    output_tokens: int | None = Field(default=None, ge=0)
    total_cost: Decimal | None = Field(default=None, ge=0)
    qa_eval_run_id: str | None = None
    quality_score: Decimal | None = Field(default=None, ge=0, le=1)
    safety_finding_count: int | None = Field(default=None, ge=0)
    regression_count: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _window_is_forward(self) -> CandidateScorecard:
        if self.window_end <= self.window_start:
            raise ValueError("window_end 는 window_start 이후여야 한다")
        return self


if __name__ == "__main__":
    from datetime import timezone

    card = CandidateScorecard(
        candidate_id="ic-1", window_start=datetime(2026, 8, 1, tzinfo=timezone.utc),
        window_end=datetime(2026, 8, 2, tzinfo=timezone.utc), recorded_by="hr-03",
        input_tokens=100, output_tokens=50, total_cost=Decimal("1.25"),
        qa_eval_run_id="eval-1", quality_score=Decimal("0.98"),
        safety_finding_count=0, regression_count=0,
    )
    assert card.total_cost == Decimal("1.25")
    try:
        CandidateScorecard(candidate_id="ic-1", window_start=card.window_end,
                           window_end=card.window_start, recorded_by="hr-03")
        raise AssertionError("역전된 관찰 기간이 통과함")
    except ValueError:
        pass
    print("ok - P1-1 후보별 Scorecard 계약 점검 통과")
