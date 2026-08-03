# HgFinance Worker Runtime Summary

검토일: 2026-08-03 (KST)

## 확정 구조

모든 부서는 `Hermes 부서장 → 직원별 독립 LangGraph Worker → 부서장 Context 종합` 구조를 사용한다.

| 부서 | 전체 Worker | 항상 실행 | 조건부 실행 |
|---|---:|---:|---:|
| CEO | 1 | 1 | 0 |
| HR | 5 | 2 | 3 |
| Research | 6 | 2 | 4 |
| Trading | 6 | 2 | 4 |
| Risk | 4 | 2 | 2 |
| Quant / Backtest | 7 | 2 | 5 |
| Accounting / Portfolio | 8 | 2 | 6 |
| QA | 5 | 1 | 4 |

- 부서장: Hermes Agent + `openai-codex/gpt-5.6-luna` 기본. 승인된 Claude Code 경로를 대체 provider로 둔다.
- 직원: 독립 LangGraph Graph + Ollama `qwen3:8b` (현재 전 부서 고정값).
- 공통 출력: 비바인딩 `worker-context.v1`; 입력 해시, Worker, 시도 횟수, 실패·폴백을 남긴다.
- Risk Gate와 Evidence QA Gate는 결정론적 엔진이 소유한다. LLM은 주문·한도·원장·QA 판정을 직접 수행하지 않는다.
- 파이프라인 계약: `case_request → research_packet → order_intent → risk_decision → qa_assessment → execution_result → accounting_snapshot → ceo_case_summary`.
- CEO의 `executive-briefing-worker`는 최종 Case Summary만 작성하며 주문 제출·Risk 승인·원장 수정·NAV 확정 권한이 없다.

## 연동 상태 해석

- Notion Reporter와 Markdown block 변환기는 Projection 어댑터다. `NOTION_TOKEN`·부서 DB ID·API 성공이 확인된 경우에만 실제 업로드로 기록한다.
- LangSmith handoff 필드는 존재하지만 기본 tracing은 비활성이다. 환경변수·자격증명·DNS·네트워크·민감정보 마스킹을 통과한 실제 run만 연결 성공이다.
- PIKE-RAG와 Light-RAG는 전사 적용 완료가 아니라 향후 제한 도입 항목이다. Policy RAG는 Risk, Evidence RAG/감사는 Research·QA가 소유한다.

## 역할 통합 판정

Risk의 파생·Counterparty, QA의 Model Risk·Internal Audit 및 Ops·Permission은 이미 통합했다. Research·Trading·Quant·Accounting·HR의 나머지 Worker는 입력·권한·재현 단위가 달라 추가 병합하지 않는다.

상세 역할 경계와 통합 근거는 [WORKER_ROLE_BOUNDARIES.md](docs/02-engineering/WORKER_ROLE_BOUNDARIES.md), 모델 기준은 [WORKER_MODEL_MATRIX.md](docs/02-engineering/WORKER_MODEL_MATRIX.md), 전체 구조는 [DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md](docs/02-engineering/DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md)를 따른다.
