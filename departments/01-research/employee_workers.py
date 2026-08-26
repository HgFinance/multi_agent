"""Research employee Worker registry: hypothesis supply, no trade decision.

2026-08-10 전면 재편 (재일). 이전 편제는 종목 애널리스트 6인(universe/microstructure/
technical/fundamental/news-macro/evidence-rag)이었고 산출물은 종목별 Research Packet 이었다.
그 편제는 **프레임워크 자체가 투자판단을 내리는** 구조를 전제한다 - 리서치가 종목 견해를
내면 그것이 곧 매매 근거가 된다.

전략 공장에서 리서치본부는 **가설 공급 조직**이다. 웹(논문·실무자 글·커뮤니티·타 분야)에서
방법론을 수집해 반증 가능한 실험 기획안으로 만들어 퀀트본부에 넘긴다. 종목 방향·확률
예측은 하지 않는다 - 그것은 실험을 통과해 승격된 전략의 몫이다.

▶ 왜 스카우트를 렌즈별로 나누는가
  한 워커에게 "웹에서 방법론을 찾아라"라고 하면 검색이 한 방향으로 쏠린다. 렌즈(학술·
  실무·커뮤니티·타분야)를 나눠 **서로의 결과를 보지 않고** 동시에 뒤지면, 한 관점이
  놓치는 광맥을 다른 관점이 잡는다. 이것이 직원을 두는 이유(병렬성 + 맥락 격리)다.

▶ 회의론자를 분리하는 이유
  경쟁 설명("그냥 베타 아닌가")을 기획안 작성자와 같은 맥락에서 쓰게 하면 반드시
  앵커링된다. 회의론자는 채택 사유를 보지 않고 초안만 본다 - 독립성은 프롬프트가 아니라
  **입력 격리**로만 만들어진다.

▶ 시장을 보는 자리를 둘로 나눈 이유
  ① market-context-worker: 실험 기획은 "이 유니버스가 실재하는가, 데이터가 그만큼
     있는가, 지금 시장 상태가 어떤가"를 알아야 성립한다. 방향 예측이 아니라
     **실행 가능성 판정 재료**다. 공장 쪽 자리다.
  ② holdings-analyst-worker: 사용자가 자기 포트폴리오의 개별 종목을 물어볼 때 답하는
     자리다. **서비스 쪽 자리**이고 공장 흐름에 들어가지 않는다 - 이 산출물은
     기획안의 근거가 되지 않고 주문 경로에도 닿지 않는다. 둘을 한 워커로 합치면
     "사용자에게 설명한 견해"가 "실험의 근거"로 새어 들어간다.

기존 종목 애널리스트 편제는 **운영에서 내린다.** 코드(agents/, scripts.py)는 삭제하지
않지만 런타임 직원 계층에서 빠지고 어떤 공장 흐름에도 연결되지 않는다 - 나중에 실험을
돌려보고 쓸 만하다고 판단되면 그때 하나의 전략 후보로 다시 올리면 되고, 그것은 지금
결정할 일이 아니다. 지금은 공장 하나에만 집중한다.
"""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

try:
    from departments.employee_worker_runtime import (
        WorkerLLM,
        WorkerLLMFactory,
        WorkerSpec,
        StructuredArtifactSpec,
        run_worker_registry,
        tools_for_specs,
    )
except ModuleNotFoundError:
    from employee_worker_runtime import (
        WorkerLLM,
        WorkerLLMFactory,
        WorkerSpec,
        StructuredArtifactSpec,
        run_worker_registry,
        tools_for_specs,
    )

