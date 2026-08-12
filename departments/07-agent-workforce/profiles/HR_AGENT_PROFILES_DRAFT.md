# HR 인사팀 Agent Profile 빌딩아웃 (초안)

> 상태: **DRAFT** — Y3 Workforce Registry DB 등록 전 설계 단계. 확정 아님.
> 소유: 영주 (Agent Workforce 인사팀)
> 출처: [AGENT_EMPLOYEE_PROFILES.md](../../../docs/04-organization/AGENT_EMPLOYEE_PROFILES.md) §5(HR-00~04),
>       [hermes/config.yaml](../hermes/config.yaml) personalities, TEAM_YOUNGJU §6.3(Profile 필수 항목)
> 목적: config.yaml 의 prompt-only personality 5명을 `workforce.agent_profiles` +
>       `agent_profile_versions` 로 등록하기 위한 필드를 채운다. F19(자기 개선)가 이 등록된
>       Agent 들을 대상으로 동작한다.

## 1. 모델 풀 (제안)

문서화된 직원 모델 후보는 **Bedrock Claude(주)** 와 **Ollama(로컬·저비용)** 다
(AGENT_EMPLOYEE_PROFILES §1 45행, HR-02 320행). 현 config.yaml 의 `nous/poolside`는
부서 Supervisor 베이스라인이며 직원 모델과 별개 층이다.

> ⚠️ **미결정·ADR 대상**: 전체 Cloud Provider 와 Production 주 모델 확정은 CLAUDE.md 상
> ADR 게이트다. 아래는 문서화된 Bedrock/Ollama 풀 안에서의 **배정 제안**이지 벤더 확정이 아니다.

`workforce.models` 카탈로그(제안):

| model 코드(제안) | provider | 용도 | 배정 기준 |
|---|---|---|---|
| `bedrock-claude-strong` | Bedrock | 판단·설계·Eval 등 추론 중심 역할 | 품질 우선 |
| `ollama-local-economy` | Ollama | 결정론 인접·조율·집계 역할 | 비용 우선 |
| `poolside-laguna` (현행) | nous | 로컬 개발 baseline | 개발/저비용 |

**모델별 판정(한도·집계·SLA 계산)은 결정론적 Service 가 하고, LLM 은 서술·판단에만 쓴다**
(CLAUDE.md 개발 원칙). 그래서 분석·조율 역할은 economy 모델로 충분하다.

## 2. Agent Profile 5명 (P0 우선)

채용 순서: **P0(HR-00·01·04) 먼저 → P1(HR-02·03).**
2026-07-31 기준 **5명 전원 `supabase/seed.sql`에 CANDIDATE/DRAFT 로 등록 완료.** 활성화(→ACTIVE)는
QA Eval + CEO 승인이 남아 있다.

| 필드 | HR-00 인사팀장 | HR-01 Workforce Planner | HR-04 Lifecycle Coordinator |
|---|---|---|---|
| personality (config) | agent-workforce-supervisor | workforce-planning-agent | lifecycle-coordinator |
| 채용 tier | **P0** | **P0** | **P0** |
| Runtime | 독립 Hermes Supervisor | Analytics Worker + Specialist | Deterministic Workflow + Coordinator |
| **모델(제안)** | `bedrock-claude-strong` | `ollama-local-economy` | `ollama-local-economy` |
| Mission | 6개 본부 업무량·품질·비용·Skill Gap 기반 채용/교육/비활성화 결정안 | 본부별 수요·병목 측정 → 역할·동시성·Budget 산정 | 승인된 Agent 최소권한 시작 + 이동·퇴직 시 완전 회수 |
| 필수 Skill | `ORG-01~05`,`HR-01~06`,`OPS-01~02`,`QAA-03~05` | `HR-01~02`,`OPS-01~02`,Queue/SLA,Capacity,SoD | `HR-06`,`QAA-03`,Identity,Least Privilege,J/M/L |
| Tool Allowlist | Requisition/Queue/Eval/Cost/Registry `READ`; 채용·역할변경·비활성화 `PROPOSE` | Case Arrival/Queue/Cost `READ`; Staffing Scenario `PROPOSE` | Job Profile/승인 `READ`; Access/Onboarding Case `PROPOSE` |
| 금지 행위 | 투자판단·주문·Risk승인·자기채용·IAM직접부여·QA우회 | 수익률만으로 증원, Risk/QA 인력 축소 | IAM Admin 직접 사용, 자기를 승인자 지정 |
| KPI | 불필요 Agent 증가율, Skill Gap Aging, 수습 후 성과, Cost/Case, Revocation SLA | SLA 예측오차, 과잉/과소배치율, 비용대비 처리량, Coverage | 승인없는 활성화 0, Provisioning Lead Time, 회수 SLA, Orphan Case 0 |
| memory namespace | `workforce/hr-00` | `workforce/hr-01` | `workforce/hr-04` |
| Owner / Backup | 영주 / (미정) | 영주 / (미정) | 영주 / (미정) |

