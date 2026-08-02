# Risk·QA 최종 요약

검토일: 2026-08-02 (KST)

## 구현 완료

- Risk: LS증권 시세·잔고 읽기 전용 어댑터와 결정론적 Stress Test·historical VaR·Black–Scholes Greeks 계산기를 추가했다.
- QA: 실제 정책만 허용하는 PIT-aware ingestion 및 pgvector 트랜잭션 적재 경로를 추가했다.
- pgvector: 1024차원 `evidence_chunks`와 검색 RPC의 차원 불일치를 Migration으로 수정했다.
- `qa-check.v1` 계약과 production 승인 게이트를 추가했다. 미승인 production 호출은 `503`으로 차단한다.
- 모든 외부 장애·입력 오류는 Risk `REJECT/HALTED`, QA `FAIL/ESCALATE` 방향으로 처리한다.

## 아직 필요한 외부 조건

- API 키는 수집·저장하지 않았다. `scripts/check_risk_qa_credentials.py`가 키 값 없이 설정 여부만 점검한다.
- 실제 실행에는 LS Paper 자격증명, `DATABASE_URL`, `QA_POLICY_SOURCE_ID`, `OPENAI_API_KEY`, 실제 정책 문서가 필요하다.
- `SAMPLE_PLACEHOLDER` corpus는 Production ingestion에서 거부된다.
- `QA_CHECK_CONTRACT_APPROVED=true`와 상위 서비스 승인, Workforce의 실제 `ACTIVE` profile version이 필요하다.
- Risk 어댑터 결과를 주문 RiskContext에 연결하기 전 Instrument Master 매핑·`as_of`/staleness·RLS·E2E 검증이 필요하다. 주문 전송 권한은 추가하지 않았다.

## 검증

- Risk/QA/schema 전체 테스트 통과; 외부 Redis 통합 8개는 DNS 미연결로 skip.
- 새 코드 Ruff 안전 수정 및 검사 통과.
