# ADR-0006: Pinecone 채택 — Risk/QA 정적 규정·정책 코퍼스 Vector Store 분리

- 상태: Accepted
- 날짜: 2026-08-07
- 제안: 동규 (리스크 ↔ AI QA/감사)
- 영향 본부: 리스크, AI QA/감사
- 관련: [TECH_STACK_DECISIONS.md](../TECH_STACK_DECISIONS.md) 3.5, 4,
  [skills/agentic-rag/SKILL.md](../../../skills/agentic-rag/SKILL.md),
  [RAG_LANGGRAPH_STACK.md (리스크)](../../../departments/03-risk/RAG_LANGGRAPH_STACK.md),
  [RAG_LANGGRAPH_STACK.md (QA)](../../../departments/06-ai-qa-audit/RAG_LANGGRAPH_STACK.md),
  [HEDGE_FUND_MASTER_PLAN.md](../../HEDGE_FUND_MASTER_PLAN.md) 13.1

## 배경

Risk의 `compliance-policy-worker`(Agentic RAG baseline, `skills/agentic-rag`)와
QA의 `hallucination-critic-worker`가 참조하는 코퍼스는 규제·컴플라이언스 정책
문서와 Incident 참고자료다. `TECH_STACK_DECISIONS.md` 1.1/4는 Supabase pgvector를
RAG Vector Store로 확정했고, 두 부서의 `RAG_LANGGRAPH_STACK.md`도 "별도 Vector DB는
즉시 추가하지 않는다 — Supabase pgvector가 Source of Truth"라고 못박아뒀다.

그런데 이 코퍼스의 접근 패턴은 같은 Supabase에 있는 Order·Ledger·Position 데이터와
다르다.

- **정적/독립**: 정책·Incident 문서는 거래 이벤트와 무관하게 버전 단위로만 갱신되고,
  특정 Order ID·거래 ID·Ledger Entry와 SQL Join이 필요 없다.
- **조회 패턴**: `as_of` 기준 최신/유효 버전 검색 + Metadata Filter(문서 유형, 관할,
  유효기간)가 전부이며 relational join이 없다.
- **부하 격리**: pgvector 검색은 Supabase Postgres의 같은 IOPS/CPU를 Order/Ledger/Risk
  계산과 공유한다. Compliance·Incident 질의가 늘어나면 트레이딩 hot path의 자원과
  경쟁한다.

## 결정

1. Risk `compliance-policy-worker`와 QA `hallucination-critic-worker`가 참조하는
   **정적 규정·정책 코퍼스**(compliance corpus, incident postmortem 참고자료)는
   Pinecone을 Vector Store로 쓴다. Metadata Filter(`document_type`, `jurisdiction`,
   `effective_from`/`effective_to`, `profile_version`)는 Pinecone 자체 필터로 수행한다.
   같은 Pinecone Index 안에서도 **Namespace를 부서별로 분리한다**
   (`risk-compliance-policy`, `qa-hallucination-reference` 등) — 현재 Baseline
   (`skills/agentic-rag/src/retriever.py`의 `LocalVectorIndex`)이 이미
   `corpus/compliance/`와 `corpus/evidence/`를 완전히 별개 인덱스로 두고 있고,
   두 부서가 서로 다른 코퍼스(정책 vs 증거참고자료)를 다루므로 이 격리를 유지한다.
   Namespace를 공유하면 QA의 Incident 참고자료가 Risk의 Compliance 검색에
   섞여 나올 수 있고, 그 반대도 마찬가지다.
2. **거래·회계 증거처럼 특정 Order/Ledger/Position ID에 Transaction으로 join돼야
   하는 Evidence**는 계속 Supabase pgvector에 남는다 — 예: `research.evidence_chunks`
   중 execution/accounting 근거, Risk Snapshot에 결합되는 증거.
3. `skills/agentic-rag/src/retriever.py`의 `search()` 인터페이스(`DocumentChunk` in,
   `list[ScoredChunk]` out)는 그대로 유지한다. Pinecone은 이 인터페이스 뒤의 새
   Adapter(`PineconeRetriever`)로 구현하고 `nodes.py`는 변경하지 않는다(SKILL.md의
   교체 규칙과 동일).
4. Embedding Model/Version 규율(TECH_STACK_DECISIONS.md 3.4)은 그대로 적용한다 —
   Pinecone Index에도 `embedding_model_id`, `embedding_dimension`, `embedding_version`,
   `index_version`을 Metadata로 저장한다.
