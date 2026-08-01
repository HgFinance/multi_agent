begin;

-- 담당자: 재일 (리서치본부, RES-08 RAG 사서)
-- 근거: TEAM_JAEIL 가이드 Sprint J4 모델 선정 결정(2026-08-01) - 임베딩은
--       로컬 무료 모델로 간다. 실측: Ollama 로 구동 가능한 임베딩 모델
--       (bge-m3/mxbai-embed-large 1024, nomic-embed-text 768) 중 1536 차원은
--       없다. 1536 은 OpenAI text-embedding-3-small 전제의 값인데 그 API 를
--       쓰지 않기로 했으므로, 컬럼을 실제 채택 모델(bge-m3, 다국어·한국어
--       강함)의 1024 로 맞춘다.
--
-- 안전성: evidence_chunks 는 이 시점에 0행이다(원문 952건은 아직 청크 전).
-- vector 차원 변경은 USING 캐스팅이 없어 빈 테이블에서만 안전하다 - 행이
-- 생긴 뒤에는 이 방식의 마이그레이션을 쓸 수 없고 재임베딩 절차가 필요하다.
-- HNSW 인덱스는 컬럼 타입 변경 전에 지우고 같은 정의로 다시 만든다.

drop index if exists research.evidence_chunks_embedding_hnsw_idx;

alter table research.evidence_chunks
  alter column embedding type extensions.vector(1024);

create index evidence_chunks_embedding_hnsw_idx
  on research.evidence_chunks using hnsw (embedding extensions.vector_cosine_ops)
  where embedding is not null;

commit;