class SkepticReviewV1(BaseModel):
    """Typed independent-review artifact consumed by proposal intake."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    title: str = Field(min_length=1)
    competing_explanation: str = Field(min_length=1)
    competing_codes: tuple[
        Literal["BETA_EXPOSURE", "LIQUIDITY_PREMIUM", "DATA_MINING",
                "COST_UNACCOUNTED"], ...
    ] = Field(min_length=1)
    verdict: Literal["PROCEED", "STOP"]
    falsification_test: str = Field(min_length=1)


def _validate_skeptic_review(value: Any) -> dict[str, Any]:
    return SkepticReviewV1.model_validate(value).model_dump(mode="json")


def _validate_skeptic_reviews_against_input(value: Any,
                                            payload: Mapping[str, Any]) -> list[dict]:
    """Bind one typed review to each source TITLE without ambiguous guessing."""
    draft = payload.get("proposal_draft", "")
    if isinstance(draft, Mapping):
        candidate = draft.get("title") or draft.get("proposal_id")
        titles = [str(candidate).strip()] if candidate else []
    else:
        titles = [match.group(1).strip() for match in re.finditer(
            r"(?m)^\s*TITLE\s*:\s*(.+?)\s*$", str(draft or ""))
            if match.group(1).strip()]
    if not titles:
        raise ValueError("proposal_draft must contain at least one TITLE line")
    reviews = list(value) if isinstance(value, list) else []

    def _merge_single_proposal(items: list[Any]) -> list[dict]:
        """Conservatively fold over-generation into one independent review.

        A small model often emits several alternative attacks on one proposal.
        Dropping all of them wastes a valid skeptic run; selecting one weakens
        it arbitrarily. Unioning codes/tests and letting STOP dominate retains
        every objection without granting any additional authority.
        """
        validated = [_validate_skeptic_review(item) for item in items]
        if not validated:
            raise ValueError("skeptic_reviews must contain exactly 1 item")

        def unique_text(key: str) -> str:
            seen, parts = set(), []
            for item in validated:
                text = str(item[key]).strip()
                if text and text not in seen:
                    seen.add(text)
                    parts.append(text)
            return "; ".join(parts)

        allowed = ("BETA_EXPOSURE", "LIQUIDITY_PREMIUM", "DATA_MINING",
                   "COST_UNACCOUNTED")
        present = {code for item in validated
                   for code in item["competing_codes"]}
        return [{
            "title": titles[0],
            "competing_explanation": unique_text("competing_explanation"),
            "competing_codes": [code for code in allowed if code in present],
            "verdict": ("STOP" if any(item["verdict"] == "STOP"
                                       for item in validated) else "PROCEED"),
            "falsification_test": unique_text("falsification_test"),
        }]

    # Small reviewer models occasionally echo reviews for proposals that were
    # present in retrieved context even though proposal_draft contains only one
    # active block.  Do not let those unrelated, explicitly named artifacts
    # poison the active review.  Exact title binding is safe to recover because
    # the planner title is the join key; ambiguous duplicates and renamed
    # surplus reviews still fail closed below.
    if len(reviews) != len(titles):
        title_set = set(titles)
        exact = [review for review in reviews
                 if isinstance(review, Mapping)
                 and str(review.get("title") or "").strip() in title_set]
        exact_titles = [str(review.get("title") or "").strip()
                        for review in exact]
        if (len(exact) == len(titles)
                and len(set(exact_titles)) == len(titles)
                and set(exact_titles) == title_set):
            reviews = exact
    if len(titles) == 1 and len(reviews) != 1:
        reviews = _merge_single_proposal(reviews)
    if len(reviews) != len(titles):
        raise ValueError(
            f"skeptic_reviews must contain exactly {len(titles)} item(s), one for "
            f"each TITLE line; got {len(reviews)}")
    if len(titles) == 1:
        normalized = [dict(reviews[0])]
        normalized[0]["title"] = titles[0]
        return normalized
    review_titles = [str(review.get("title") or "").strip() for review in reviews]
    if sorted(review_titles) != sorted(titles):
        raise ValueError("skeptic_reviews title set must exactly match TITLE lines")
    return reviews


_SKEPTIC_HIDDEN_FIELDS = frozenset({
    # These are author-provided objections. Passing them to the independent
    # reviewer creates an anchoring path and lets an apparently independent
    # review become a paraphrase of the planner's answer.
    "COMPETING_EXPLANATION",
    "COMPETING_CODES",
    "FALSIFICATION_TESTS",
})


def build_skeptic_view(proposal_draft: str) -> str:
    """Project a proposal for independent review without prior objections.

    The raw proposal remains in the payload for digest binding and audit. Only
    this derived view is exposed through the Worker context tool.
    """

    lines: list[str] = []
    for line in str(proposal_draft or "").replace("\r\n", "\n").split("\n"):
        key, separator, _value = line.partition(":")
        if separator and key.strip().upper() in _SKEPTIC_HIDDEN_FIELDS:
            continue
        lines.append(line)
    return "\n".join(lines).strip()


# ▶ 방법론 스카우트는 여기 없다. 웹 검색·열람·검증 도구가 **본부장(Hermes)에만**
#   있어서 로컬 모델 워커로는 조사 자체가 성립하지 않기 때문이다 - 자리를 남겨두면
#   "조사했는데 아무것도 못 찾았다"가 조용히 정상처럼 보인다. 공장 쪽 조사는
#   본부장이 직접 하고, 필요하면 서브에이전트를 부른다.
#
# ▶ `holdings-analyst-worker` 는 **서비스 쪽 자리**라 남는다(위 ② 참고).
#   빼놨다가 되살렸다 - 이 워커는 포트폴리오 자문 경로(`/ui/portfolio-recommendations`)
#   가 실제로 부르는 유일한 리서치 워커다. 없애면 그 화면의 리서치 구간이
#   "0개 Worker 결과"로 조용히 비고, 사용자는 그게 실패인지 원래 그런 건지 모른다.
#   도구가 `research.*` **읽기 API** 라 본부장 전용 웹 도구와 사정이 다르다 -
#   로컬 모델로도 성립한다(스카우트를 뺀 근거가 이 워커에는 해당하지 않는다).
WORKER_SPECS = (
    WorkerSpec(
        "competing-explanation-worker",
        "Competing explanation and falsification analyst",
        ("research.outcomes.read", "research.evidence.search"),
        "proposal_draft",
        ("skeptic_view",),
        # Keep the shared non-binding envelope; skeptic_reviews is a validated
        # station-specific artifact inside it, not a new authority boundary.
        output_contract="research.worker-context.v1",
        prompt_instructions=(
            "Review every proposal block in the supplied skeptic_view independently. A block begins "
            "ONLY at a line starting `TITLE:`; LEAD_IDS, ECONOMIC_RATIONALE, COUNTERPARTY, "
            "EDGE_TYPE, and UNIVERSE_KEY are fields, "
            "NOT additional proposal titles. Copy the exact text after each TITLE: into "
            "exactly one skeptic_reviews item. Every item MUST contain all five keys in "
            "this exact shape: {\"title\":\"exact title\",\"competing_explanation\":\"strongest "
            "non-alpha explanation\",\"competing_codes\":[\"DATA_MINING\"],\"verdict\":"
            "\"PROCEED\",\"falsification_test\":\"one concrete test as a STRING, not an "
            "array\"}. Choose one or more allowed competing_codes yourself; do not omit "
            "that key. PROCEED means the proposal is testable after recording that "
            "challenge; STOP means it is too vague or invalid to spend a trial. Do not "
            "soften the review to make it pass. Even when you identify several attacks "
            "on one proposal, combine them inside that proposal's single review item; "
            "never emit alternative review items for the same TITLE."
        ),
        structured_artifact=StructuredArtifactSpec(
            key="skeptic_reviews",
            required_strings=("title", "competing_explanation", "verdict",
                              "falsification_test"),
            required_string_lists=("competing_codes",),
            enum_values=(
                ("competing_codes", ("BETA_EXPOSURE", "LIQUIDITY_PREMIUM",
                                     "DATA_MINING", "COST_UNACCOUNTED")),
                ("verdict", ("PROCEED", "STOP")),
            ),
            many=True,
            validator=_validate_skeptic_review,
            context_validator=_validate_skeptic_reviews_against_input,
        ),
    ),
    # portfolio_state.price_levels 에 서버가 계산한 지지·저항·목표·손절이
    # 실린다. 지시가 없으면 모델이 그 값을 무시하고 자기 숫자를 답한다 -
    # 목표가는 근거를 검증할 수 있어야 하므로 인용만 허용한다.
    WorkerSpec("holdings-analyst-worker", "Portfolio holdings question-answering analyst", (
               "research.evidence.search", "research.news.read", "research.market_snapshot.read"),
               "holding_question",
               ("holding_question", "portfolio_state", "news"),
               prompt_instructions=(
                   "가격 수치(목표가·손절가·지지·저항·진입가)는 portfolio_state.price_levels 에 있는 값만 인용한다. 거기 없거나 status 가 OK 가 아니면 계산되지 않았다고 말하고, 직접 계산하거나 추정한 숫자를 답하지 마라. 뉴스·공시는 news.request_time_evidence 의 제목만 근거로 쓴다. 종목이 특정되지 않은 추천 질의라면 portfolio_state.ownership_scan 의 종목을 이름과 수치 그대로 나열하라 - 그것이 답이다. 다만 그것은 지분공시에서 관측된 매집이지 상승 예측이 아니며, 5% 룰이 5영업일 내 보고라 후행 지표임을 함께 밝혀라. 근거가 실려 있는데도 정보가 부족하다고 답하지 마라."
               )),
)


def run_employee_workers(payload: Mapping[str, Any], *, llm: WorkerLLM | None = None,
                         llm_factory: WorkerLLMFactory | None = None) -> dict[str, Any]:
    """llm_factory 가 있으면 워커별로 모델 좌표를 해석한다(부서 LoRA 경로).

    단일 llm 은 워커 정체를 모른 채 공유되므로 Worker Model Gateway 의
    worker_id→adapter 해석이 전달되지 않는다 - MCP 경로는 factory 를 쓴다.
    """
    # stage= 는 HR 유휴 관측 이벤트 이름에 들어간다(2026-08-20). 안 주면 본부장이
    # MCP 로 직접 돌린 실행이 계측에서 빠져 HR 리포트에 IDLE 로 뜬다.
    worker_payload = dict(payload)
    if worker_payload.get("proposal_draft"):
        # Never trust a caller-supplied projection. The raw draft is the
        # auditable source and the projection is derived at the boundary.
        worker_payload["skeptic_view"] = build_skeptic_view(
            str(worker_payload["proposal_draft"])
        )
    return run_worker_registry(WORKER_SPECS, worker_payload,
                               tools=tools_for_specs(WORKER_SPECS),
                               llm=llm, llm_factory=llm_factory, stage="research")
