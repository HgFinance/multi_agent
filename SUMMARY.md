# Risk·QA 요약

검토일: 2026-08-02 (KST)

- Risk P1: API 입력·Instrument 매핑·PIT Exposure·Stress/VaR/Correlation·Kill Switch DB·RLS·진입 차단 게이트 구현.
- QA P1: 정책 ingestion 보호, Model-Risk/Internal-Audit 결정론적 검사 및 API 구현. `qa-check` production 미승인 호출은 `503` 차단.
- Harness: 두 부서에 Skill Registry·trace/비밀값/권한 preflight·bounded retry·fail-closed fallback·Redis health check를 추가.
- AI Office: Risk·QA만 Profile/계약 allowlist 연결, RSK-00~06(6명)·QAA-00~07(8명) 직원/Skill 매핑. 초기 1회+재시도 2회이며 주문·원장 권한은 없음(실시간 API는 별도 실행 필요).
- 로그/Replay/Review: Hermes 실행과 LangGraph 직원 실행을 run_id로 묶어 InputSnapshot·AgentOutput·Validation·Decision을 기록하고 Risk만 Order/Fill을 분리. inputs_hash·버전·retry/fallback·원문/요약을 보존하며 Risk/QA 전용 DB migration을 추가. 기본 Journal은 안전한 인메모리/JSONL 계약이며 운영 DB sink wiring은 별도 배선 조건이다.
- 실제 정책 원문과 `SAMPLE_PLACEHOLDER`는 구분하며, placeholder는 운영 적재하지 않음.
- 운영 완료 조건: 실제 API/DB 자격증명, governed FK, ACTIVE Profile, 정책 Corpus/pgvector, 상위 계약 승인, E2E.
- API 키는 수집·저장하지 않음. 추가 Agent 증원은 현재 불필요.

검증: Risk·QA 테스트 통과, Schema contract 12개 통과. 외부 Redis 통합은 DNS 미가용으로 skip.
