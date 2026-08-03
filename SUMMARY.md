# HgFinance Worker Runtime Summary

검토일: 2026-08-03 (KST)

- 전 부서 구조를 `Hermes 부서장 → 직원별 독립 LangGraph Worker → 부서장 context 종합`으로 통일했다.
- 직원 수: CEO 1, HR 5, Research 6, Trading 6, Risk 4, Quant/Backtest 7, Accounting/Portfolio 8, AI QA 5.
- 모든 Worker의 현재 모델은 Ollama `qwen3:8b`; Worker별 경량·표준·중량 모델 변경은 benchmark → HR 제안 → QA 검증 → CEO 승인 후에만 허용한다.
- Risk Gate와 Evidence QA Gate는 결정론적 바인딩 엔진으로 유지한다. Worker와 Hermes는 주문·한도·원장·QA 판정을 직접 수행하지 않는다.
- 공통 출력은 `worker-context.v1`이며, 입력 해시·실행 Worker·실패·재시도·폴백을 기록하고 실패 시 안전 방향으로 격리한다.
- 전체 계약은 `case_request → research_packet → order_intent → risk_decision → qa_assessment → execution_result → accounting_snapshot → ceo_case_summary`다.
- 전체 아키텍처: [DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md](docs/02-engineering/DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md)
- 모델 매트릭스: [WORKER_MODEL_MATRIX.md](docs/02-engineering/WORKER_MODEL_MATRIX.md)
- 실거래·운영 DB·실제 정책 Corpus·외부 API는 별도 승인과 운영 증거가 필요하며, paper 모드는 외부 쓰기를 하지 않는다.