| 필드 | HR-02 Profile Architect | HR-03 Selection/Performance |
|---|---|---|
| personality (config) | profile-architect | selection-performance-agent |
| 채용 tier | P1 (2차) | P1 (2차) |
| Runtime | Deep Specialist + Profile Builder | Eval Workflow + Training Manager |
| **모델(제안)** | `bedrock-claude-strong` | `bedrock-claude-strong` |
| Mission | 승인된 Skill Gap → Job Profile + 비교 후보 구성 | 후보 선발 + 재직 Agent 반복실패를 교육/개선/역할변경 |
| 필수 Skill | `HR-02~03`,Prompt/Tool Schema,Model Routing,비용추정 | `HR-04~05`,`QAA-04`,Golden/Adversarial Eval,Shadow |
| Tool Allowlist | Requisition/Registry/Model·Tool Catalog `READ`; Candidate Set `PROPOSE` | Candidate/Eval Set/Scorecard `READ`; Selection/PIP `PROPOSE` |
| 금지 행위 | Eval 기준 사후 변경, 편의상 범용 Tool 권한 요청 | **자기 Eval 만으로 Production 승인 금지** (QAA-04 독립 Gate 필수) |
| KPI | Profile 중복률, Schema 완전성, Eval 진입률, 비용오차 | Eval-Production 상관, 수습 실패율, 반복 Finding 감소, False Promotion |
| memory namespace | `workforce/hr-02` | `workforce/hr-03` |
| Owner / Backup | 영주 / (미정) | 영주 / (미정) |

## 3. 스키마 매핑 (Y3 등록 시)

| 위 필드 | 대상 테이블·컬럼 |
|---|---|
| personality(prompt), memory namespace, 모델, Tool Allowlist, 금지행위, Eval | `workforce.agent_profile_versions` (prompt_artifact_path, memory_namespace, model_id, tool_allowlist, forbidden_actions, eval_requirements) |
| Mission, 역할, 필수 Skill, KPI, 금지 | `workforce.role_templates` (mission, required_skills, forbidden_actions, kpi) |
| 모델 카탈로그 | `workforce.models` |
| Skill / Tool 배정 | `workforce.agent_skill_assignments`, `agent_tool_permissions` |
| employee_code, department, role, owner | `workforce.agent_profiles` |

## 4. F19 와의 연결

이 5명이 등록되면 F19 자기 개선의 **대상(target)** 이 된다:
- `selection-performance-agent`(HR-03)가 Scorecard/Eval 저하를 감지 → ImprovementCandidate **author**
- `profile-architect`(HR-02)가 개정안 = 새 `agent_profile_versions` 설계
- HR-03 은 자기 후보를 단독 승인 못 함 → F19 `workflow.py` 의 `SelfApprovalError` 로 강제
- QA(QAA-04) 독립 Gate + CEO 승인 후 배포

## 5. 남은 결정 (등록 전 확정 필요)

1. **모델 벤더 확정** — 위 Bedrock/Ollama 배정을 승인할지 (ADR 대상). 확정 전엔 `poolside-laguna` 로 등록 후 model_id 만 교체 가능.
2. **Backup Owner** — 각 Agent 의 backup_owner_user_id (현재 전부 미정).
3. **employee_code 규칙** — 예: `HR-00`…`HR-04` 그대로 쓸지, 별도 사번 체계.
4. **Eval Set 실체** — Golden/Adversarial Fixture 는 QA(감사본부) 소유 → HR 단독 작성 불가.