5. `audit.rag_runs`/`audit.rag_retrievals` 등 RAG 감사 테이블은 Supabase에 남는다 —
   Pinecone은 Vector Store일 뿐 Audit Trail의 Source of Truth가 아니다. Query hash,
   selected chunk id, score는 지금처럼 Supabase에 기록한다.

## 근거

- 규정·정책 문서는 Order/Ledger/Position과 관계형으로 join될 필요가 없는 참조
  데이터다 — 정규화된 RDB에 넣어야 할 이유가 약하고, pgvector의 강점(같은 트랜잭션
  안에서의 relational join)을 애초에 쓰지 않는다.
- Pinecone의 Metadata Filter + Namespace가 `document_type`/`jurisdiction`/
  `effective_from`/`effective_to` 같은 PIT 후보 축소를 Query 단계에서 바로 처리해,
  Python 후처리 필터링 부담을 줄인다. 다만 최종 PIT/인용 검증은 여전히
  결정론적 Python이 한다 (아래 "지키는 경계" 참고).
- Compliance/Incident 질의량이 늘어나도 Supabase Postgres의 IOPS/CPU를 트레이딩
  hot path(Order, Fill, Risk 계산)와 경쟁하지 않는다 — 격리가 곧 안정성이다.

기각한 대안:

- **대안 1: Supabase pgvector 유지.** 위 부하 격리·Join 불필요 근거가 여전히
  유효하고, 같은 DB Instance 안에서는 Compliance 질의 급증이 트레이딩 경로의 자원과
  계속 경쟁한다.
- **대안 2: 별도 Qdrant 인스턴스.** `RAG_LANGGRAPH_STACK.md`가 이미 "Qdrant/Neo4j
  즉시 추가 안 함" 원칙을 갖고 있고, 셀프호스팅 운영 부담이 Managed Pinecone보다
  크다 — 이 저장소는 아직 별도 인프라 운영 인력이 없다.

## 대가

- **새 Vendor 종속.** Pinecone API Key(현재 `.env`에 `PINECONE_API_KEY`로 존재,
  코드 미연결) 관리, Rate Limit, 비용이 새로 생긴다. Secret은 pgvector Credential과
  동일하게 원문을 Supabase나 코드에 저장하지 않는다.
- **Vector Store가 두 곳으로 나뉜다.** `retriever.py`의 `search()` 뒤에 Adapter가
  두 종류(Supabase pgvector, Pinecone) 생기므로, 어떤 Worker가 어떤 Adapter를
  쓰는지 config로 명시하지 않으면 혼동이 생긴다.
- **감사 일관성 유지 비용.** Vector 자체는 Pinecone에 있지만 Audit Trail
  (`audit.rag_runs` 등)은 Supabase에 남아야 하므로, 두 시스템 간 `chunk_id` 매핑이
  깨지지 않게 관리해야 한다.

## 지키는 경계

이 결정은 어떤 권한도 이전하지 않는다.

- Risk의 결정론적 Risk Engine, QA의 독립 검증 권한은 그대로다 — Vector Store 선택은
  검색 백엔드 교체일 뿐 판정 권한과 무관하다.
- `grounded: false`이면 여전히 inconclusive이며 escalate한다(SKILL.md 원칙 유지) —
  Pinecone 도입이 이 Fail-closed 규칙을 완화하지 않는다.
- Point-in-Time 필터·인용 검증은 여전히 결정론적 Python(`src/nodes.py`)이 한다.
  Pinecone Metadata Filter는 후보를 좁히는 1차 단계일 뿐, 최종 검증을 대체하지 않는다.
- 코퍼스 3개 문서가 `SAMPLE_PLACEHOLDER`인 상태(SKILL.md)는 이 ADR로 바뀌지 않는다 —
  Pinecone으로 옮겨도 placeholder 코퍼스는 여전히 실제 정책 근거로 신뢰하지 않는다.

## 영향 파일

- `docs/02-engineering/TECH_STACK_DECISIONS.md` — 1.1 Supabase 행 각주, 3.5(신규)
  Vector Store 분리, 4 "Supabase에 저장" 각주
- `departments/03-risk/RAG_LANGGRAPH_STACK.md` — 5절 저장소 설명에 Pinecone 예외 추가
- `departments/06-ai-qa-audit/RAG_LANGGRAPH_STACK.md` — 8절 저장소 설명에 Pinecone
  예외 추가
- (다음 단계, 이 ADR의 구현) `skills/agentic-rag/src/retriever.py` —
  `PineconeRetriever` Adapter 추가
- (다음 단계) `.env` — `PINECONE_API_KEY`를 실제 코드에 연결, Production/Test Index
  분리(`RISK_QA_PRODUCTION_ENABLED` 관례 준용)
