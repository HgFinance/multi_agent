# 부서 실행 계층 v2: Hermes 부서장과 LangGraph 직원

상태: 확정 설계 · Risk/QA 1차 적용

이 문서는 전체 투자 파이프라인의 공통 실행 계층을 정의한다.

- 부서장: Hermes Agent가 연결한 상위 LLM(Codex 또는 Claude Code)
- 직원: 역할별 독립 LangGraph Worker Graph + 로컬 Ollama `qwen3:8b` (Risk/QA 현재 고정)
- 결정론적 엔진: Risk Gate, Evidence QA Gate, PIT·인용·권한·상태 전이의 유일한 바인딩 소유자

![0–7번 부서 전체 파이프라인 아키텍처](assets/whole_pipeline_0_7.png)

원본 편집 가능한 다이어그램은 [`whole_pipeline_0_7.svg`](assets/whole_pipeline_0_7.svg)이며, PNG는 [`render_whole_pipeline.py`](assets/render_whole_pipeline.py)로 재생성한다.

## 전체 파이프라인

```text
case_request
  → Research Hermes / LangGraph employees
  → Trading Hermes / LangGraph employees
  → Risk Hermes / LangGraph employees → deterministic Risk Engine
  → QA Hermes / LangGraph employees → deterministic Evidence QA Engine
  → Accounting / Portfolio
  → CEO Hermes → ceo_case_summary
```

각 부서의 직원 결과는 `worker-context.v1` 형태의 비바인딩 context로 부서장에게 전달된다. 부서장은 결과를 종합하고 서술하지만, 주문·원장·한도·감사 종결을 직접 실행하지 않는다. 직원 모델을 바꿀 때는 `ollama list`로 설치 모델을 확인하고 benchmark와 HR·QA 승인을 거쳐 `OLLAMA_CHAT_MODEL` 및 Profile을 함께 변경한다. 자동 모델 교체는 허용하지 않는다.

## 호출 경계

| 계층 | 런타임 | 책임 | 금지 |
|---|---|---|---|
| Department Head | Hermes + Codex/Claude Code | 하위 context 종합, 누락·충돌·에스컬레이션 서술 | 주문 제출, Risk 판정 변경, 원장 수정 |
| Employee Worker | LangGraph + Ollama `qwen3:8b` | 허용된 도구 호출, 근거 요약, 역할별 context 생성 | 바인딩 승인, 임의 API/도구 호출 |
| Deterministic Gate | Python engine/service | Risk/QA 판정, PIT·스키마·권한·상태 검증 | LLM 자유 서술에 의한 판정 변경 |
| HR Registry | Workforce/HR | Worker 활성화·비활성화·교체·성과 검토 | 자기 후보 최종 승인, 권한 우회 |

직원 그래프의 기본 경로는 `tool → worker_llm → schema validation`이다. 최대 재시도는 2회(최대 3 attempts)이며, 실패·스키마 불일치·Ollama 장애는 `DEGRADED`와 HOLD/ESCALATE로 기록한다.

## Risk 직원 구성

부서장 `risk-supervisor`는 Hermes가 담당한다. 중복 역할은 다음 네 Worker로 정리했다.

| Worker | 상태 | 도구 | 입력·출력 경계 |
|---|---|---|---|
| `market-liquidity-worker` | active | `risk.trading_state.read`, `risk.p1.snapshot` | 시장·유동성·노출 context만 생성 |
| `pre-trade-risk-worker` | active | `risk.case.check` | RiskEngine 결과를 설명만 함 |
| `compliance-policy-worker` | conditional | `risk.compliance.check` | 정책 근거가 있을 때만 PIT context 생성 |
| `derivatives-counterparty-worker` | conditional | `risk.trading_state.record.read` | 거래상대방·파생 신호가 있을 때만 실행 |

`RiskEngine.check_order`가 `approve/resize/reject`의 유일한 바인딩 소유자다. Worker 또는 Hermes가 그 값을 덮어쓸 수 없다.

## QA 직원 구성

