"""사용자 아이디어 -> 공장 리드 후보의 계약.

담당: 재일 (리서치본부 RES)
형판: `orchestration/contracts/user_paper_order.py` - **같은 위험을 이미 한 번
      푼 자리다.** 거기 머리말이 이 모듈의 존재 이유를 그대로 설명한다:
      "언어 모델은 주문 권한이 없다. 엄격한 후보를 제안만 할 수 있다. 이 모듈이
      원문을 독립적으로 다시 읽어 ... 모든 증거 구간을 원문과 정확히 대조한다."

▶ 무엇을 막는가
  사용자가 흘리듯 말한 것("장 마감 직전 호가가 얇아지는 종목이 다음날 밀리던데")
  을 LLM 이 그럴듯한 경제 논리로 부풀려 원장에 넣는 것. 공장의 리드는 실험
  예산을 태우고 다중검정 회계에 잡히므로, **사용자가 실제로 말하지 않은 주장이
  리드가 되면 그 비용을 사용자 이름으로 치르게 된다.**

▶ 어떻게 막는가
  후보의 모든 구조화 주장은 **원문의 정확한 구간을 인용**해야 한다
  (`TextEvidence(field, start, end, text)`). 검증기가 원문을 다시 읽어 구간이
  실제로 그 문자열인지 대조한다. 인용이 없으면 리드가 아니라 **되묻는 응답**이
  나간다 - 이것이 실패가 아니라 정상 동작이다.

▶ 무엇을 막지 않는가
  아이디어가 실행 가능한 수식(AST_READY)이 되는 것은 여기서 판정하지 않는다.
  그건 `lead_intake` 의 결정론 계약이 이미 한다. 이 모듈의 책임은 딱 하나 -
  **"이 경제적 주장이 정말 사용자의 말에서 나왔는가"**.
"""

from __future__ import annotations

import hashlib
import unicodedata
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT_VERSION = "user-idea-lead-interpretation.v1"

# 사용자 원문 상한. 주문 경로(500자)보다 넉넉하다 - 아이디어는 서술이고
# 명령이 아니다. 다만 무한정 받으면 인용 대조 비용이 선형으로 는다.
MAX_IDEA_CHARS = 4000
# 인용 구간 하나의 상한. 이보다 길면 "인용" 이 아니라 원문 복사다.
MAX_EVIDENCE_CHARS = 300


class IdeaEvidenceField(StrEnum):
    """후보의 어느 칸이 이 인용에 기대는가.

    `lead_intake` 의 KEY 이름과 일부러 같게 맞췄다 - 검증을 통과한 값이
    그대로 리드 블록의 그 KEY 로 간다. 이름이 갈리면 옮기는 자리에서
    조용히 틀린다(2026-08-11 `trial_family` 사고와 같은 유형).
    """

    MECHANISM = "MECHANISM"                 # 왜 잔여 수익이 남는가
    COUNTERPARTY = "COUNTERPARTY"           # 누가 반대편에서 손해를 보는가
    MARKET_CONTEXT = "MARKET_CONTEXT"       # 어느 시장·기간·국면
    FAILURE_MODE = "FAILURE_MODE"           # 언제 안 될 것 같은가
    CLAIMED_EDGE = "CLAIMED_EDGE"           # 사용자가 주장한 엣지 한 줄
    OBSERVABLE_HINT = "OBSERVABLE_HINT"     # 어떤 관측치를 보라고 했는가
    HORIZON_HINT = "HORIZON_HINT"           # 어느 시간 지평


