# Worker 모델 배치 기준

이 문서는 8개 부서의 독립 LangGraph Worker에 적용할 모델 선택 기준이다.

현재 운영 기본값은 모든 Worker에 `qwen3:8b`다. `light`·`standard`·`heavy`는 현재 모델을 바꾸라는 설정이 아니라, 향후 `ollama list` 결과와 Worker별 평가를 바탕으로 교체할 때 사용하는 승인된 분류다.

## 선택 원칙

| Tier | 적합한 업무 | 변경 후보 입력 | 현재 fallback |
|---|---|---|---|
| `light` | 라우팅, 검색, 포맷 검증, 단순 상태 분류 | `OLLAMA_LIGHT_MODEL` | `qwen3:8b` |
| `standard` | 도메인 분석, 근거 요약, 조건부 검토 | `OLLAMA_CHAT_MODEL` | `qwen3:8b` |
| `heavy` | 충돌 조정, 다중 근거 합성, 복합 시나리오 검토 | `OLLAMA_HEAVY_MODEL` | `qwen3:8b` |

모델 변경은 `ollama list` 확인 → Worker Golden/Adversarial 평가 → 지연·비용·검증 실패율 비교 → HR 변경 제안 → QA 독립 검증 → CEO 승인 순서다. 자동 교체와 실패 시 상위 모델 무제한 재시도는 허용하지 않는다.

## Worker Registry

| 부서 | Worker 수 | `light` | `standard` | `heavy` |
|---|---:|---|---|---|
| CEO Office | 1 | — | `executive-briefing-worker` | — |
| Agent Workforce | 5 | `workforce-planning-worker`, `lifecycle-coordination-worker` | `profile-architecture-worker`, `workforce-governance-worker` | `selection-performance-worker` |
| Research | 6 | `research-data-worker`, `evidence-rag-worker` | `microstructure-worker`, `technical-signal-worker`, `news-macro-worker` | `fundamental-valuation-worker` |
| Trading | 6 | `order-constraint-worker`, `venue-cost-worker` | `market-thesis-worker`, `execution-planning-worker`, `derivatives-structure-worker` | `trade-proposal-worker` |
| Risk | 4 | `market-liquidity-worker` | `pre-trade-risk-worker`, `derivatives-counterparty-worker` | `compliance-policy-worker` |
| Quant / Backtest | 7 | `dataset-feature-worker`, `execution-cost-worker` | `strategy-hypothesis-worker`, `backtest-optimization-worker`, `regime-robustness-worker` | `strategy-release-worker`, `ml-quant-worker` |
| Accounting / Portfolio | 8 | `ledger-reconciliation-worker`, `fee-accrual-tax-worker` | `portfolio-control-worker`, `treasury-liquidity-worker`, `pnl-attribution-worker`, `valuation-corporate-actions-worker` | `nav-close-worker`, `investor-reporting-worker` |
| AI QA / Audit | 5 | `ops-and-permission-worker`, `incident-postmortem-worker` | `evidence-qa-worker`, `hallucination-critic-worker` | `model-and-internal-audit-worker` |

## 실행 계층

```text
Hermes Department Head (Codex or Claude Code)
  └─ independent LangGraph Worker Graph × Registry count
       ├─ allow-listed deterministic/read-only tools
       ├─ Ollama LLM (temporary active model: qwen3:8b)
       ├─ schema validation + max 3 attempts
       └─ non-binding worker-context.v1 → Hermes context
```

Risk Gate, Evidence QA Gate, OMS, Ledger, IAM, HR 승인과 CEO 권한은 Worker가 소유하지 않는다. Worker가 실패하면 해당 부서의 계약된 HOLD/REJECT/ESCALATE 방향으로만 전달한다.