부서장 `qa-audit-supervisor`는 Hermes가 담당한다. 중복 역할은 다음 다섯 Worker로 정리했다.

| Worker | 상태 | 도구 | 입력·출력 경계 |
|---|---|---|---|
| `evidence-qa-worker` | active | `qa.evidence.check` | EvidenceQaEngine Claim 결과를 설명만 함 |
| `hallucination-critic-worker` | conditional | `qa.evidence.rag` | UNSUPPORTED/CONTRADICTED claim만 검토 |
| `model-and-internal-audit-worker` | conditional | `qa.model_risk.evaluate`, `qa.internal_audit.evaluate` | 모델 재현성·SoD 감사 신호를 함께 검토 |
| `ops-and-permission-worker` | conditional | `qa.ops.evaluate`, `qa.tool_permission.check` | 운영 장애·권한 위반 신호를 함께 검토 |
| `incident-postmortem-worker` | conditional | `qa.incident.record` | 실제 Incident가 있을 때 FACT/INFERENCE context 생성 |

`EvidenceQaEngine.check_artifact`가 PASS/WARN/FAIL의 유일한 바인딩 소유자다. QA Worker 또는 Hermes는 Claim 결과, Finding 상태, Corrective Action 상태를 변경할 수 없다.

## 직원 생명주기와 HR 운영

## Worker count와 execution count

`workers`와 각 부서의 `WORKER_SPECS`가 실행 직원 수의 단일 기준이다. 기존 `agent.personalities` 역할명은 Hermes·DB·감사 호환용 Alias이며 런타임 Worker 수에 포함하지 않는다.

| 부서 | Registry 전체 | 기본 실행 | 조건부 실행 | 케이스 최대 |
|---|---:|---:|---:|---:|
| Risk | 4 | 2 | 2 | 4 |
| QA | 5 | 1 | 4 | 5 |

기본 실행 수는 모든 입력에서 호출되는 Worker 수이고, 조건부 실행 수는 해당 신호가 있을 때만 호출되는 Worker 수다. 이 구분 없이 Registry 전체 수를 “매 실행 호출 수”로 해석하지 않는다.

Profile에 정의된 직원은 자동으로 실행되는 직원이 아니다. `workers.<id>.status`와 trigger를 기준으로 Registry가 호출 대상을 정한다.

- `active`: 기본 입력이 있으면 매 실행 호출
- `conditional`: 명시된 사건·근거·운영 신호가 있을 때만 호출
- `paused`: HR 검토 중 호출하지 않음
- `retired`: 신규 실행에서 제외하고 Replay 이력은 보존
- 새 역할이 필요하면 기존 계약과 Tool Allowlist를 먼저 정의한 뒤 Worker를 추가한다.
- 역할 중복, 낮은 근거 기여도, 높은 지연·비용, 반복적인 schema failure는 HR의 해고·교체 검토 신호다.

HR 변경은 `agent_profile`/`agent_profile_version`과 Worker 상태를 갱신하지만, Risk·QA의 권한 경계를 합치지 않는다. 부서장 Profile과 직원 Worker 모델은 별도 버전으로 기록한다.

## 운영·리플레이 계약

모든 Worker 실행은 부서 실행 매니페스트에 다음을 남긴다.

`worker_id`, `role`, `tools`, `executor`, `provider`, `model`, `output_contract`, `attempts`, `status`, `input_hash`, `error`, `evidence_refs`.

Paper/Replay에서는 Broker 주문, Ledger Posting, 운영 DB 쓰기를 수행하지 않는다. 전체 파이프라인은 한 단계라도 실패하면 자동 승인으로 진행하지 않고 HOLD/REJECT/ESCALATE/ROLLBACK 중 해당 안전 방향으로 종료한다.

구현 기준:

- Risk: `departments/03-risk/risk_employee_workers.py`
- QA: `departments/06-ai-qa-audit/qa_employee_workers.py`
- Profile: 각 부서 `hermes/config.yaml`의 `employee_runtime`와 `workers`
- 부서장 Profile: 각 부서 `hermes/config.yaml`의 `model` 및 `SOUL.md`
