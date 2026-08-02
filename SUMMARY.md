# Risk·QA 요약

검토일: 2026-08-02 (KST)

- Risk P1: API 입력·Instrument 매핑·PIT Exposure·Stress/VaR/Correlation·Kill Switch DB·RLS·진입 차단 게이트 구현.
- QA P1: 정책 ingestion 보호, Model-Risk/Internal-Audit 결정론적 검사 및 API 구현. `qa-check` production 미승인 호출은 `503` 차단.
- 실제 정책 원문과 `SAMPLE_PLACEHOLDER`는 구분하며, placeholder는 운영 적재하지 않음.
- 운영 완료 조건: 실제 API/DB 자격증명, governed FK, ACTIVE Profile, 정책 Corpus/pgvector, 상위 계약 승인, E2E.
- API 키는 수집·저장하지 않음. 추가 Agent 증원은 현재 불필요.

검증: Risk·QA 테스트 통과, Schema contract 12개 통과. 외부 Redis 통합은 DNS 미가용으로 skip.