class IdeaReasonCode(StrEnum):
    """거부·보류 사유. **구조화 코드로만 답한다** - 산문 사유는 세지 못한다."""

    # 무결성
    INVALID_CANDIDATE_SCHEMA = "INVALID_CANDIDATE_SCHEMA"
    RAW_TEXT_HASH_MISMATCH = "RAW_TEXT_HASH_MISMATCH"
    IDEA_TEXT_TOO_LONG = "IDEA_TEXT_TOO_LONG"
    EVIDENCE_MISSING = "EVIDENCE_MISSING"
    EVIDENCE_SPAN_INVALID = "EVIDENCE_SPAN_INVALID"
    EVIDENCE_TEXT_MISMATCH = "EVIDENCE_TEXT_MISMATCH"
    EVIDENCE_FIELD_MISMATCH = "EVIDENCE_FIELD_MISMATCH"
    EVIDENCE_TOO_LONG = "EVIDENCE_TOO_LONG"
    # 발화 종류 - 아이디어가 아닌 것들
    QUESTION_ONLY = "QUESTION_ONLY"                 # 질문일 뿐 제안이 아니다
    ORDER_INTENT = "ORDER_INTENT"                   # 주문 지시다(다른 경로다)
    OPERATIONAL_REQUEST = "OPERATIONAL_REQUEST"     # 운영 요청이다(공장 아님)
    QUOTED_OR_EXAMPLE = "QUOTED_OR_EXAMPLE"         # 인용·예시를 자기 주장으로 옮겼다
    # 내용 - 리드가 되기엔 모자란 것들
    NO_MECHANISM = "NO_MECHANISM"                   # 왜 남는지가 없다
    NO_FALSIFIABLE_CLAIM = "NO_FALSIFIABLE_CLAIM"   # 반증할 수 있는 문장이 없다
    NO_OBSERVABLE = "NO_OBSERVABLE"                 # 무엇을 재는지가 없다
    PURE_PRICE_PATTERN = "PURE_PRICE_PATTERN"       # 가격 모양만 있고 기전이 없다
    # 기억
    FAMILY_ALREADY_REJECTED = "FAMILY_ALREADY_REJECTED"  # 이미 기각된 계열이다


class TextEvidence(BaseModel):
    """원문의 정확한 한 구간. `text` 는 `raw[start:end]` 와 **글자 그대로** 같아야 한다."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    field: IdeaEvidenceField
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=MAX_EVIDENCE_CHARS)


class UserIdeaCandidate(BaseModel):
    """**LLM 이 제안할 수 있는 것의 전부.**

    `binding: Literal[False]` 는 장식이 아니라 타입 수준의 못이다 - 이 객체는
    어떤 경로로도 "확정된 리드" 로 승격되지 않는다. 승격은 검증기만 한다.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    binding: Literal[False] = False

    # 어떤 원문을 해석했는지 못박는다. 다른 텍스트를 해석한 후보는 거부된다.
    raw_text_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    title: str = Field(min_length=4, max_length=160)
    claimed_edge: str = Field(min_length=4, max_length=300)
    mechanism: str = Field(min_length=20, max_length=1500)
    counterparty: str | None = Field(default=None, max_length=500)
    market_context: str | None = Field(default=None, max_length=500)
    failure_mode: str | None = Field(default=None, max_length=500)
    observable_hint: str | None = Field(default=None, max_length=500)
    horizon_hint: str | None = Field(default=None, max_length=200)

    evidence: tuple[TextEvidence, ...] = ()
    # 에이전트가 스스로 못 하겠다고 신고하는 자리. 비어 있으면 "할 수 있다" 는 주장이다.
    self_reported_gaps: tuple[str, ...] = ()


class VerifiedIdeaLead(BaseModel):
    """검증을 통과한 것. **이것만이** `factory_submit_leads` 블록으로 옮겨진다."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    verified: Literal[True] = True

    title: str
    claimed_edge: str
    mechanism: str
    counterparty: str | None
    market_context: str | None
    failure_mode: str | None
    observable_hint: str | None
    horizon_hint: str | None

    raw_text_sha256: str
    evidence: tuple[TextEvidence, ...]
    # 출처: 사용자의 그 대화가 원문이다. 이 URL 은 인증을 요구하므로
    # `check_link` 에서 401 -> UNVERIFIED 로 남고 리드는 살아남는다(실측).
    source_url: str
    root_task_id: str


class IdeaRejection(BaseModel):
    """거부. **되묻는 문장을 함께 낸다** - 거부만 하면 사용자는 다음 수를 모른다."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[CONTRACT_VERSION] = CONTRACT_VERSION
    verified: Literal[False] = False
    reason_codes: tuple[IdeaReasonCode, ...] = Field(min_length=1)
    needs_from_user: tuple[str, ...] = ()


def idea_text_sha256(raw: str) -> str:
    """원문 해시. **NFC 정규화 후** 해싱한다 - 한글 자모 분리(NFD)로 들어온
    같은 문장이 다른 해시가 되면 후보가 억울하게 거부된다(macOS 입력 실측 유형).
    """
    return hashlib.sha256(
        unicodedata.normalize("NFC", raw).encode("utf-8")).hexdigest()
