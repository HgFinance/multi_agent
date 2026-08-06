"""트레이딩 직원 Skill/RAG 경계. **판정은 하지 않고 근거만 만든다.**

도메인 판정은 그대로 `contracts/`·`oms/`·`execution/` 이 소유한다. 이 패키지는
직원 LangGraph 가 검증된 근거를 받고, 낸 인용이 검증되는 경계만 담당한다.

  rag_router.py       직원별 RAG 경로 정책 (7명 전원 forced — 입력으로 못 바꾼다)
  trigger_payload.py  조건부 직원 트리거 파생 (승인은 risk_gate 만이 만든다)
  worker_evidence.py  직원별 근거 주입 (Bull/Bear 는 상대 원문을 절대 안 받는다)
  citations.py        네임스페이스 인용 검증 (ls / tca / state / cert / claim)

모듈 이름이 `evidence`·`payload` 가 아닌 이유: 다른 본부에 같은 이름의 디렉터리가
있어(`departments/01-research/evidence`, `06-ai-qa-audit/evidence`) 한 프로세스에서
8개 부서 Worker 를 다 로드하면 flat import 가 엉뚱한 곳을 가리킬 수 있다.
"""

from .citations import KNOWN_PREFIXES, apply_citation_checks, verify_refs
from .rag_router import (
    WORKER_RAG_POLICIES,
    RAGPlan,
    RAGRoute,
    choose_rag_route,
    rag_policy_for_worker,
)
from .trigger_payload import DERIVED_TRIGGERS, enrich_payload
from .worker_evidence import PROVIDERS, grounded_tool

__all__ = [
    "KNOWN_PREFIXES", "apply_citation_checks", "verify_refs",
    "WORKER_RAG_POLICIES", "RAGPlan", "RAGRoute", "choose_rag_route",
    "rag_policy_for_worker",
    "DERIVED_TRIGGERS", "enrich_payload",
    "PROVIDERS", "grounded_tool",
]
