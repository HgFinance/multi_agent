# HgFinance Worker 역할·통합 판정

검토일: 2026-08-07 (KST)
상태: **최종 확정** (단, HR 5 -> 0 통합은 2026-08-07 **제안**이며 QA 독립검증·CEO 승인 대기)

이 문서는 직원 수를 늘리거나 줄일 때 사용하는 역할 경계와 통합 판정의 기준이다. 실행 기준은 각 부서의 `hermes/config.yaml`과 `employee_workers.py`의 `WORKER_SPECS`이며, `agent.personalities`의 예전 역할명은 호환용 Alias로만 취급한다.

## 공통 실행 구조

```text
Hermes Department Head (Codex 기본 / 승인된 Claude Code 대체)
  └─ 독립 LangGraph Worker Graph × Worker Registry
       ├─ allow-listed read/calculation tools
       ├─ environment-specific model selected by the runtime contract
       ├─ schema validation + 최대 2회 재시도(총 3회 시도)
       └─ non-binding worker-context.v1 → Hermes context
```

Hermes는 직원 Context를 종합·에스컬레이션한다. 주문 제출, Risk 승인, QA 판정, 원장 Posting, NAV 확정, 감사 Finding 종결은 각각 결정론적 서비스와 독립 통제 부서의 권한이다.

## 확정 Worker Registry

| 부서 | 전체 | 항상 실행 | 조건부 | 현재 통합 판정 |
|---|---:|---:|---:|---|
| CEO | 1 (+결정론 1) | 1 | 0 | **2026-08-11 `ceo-runner` 신설** — `executive-briefing-worker`는 유지, head가 짊어지던 결정론 부기(부서 판정 집계·미완료 단계 조회)만 러너로 내림 |
| HR | 1 | 0 | 1 | `profile-architecture-worker`만 실행; Job Profile/Eval Set 제안 전용 |
| Research | 2 | 0 | 2 | **2026-08-11 축소** — `competing-explanation-worker`(proposal_draft)·`holdings-analyst-worker`(holding_question) 둘만 남고 전부 소집형. 상시였던 market-context 를 폐지해 상시가 0 이 됐다 — 스카우트·회의론자를 상시로 켜두면 편집장이 읽지 못하는 리드만 쌓인다 |
| Trading | 0 (+결정론 1, +임시 전략 Worker) | 0 | 0 | **2026-08-10 Bull/Bear 제거** — 고정 LLM 0명, 전략 Bundle당 임시 결정론 Worker |
| Risk | 1 (+결정론 1) | 0 | 1 | **2026-08-06 tool 강등** — `compliance-policy-worker`만 LLM, 나머지 2명은 `risk-runner`로 통합 |
| Quant / Backtest | 2 | 0 | 2 | **2026-08-10~11 축소** — `result-interpretation-worker`(experiment_card)·`strategy-author-worker`(strategy_authoring) 둘만 남고 전부 소집형. 상시였던 proposal-intake 를 본부장이 흡수했다 — 카드도 없는데 해석 워커를 돌릴 이유가 없다 |
| Accounting / Portfolio | 1 (+결정론 1) | 1 | 0 | **2026-08-07 tool 강등** — `exception-investigation-worker`와 `back-office-runner`가 기존 8개 역할을 흡수·개명 |
| QA | 2 (+결정론 1) | 0 | 2 | **2026-08-06 tool 강등** — Hallucination·Incident만 LLM, 나머지 3명은 `qa-runner`로 통합 |

LLM Worker **10개**(2026-08-10~11 Research 6→2·Quant 7→2 축소 전 19개, 2026-08-10 Trading Bull/Bear 제거 전 21개, 2026-08-06 Trading 강등 전 42개, Risk·QA 강등 전 38개, Accounting 강등 전 32개, HR 통합 전 25개)와 8개 Hermes Profile, 그리고 결정론 Worker 5개(`desk-runner`, `risk-runner`, `qa-runner`, `back-office-runner`, `ceo-runner`)다.

> ⚠ 2026-08-12 정정: 위 표가 오래 Research 6·Quant 7(합 19)로 남아 있었다. 그 축소는
> 코드·부서 `hermes/config.yaml`·`tests/test_worker_architecture.py` 에는 반영됐는데
> **이 문서에만 전파되지 않았고**, `CLAUDE.md` 가 그 19를 그대로 물려받았다.
> 편제 수를 바꿀 때 **표·이 문단·테스트·CLAUDE.md 네 곳을 같이** 고친다.
> 대조 근거는 `test_final_worker_shape_has_no_duplicate_roles`(부서 프로필의
> `workers` 를 읽어 (전체, 상시, 조건부)를 검사한다). Trading의 임시 전략 Worker는 요청 단위로 생겼다 사라지고 모델을 부르지 않으므로 어느 쪽 수에도 넣지 않는다. 조건부 Worker는 Registry에 존재하지만 해당 입력 신호가 없으면 호출하지 않는다.

**표의 "전체"는 LLM Worker 수다.** 결정론 Worker는 모델을 부르지 않으므로 따로 센다 — 섞으면 "Registry에 있다 = 모델을 태운다"가 깨져서 비용·동시성 산정이 흐려진다.

**HR은 결정론 Worker가 0이고 LLM Worker가 1이다.** `profile-architecture-worker`만 비정형 채용·개선 요구를 Job Profile 및 Golden/Adversarial Eval Set 제안으로 바꾼다. 상태 전이·Eval 실행·승인·권한 부여는 모두 독립 서비스의 결정론 경계에 남는다.

## 부서별 역할과 병합 판정

- **CEO**: `executive-briefing-worker` (LLM) + `ceo-runner` (결정론, LLM 없음). 서술 종합은 그대로 Worker가 하고, 러너는 **새 판정을 만들지 않고** Risk/QA 결정론 엔진이 이미 확정한 판정(`risk_decision.verdict`·`expires_at`, `qa_assessment.decision`)을 blocker로 옮기고 안 온 단계를 `missing_inputs`로 적는다. 주문·Risk 승인·원장 수정·NAV 확정 권한은 둘 다 없다.

  2026-08-11 신설. CEO는 앞선 네 부서의 정리를 한 번도 거치지 않은 유일한 부서였고, 그 결과가 `executive-orchestrator` 페르소나 한 문단에 Mandate 해석 + 6본부 라우팅 + 4개 위원회 소집 + Chief-of-Staff 8개 업무가 전부 들어 있던 상태였다. **다른 부서와 달리 LLM 직원을 줄인 것이 아니다**(1명 그대로) — head가 짊어지던 부기 중 결정론인 것만 내렸다. Mandate 해석·라우팅·예산 배분은 같은 입력에 다른 출력이 나오는 것이 산출물이므로 head에 남고, 위원회 소집·정족수·veto는 이미 결정론(`src/committee/`)이라 head는 API 호출만 한다.

  **`missing_inputs`가 이 러너의 핵심이다.** workflow상 CEO 임무인 "각 결과를 통합해 사용자 설명과 **미완료 상태**를 보고"(`investment-case.yaml:84`)에서 뒷부분은 판단이 아니라 조회이며, `back_office_runner()`의 `missing_blocks`("없는 것을 없다고 적는다")와 같은 원칙이다. 다만 **미완료는 escalate 사유가 아니다** — CEO는 4개 workflow에 등장하고 흐름마다 도달한 단계가 다르므로, 안 온 단계를 전부 올리면 그 신호가 곧 의미를 잃는다. `escalate`는 Risk/QA가 실제로 막았을 때만 True다.

  **남은 배관 공백**: `RiskDecision.expires_at`은 계약에 있지만 CEO로 오는 봉투에는 실려 있지 않다(`departments/03-risk/scripts.py`가 만드는 assessment dict에 그 필드가 없다). 러너는 이때 "기한 안"이라고 말하지 않고 `expiry_checked: false`로 적으며, blocker로도 올리지 않는다. 실제 만료 검사를 켜려면 리스크본부가 봉투에 그 필드를 실어야 한다 — CEO Office가 대신 만들지 않는다.
- **HR**: `profile-architecture-worker` — 채용·개선 요구와 경계 증거를 읽어 Job Profile 및 Golden/Adversarial Eval Set을 제안한다. 제안은 항상 non-binding이며, Eval 실행은 QA, 승인과 활성화는 CEO/결정론 게이트, Identity·권한 생성은 Platform/IAM 소유다. 나머지 HR 역할은 결정론 모듈 또는 소비자 부재로 Worker를 두지 않는다.
  - **(A) 결정론 코드가 이미 그 판정을 소유함** — 일은 그대로 일어나고 수행 주체만 바뀐다. 계획·평가·Lifecycle·SoD가 서로 다른 상태 전이라는 2026-08-03 판단 자체는 맞지만, **그 상태 전이를 소유한 것은 Worker LLM이 아니라 결정론 모듈이다.** `selection-performance` → `scorecard/quality.py`의 `aggregate_quality()`(Snapshot이 없으면 0이 아니라 `None`을 돌려 "결함 없음"과 "집계할 데이터 없음"을 구분)와 `cost.py`의 `assess_budget()`, Eval 원본은 QA 소유 `audit.eval_runs`. `lifecycle-coordination` → `lifecycle/access.py`의 `approve_request()`·`provision()`·`revoke()`·`find_expired()`(다섯 개가 전부 거부 규칙이라 프롬프트 부탁이 예외로 바뀐다). `workforce-governance` → `improvements/workflow.py`의 `transition()`과 `roster/activation_evidence.py`(문자열이 비었는지가 아니라 그 ID가 DB에 실재하는지 조회해 판정 — LLM이 원리적으로 못 하는 검사다).
  - **(B) 산출물의 소비자가 없음** — 일 자체가 불필요하다. `workforce-planning`의 인력 상황 서술은 소비자가 없다(Notion 리포트는 `_render_report_md()` 결정론 템플릿, Scorecard는 `build_department_scorecard()`의 구조화 JSON, 대시보드는 그 수치를 그대로 렌더링). 남는 소비자인 Hermes 부서장도 LLM이라 구조화 JSON을 그대로 읽으면 되며, LLM이 LLM에게 주려고 요약하면 정보가 줄기만 한다.
  - 임계값을 스스로 정하고 갱신하는 판단("Queue 10건이 맞는 기준인가")은 Worker가 아니라 Hermes 부서장 몫이며 제안까지만 허용된다(`workforce.hiring_request.propose`). 기준값 자체는 결정론 코드의 상수이고 변경은 사람의 PR이다.
  - **Eval Runner 구현 완료 — 2026-08-10 코드 실측 재확인(2026-08-07 기록을 정정).** `departments/06-ai-qa-audit/eval_runner.py`에 `EvalRunner`/`EvalSet`/`MockToolRegistry`/`ShadowMemory` 전체 구현이 있고, `audit/repository.py`의 `PostgresAuditRepository`가 `audit.eval_runs`/`eval_results`/`eval_sets`에 실제 INSERT·UPDATE를 한다(2026-08-07 시점엔 0건이었던 그 코드). QA API `POST /qa/v1/eval-runs`(`api/app.py`)가 실제로 `EvalRunner(repository=...).evaluate()`를 호출해 COMPLETED/FAILED 행을 기록하고 `GET /qa/v1/eval-runs/{id}`로 조회된다 — `/qa/v1/model-risk/evaluate` 등과는 별개의 진짜 Golden/Adversarial Runner 경로다. **남은 공백**: 후보 Runner 등록(`register_eval_candidate()`)이 QA API 프로세스 내부 함수라, HR 등 다른 부서가 원격으로 새 후보를 등록할 HTTP 경로가 없다 — QA가 그 프로세스 안에서 직접 import해 등록해야 한다.
  - 위 구현으로 HR이 제안한 후보 Agent의 ACTIVE 전이는 `roster/activation_evidence.py`의 실재성 게이트(`qa_eval_run_id`가 가리키는 COMPLETED 행)를 원리적으로 통과할 수 있게 됐다. 다만 QA 쪽에서 해당 후보의 Runner를 등록하고 Eval을 직접 실행해줘야 하는 수동 연동은 남아 있다(위 공백 참고).
  - **복원 근거**: Adversarial Eval Case 작성은 역할마다 새로 써야 하는 창작이라 결정론화 대상이 아니다. `profile-architecture-worker` **1명**이 이 제안만 맡는다. Platform/IAM 쪽은 권한 목록이 카탈로그 선택이라 함수로도 충분하므로 Worker를 둘 근거가 되지 못한다.
  - Worker 실행과 QA Eval Runner 모두 구현됐다. 남은 것은 후보 Runner의 교차 프로세스 등록 경로(위 공백)와 실제 채용 사이클에서의 종단 실행 검증이다.
  - **Platform/IAM 부분 구현 (2026-08-10)** — `platform_iam/`(저장소 최상위) 신규. HR의 `lifecycle/access.py`가 이미 정의해둔 `provisioning_ref` 계약(승인된 `workforce.access_requests`에 실제 Postgres Role/Redis Namespace를 만들고 그 결과를 되돌려주는 것)을 채운다. `resource_kind=DATA`(CREATE ROLE + GRANT)와 `ENVIRONMENT`(Redis Namespace 등록)는 동작 확인됨(pytest 13건: 결정론 로직 9 + HR API 종단 4). `resource_kind=TOOL`은 여전히 막혀 있다 — `workforce.agent_tool_permissions` 조회 엔드포인트가 HR API에 없어 `tool_permission_id`를 얻을 방법이 없고, Platform/IAM은 이를 감지해 조용히 넘기지 않고 `SKIPPED`로 명시한다. `tool_gateway.py`의 `config.yaml` 기반 강제 경로는 건드리지 않는다(단일 출처 원칙 유지).
  - **구현 지침**: Eval Runner의 구현 요구사항은 [EVAL_RUNNER_SPEC.md](EVAL_RUNNER_SPEC.md)(QA/감사본부 소유)에, Platform/IAM의 남은 범위(TOOL 처리, Redis ACL 실제 격리, GRANT 매핑표 채우기)는 [PLATFORM_IAM_SPEC.md](PLATFORM_IAM_SPEC.md)에 정리돼 있다.
- **Research**: `research-data-worker`, `microstructure-worker`, `technical-signal-worker`, `fundamental-valuation-worker`, `news-macro-worker`, `evidence-rag-worker`. 데이터 정본·유동성 증거·지표·가치·이벤트·인용 검증은 서로 다른 입력과 Evidence 책임이므로 유지한다.
- **Risk**: `compliance-policy-worker` (LLM) + `risk-runner` (결정론, LLM 없음). 2026-08-06에 `core-risk-worker`(시장·유동성·사전 Risk Gate)와 `derivatives-counterparty-worker`(파생·Margin·Counterparty)를 **tool로 강등**해 `risk-runner` 하나로 합쳤다 — 둘 다 결정론 Risk Engine이 이미 답을 만들고 있었고 LLM은 그 답을 옮기기만 했다. `compliance-policy-worker`만 LLM이 유지된다(정책 문서 인용·근거 검증은 결정론화가 아니라 Agentic RAG의 몫). 최종 판정은 결정론적 Risk Engine이 한다.
- **Trading**: 고정 LLM 직원 **0명** + `desk-runner` (결정론, LLM 없음) + 요청 단위로 생성되는 임시 전략 Worker. 임시 Worker도 LLM이 아니다 — 퀀트가 넘긴 Strategy Bundle 하나당 결정론 Worker 하나(`employee_workers.create_temporary_worker`)이며, 전략 로직을 실행할 뿐 스스로 선발·승격하거나 Risk를 우회하거나 실주문을 내지 못한다. 비교와 선발은 Worker가 아니라 결정론 오케스트레이션(`scripts.run_alpha_strategy_selection`)이 한다.

  2026-08-06에 `trade-proposal-worker`·`order-constraint-worker`·`execution-planning-worker`·`venue-cost-worker`·`derivatives-structure-worker` 5명을 **tool로 강등**해 `desk-runner` 하나로 합쳤다. 강등 기준 둘: (1) 같은 입력에서 다른 출력이 나오는 것이 산출물인가, (2) 결정론 모듈이 이미 그 답을 만들고 있지 않은가. 다섯 다 둘 다 아니었다 — 주문 제안은 `propose_intent()`→`intent_builder`, 제약 매핑은 `contracts` 전이표, 집행 계획은 `philosophies.yaml` 프리셋 + `check_plan_feasible()`, 비용은 `tca_memory`, Certification은 서명 조회가 이미 답을 낸다.

  **`bull-thesis-worker`/`bear-thesis-worker`는 제거됐다.** 두 직원은 위 기준 (1)을 통과해 남아 있었고, 한 직원이 양쪽 논지를 다 만들면 확증편향이 구조적으로 생긴다는 [ADR-0005](adr/0005-bull-bear-worker-split.md)의 근거도 그대로 유효하다. 없앤 이유는 그 근거가 틀려서가 아니라 **투입이 바뀌었기 때문이다** — 트레이딩본부의 입력이 서술형 Research Packet에서 퀀트 소유 Strategy Bundle로 옮겨가면서, 논지를 세우고 반박할 대상 자체가 본부 밖(퀀트 백테스트)에서 이미 검증된 산출물이 됐다. 대립쌍이 아니라 **여러 전략을 같은 Paper 스트림에서 병렬로 돌리고 결정론 스코어카드로 하나를 고르는 것**이 그 자리를 대신한다. Bull/Bear를 다시 두려면 ADR-0005를 되살리는 것이 아니라 서술형 Packet이 다시 본부 입력이 되는지를 먼저 판단해야 한다.
- **Quant**: `strategy-hypothesis-worker`, `dataset-feature-worker`, `backtest-optimization-worker`, `strategy-release-worker`, `ml-quant-worker`, `execution-cost-worker`, `regime-robustness-worker`. 연구 가설, PIT Dataset, Backtest, Release, ML, 비용, Regime의 실패 원인을 독립적으로 재현해야 하므로 유지한다.
- **Accounting**: `exception-investigation-worker` (LLM) + `back-office-runner` (결정론, LLM 없음). 2026-08-07에 8명을 둘로 줄였다. 근거는 부서 헌장이다 — 마스터플랜 19.12 "공식 숫자는 Accounting Engine이 계산하며 **Agent는 예외 조사와 설명을 담당한다**", 19.16 "Agent가 수치를 계산하거나 수정하지 않는다", SOUL "only figures the Accounting Engine has confirmed".

  기존 8명은 portfolio / treasury / pnl / reporting / valuation / fee-tax라는 **도메인** 축으로 나뉘어 있었는데, 그 이름의 뜻은 "그 도메인의 *수치*를 보는 분석가"다. 수치는 헌장상 처음부터 에이전트 것이 아니므로 **에이전트에게 없는 권한을 축으로 직원을 나눈 것**이었다. 도메인마다 `qwen3:1.7b`를 하나씩 얹으면 각자 자기 도메인 수치를 한국어로 옮기는데, 그게 정확히 회계 수치가 LLM 문장을 거치는 경로다.

  헌장이 쓰는 축은 도메인이 아니라 **예외 종류**이고, 조사가 필요한 예외는 둘 다 "차이가 났는데 원인이 확정되지 않았다"로 같은 모양이다 — 19.11 거래 사실 불일치(Break, `break_triage.py`)와 19.12 항등식 잔차(`DailyReport.unexplained_pnl`, `accounting_ops.yaml pnl_exception`). 조사 방법이 같으므로(원인 후보 → 근거 대조 → 인용 검증 → fail-closed) 직원 하나가 근거 provider 셋을 문다. `nav_close_memory`는 별도 직원이 아니라 이 직원의 근거다.

  **Bull/Bear 같은 대립쌍은 두지 않는다.** Trading이 쪼갠 이유는 "논지가 틀렸다"고 말해 줄 결정론 모듈이 없어서였다([ADR-0005](adr/0005-bull-bear-worker-split.md)). 회계엔 있다 — `check_aging()`이 Break을 조용히 늙지 못하게 하고, `unexplained_pnl`이 잔차를 0으로 반올림하지 않고, `portfolio.py`가 Mark 없으면 NAV를 거부한다. 반대편 압력이 이미 코드이고 LLM 상대역보다 강하다. SOUL이 금지한 "발견한 Break을 스스로 immaterial로 닫는 것"은 상대역이 아니라 **권한을 안 주는 것**으로 막는다. 개명된 `ledger-reconciliation-worker`/`nav-close-worker`/`pnl-attribution-worker`는 config `staff_registry.renamed_workers`에 감사 추적용 alias로 남는다.
- **QA**: `hallucination-critic-worker`, `incident-postmortem-worker` (LLM) + `qa-runner` (결정론, LLM 없음). 2026-08-06에 `evidence-qa-worker`(1차 인용·근거 검사)·`model-and-internal-audit-worker`(Model Risk·Internal Audit)·`ops-and-permission-worker`(운영 건강성·Tool 권한)를 **tool로 강등**해 `qa-runner` 하나로 합쳤다 — 셋 다 결정론 Engine(`EvidenceQaEngine`/`ModelRiskEngine`/`InternalAuditEngine`/`OpsHealthMonitor`/`ToolPermissionCheck`)이 이미 PASS/WARN/FAIL을 정하고 있었고 LLM은 서술만 했다. Hallucination 비판과 Incident Postmortem만 LLM이 유지된다(반박·재구성은 결정론화 대상이 아니다). Evidence QA Gate가 최종 판정을 한다.

### 추가 병합을 승인하지 않은 이유

현재는 **추가 병합 없음**으로 확정한다. 다음 경계는 이름이 비슷해도 합치지 않는다.

| 경계 | 분리 이유 |
|---|---|
| Research microstructure ↔ Trading venue-cost ↔ Risk liquidity | 시장 증거, 주문 비용, Risk 한도라는 서로 다른 소유권 |
| Trading order-constraint ↔ Risk pre-trade | 전자는 비바인딩 제약 매핑, 후자는 바인딩 결정론적 Gate |
| Trading execution-planning ↔ Quant execution-cost | 단일 주문 계획과 역사적 비용·민감도 검증 |
| Research fundamental-valuation ↔ Accounting valuation/corporate-actions | 투자 근거와 공식 평가·기업행동 원장 |
| Accounting reconciliation ↔ QA internal audit | 원장 대사와 독립 통제 감사 |
| HR 승인 라우팅 ↔ QA ops/permission | 조직 승인 라우팅과 독립 권한 검증. HR 쪽은 Worker가 아니라 결정론 모듈이 맡지만 경계 자체는 그대로다 |

향후 통합을 제안하려면 중복 실행률·품질·지연·권한 영향을 Worker별로 측정하고, HR 제안 → QA 독립 검증 → CEO 승인 → Rollback 계획 순서를 거쳐야 한다.

### 결정론 Worker(러너)를 두는 부서와 두지 않는 부서

2026-08-06~07에 다섯 부서가 LLM 직원을 줄였는데 결과 모양이 갈렸다. Trading·Risk·QA·Accounting은 결정론 러너(`desk-runner`/`risk-runner`/`qa-runner`/`back-office-runner`)를 남겼고 HR은 아무것도 남기지 않았다. **어느 쪽이 옳은지가 아니라 부서장이 데이터를 받는 방식이 달라서 갈린 것이므로**, 다음 부서가 감축할 때 앞선 부서를 그대로 따라하지 않는다.

**용어부터 정확히 한다. 러너와 일반 결정론 모듈의 차이는 LLM 유무가 아니다** — 둘 다 LLM 호출이 0이다. 차이는 **계산 결과를 어디로 내보내는가** 하나다.

| | 결정론 러너 | 일반 결정론 모듈 |
|---|---|---|
| LLM 호출 | 없음 | 없음 |
| 호출하는 주체 | `run_employee_workers()`가 조건 없이 1회 | 부서 API 엔드포인트 또는 파이프라인 |
| 결과가 가는 곳 | `worker-context` 봉투(`workers[]`)에 실려 부서장에게 | 반환값 → DB |
| 예 | `departments/02-trading/employee_workers.py`의 `desk_runner()` | `departments/07-agent-workforce/`의 `scorecard/quality.py`, `lifecycle/access.py`, `improvements/workflow.py` |

**즉 러너는 "봉투의 빈자리를 메우는 어댑터"다.** LLM 직원을 지웠는데 부서장이 그 직원 몫을 여전히 봉투로 받고 있으면, 그 자리를 채울 무언가가 필요하다. 부서장이 애초에 봉투로 받지 않았다면 채울 자리도 없다.

#### 판단 기준 두 개 — 둘 다 충족해야 러너를 만든다

1. **부서장의 `input_contract`가 하나로 고정인가?**
   `investment-case.yaml`을 보면 Trading 부서장은 항상 `research_packet`을 받아 `order_intent`를 낸다. 역할이 하나라 봉투 모양이 고정이므로 밀어주기(push)가 성립한다.
   반면 HR 부서장은 `workforce-management.yaml`과 `agent-evolution.yaml`에서 **여섯 단계에 걸쳐 여섯 가지 계약**(`hiring_request` / `job_profile` / `org_approval` / `agent_ops_signal` / `improvement_candidate` / `revision_approval`)을 받는다. 봉투를 하나로 만들면 여섯 경우를 다 담아 비대해지거나 매번 대부분이 비어 있게 된다. 이때는 필요한 시점에 필요한 엔드포인트만 부르는 끌어오기(pull)가 맞다.

2. **러너가 쓸 입력이 dispatch payload 안에 이미 다 있는가?**
   `desk_runner()`가 성립한 것은 `portfolio_state`·`risk_decision`·`execution_constraints`·`venue_cost`·`derivatives`가 전부 하나의 case payload 안에 있었기 때문이다.
   HR의 결정론 모듈은 그렇지 않다 — `approve_request()`는 특정 `AccessRequest` 객체를, `transition()`은 특정 Candidate와 승인 근거를, `aggregate_quality()`는 Quality Snapshot 목록을 요구하는데 셋 다 dispatch payload(`case_request`)에 없다. **입력이 없으므로 러너를 만들어도 계산할 것이 없고, 빈 값을 봉투에 담으면 "직원이 있다"는 착시만 남는다.**

"어느 단계인지 보고 필요한 것만 골라 오라"는 방식은 채택하지 않는다. 그 선택을 LLM에게 시키는 순간 결정론화로 없앤 환각 경로가 다시 열린다.

#### CEO는 왜 러너를 두는가 (2026-08-11)

CEO는 **기준 1을 HR과 같은 방식으로는 통과하지 못한다.** 부서장이 4개 workflow에 등장하고 `input_contract`가 넷이다 — `accounting_snapshot`(investment-case), `strategy_qa_assessment`(strategy-research), `permission_review`(workforce-management), `revision_qa_assessment`(agent-evolution). [CEO_RUNNER_SPEC.md](CEO_RUNNER_SPEC.md) §2가 investment-case 하나만 보고 "역할이 하나라 봉투 모양이 고정"이라고 적은 것은 정확하지 않다.

**그런데도 러너가 성립하는 이유는 기준의 취지가 봉투의 *개수*가 아니라 봉투를 *만들 수 있는가*이기 때문이다.** HR이 막힌 진짜 지점은 기준 2였다 — `approve_request()`는 특정 `AccessRequest` 객체를, `transition()`은 특정 Candidate를 요구하는데 셋 다 dispatch payload에 없어서, 러너를 만들어도 계산할 것이 없었다. CEO는 다르다. 네 흐름 전부 `paper_pipeline._store()`가 **계약 이름을 키로** 산출물을 `context["artifacts"]`에 쌓고, 그 dict가 CEO dispatch payload 안에 그대로 들어온다(investment-case는 `artifacts`, 나머지 셋은 `workflow_context.artifacts`). 이름이 같으므로 러너는 어느 흐름인지 **묻지 않고** 같은 6개 이름을 조회하면 된다.

흐름마다 도달한 단계가 다르다는 사실은 러너를 막는 근거가 아니라 **러너의 산출물 그 자체**다. 안 온 단계는 `missing_inputs`로 나오고, 그것이 workflow가 CEO에게 시킨 "미완료 상태 보고"다. 그래서 미완료는 escalate로 올리지 않는다 — 정상적으로 다른 흐름을 타서 안 온 것과 와야 하는데 안 온 것을 러너가 구분하려 들면, 그 판단이 곧 "어느 단계인지 보고 골라 오라"가 되어 위 문단이 금지한 자리로 돌아간다. 그 구분은 봉투를 받은 부서장이 한다.

#### 러너를 만들 때 반드시 함께 넣는 안전장치

러너는 이름만 Worker이고 LLM이 없다. **그 사실을 프롬프트 문장이 아니라 코드로 강제한다.**

- **`WORKER_SPECS` 레지스트리 밖에 둔다.** 공용 런타임(`run_worker_registry`)은 그래프마다 LLM을 부르므로, 레지스트리에 넣으면 "LLM 없음"이 프롬프트 문장이 되고 실행 경로로는 뚫린다. 대신 `run_employee_workers()` 안에서 직접 호출해 결과를 `workers[]`에 append 한다. **네 부서 모두 이 방식이다**(`desk_runner`/`risk_runner`/`qa_runner`/`back_office_runner` 전부 레지스트리 밖 직접 호출).
- **RAG 정책표를 쓰는 부서라면 러너 항목을 비워 둔다.** 현재 Trading만 해당한다(`trading_worker_skills/rag_router.py`). 라우터는 *LLM이 무엇을 볼 수 있는가*를 제한하는 장치이고 이 Worker에는 LLM이 없다. 항목이 비어 있으면 `rag_policy_for_worker(RUNNER_ID)`가 `ValueError`를 내므로, 나중에 누군가 러너를 LLM 경로에 물리면 조용히 열리는 대신 즉시 죽는다. Trading은 이 fail-closed를 자체 점검 테스트로 고정해 뒀다 — **정책표를 도입하는 부서는 이 장치를 함께 가져간다.**
- **`summary` 같은 서술 필드를 만들지 않는다.** 결정론 모듈의 판정을 그대로 옮기고 `decided_by: deterministic`을 붙인다. 문장을 만들 자리가 없으면 그 자리에서 환각도 생기지 않는다.
- **흡수한 Worker들의 `tool_allowlist` 합집합을 러너에 명시한다.** 감사에서 "이 직원이 무엇을 읽었나"가 남아야 한다.

## 모델과 연동 상태

모델 serving, gateway, adapter resolution, 환경별 모델 값은
[FINAL_RUNTIME_ARCHITECTURE.md](FINAL_RUNTIME_ARCHITECTURE.md)와
[CURRENT_PROJECT_ARCHITECTURE.md](../CURRENT_PROJECT_ARCHITECTURE.md)가 소유한다.
이 문서는 모델 선택 자체가 아니라 Worker 권한, trigger, tool allowlist,
결정론 경계만 정의한다.

- **모델 변경 절차의 권한 경계:** 후보 모델·adapter 변경은 Worker Model
  Matrix의 compatibility index, runtime 문서의 serving contract, QA 회귀,
  CEO/승인 절차를 함께 통과해야 한다. 이 문서는 모델 이름이나 serving
  default를 다시 정의하지 않는다.
- **향후 모델 변경**: 후보 모델·adapter의 Golden/Adversarial 회귀와 QA·승인·rollback 절차는 [FINAL_RUNTIME_ARCHITECTURE.md](FINAL_RUNTIME_ARCHITECTURE.md)의 runtime contract와 [WORKER_MODEL_MATRIX.md](WORKER_MODEL_MATRIX.md)의 compatibility index를 따른다. 이 문서는 변경 절차를 반복해서 정의하지 않는다.
- **Notion**: 부서별 Reporter와 Markdown-to-Notion block 변환기는 어댑터다. 실제 업로드는 `NOTION_TOKEN`과 부서별 DB ID가 설정되고 API 호출이 성공한 경우에만 `upload_succeeded=true`로 본다. Notion은 Projection이며 원본 판정을 소유하지 않는다.
- **LangSmith**: 일부 부서의 handoff 필드는 존재하지만 기본 tracing은 꺼져 있다. 환경변수·자격증명·DNS·네트워크가 모두 확인되고 민감 필드 마스킹을 통과한 실제 run만 trace 성공으로 본다. 코드나 API Key의 존재만으로 연결 완료로 표시하지 않는다.
- **Langfuse (2026-08-10 신규)**: HR(07-agent-workforce)이 6개 투자본부 Worker의 유휴 여부를 관측하는 전용 경로다. LangSmith와 이중 계측이며 부서 코드는 겹치지 않는다 — `orchestration/workflows/portfolio_recommendation.py`의 Worker 실행 지점 한 곳(`publish_langfuse_metric`)이 6개 부서 전부를 자동으로 계측하고, HR은 `departments/07-agent-workforce/scorecard/observability.py`(결정론, LLM 없음)로 timestamp만 조회한다. 자격증명이 없거나 조회가 실패하면 `IDLE`이 아니라 `UNAVAILABLE`로 판정한다 — "쉬고 있다"와 "우리가 모른다"를 구분한다. 근거·제거 기준은 [TECH_STACK_DECISIONS.md](TECH_STACK_DECISIONS.md) 11절.
- **PIKE-RAG / Light-RAG**: 현재 전사 적용 완료가 아니다. Risk의 Policy RAG, Research의 Evidence RAG, QA의 Evidence/Hallucination Audit 범위를 우선 유지하고, PIKE/LightRAG는 corpus·평가셋·운영비용이 준비될 때 제한적으로 도입한다.

## Risk·QA Worker 기술 스택·역할·성과 계약

이 절은 역할 문서와 실행 코드의 드리프트를 막기 위한 명시적 계약이다. 상세 메타데이터의 Source of Truth는 [`departments/risk_qa_worker_profiles.py`](../../departments/risk_qa_worker_profiles.py)이며, 각 부서의 `WORKER_SPECS`가 해당 프로필을 반드시 참조한다. 문서에 적힌 기술은 권한을 확장하지 않는다. Risk·QA Worker의 출력은 `worker-context.v1` advisory이고, 주문·Risk 승인·원장·QA Finding 종결을 직접 수행하지 않는다.

공통 실행 경로는 `allow-listed read/calculation tool → 결정론적 guard/skill → Pydantic context/result 검증 → 필요한 경우 Ollama qwen3:1.7b advisory → trace/replay`다. Risk의 `risk-runner`(옛 `core-risk-worker`)와 Risk Engine 사이에는 RAG·외부 HTTP·재시도형 LLM을 넣지 않는다 — `risk-runner`는 애초에 LLM을 호출하지 않는다. Compliance와 Hallucination만 증거가 필요한 경우 Agentic RAG 경로를 사용하며, PIT·ACL·citation·provenance 검증이 실패하면 `DEGRADED/HOLD/ESCALATE`로 끝낸다.

### Risk 본부

**2026-08-06 tool 강등**: `core-risk-worker`·`derivatives-counterparty-worker`는 `risk-runner`(결정론, LLM 없음, `WORKER_SPECS` Registry 밖에서 매 케이스 항상 실행)로 합쳐졌다. 아래 두 행은 강등 전 설계의 이력 기록이다.

| Worker | 역할과 실행 조건 | 기술 스택과 사용 방식 | 입력·도구 | 성과 지표 |
|---|---|---|---|---|
| `risk-runner` | Market/liquidity/counterparty gate 결과 조회; 항상 실행, LLM 없음 | 평범한 Python 함수(LangGraph 아님). `RiskEngine.check_order()`가 만든 verdict/check_results를 그대로 옮긴다 — `summary` 필드가 없는 것이 이 직원의 요지다. `decided_by: deterministic`, `authoritative: False` | `risk.trading_state.read`, `risk.p1.snapshot`, `risk.case.check`, `risk.trading_state.record.read` | blockers 도출 정확도, Risk Engine verdict와의 일치율 |
| `core-risk-worker` (강등, `risk-runner`로 흡수) | 시장·유동성 상태와 사전 Risk Gate 분석; 항상 실행 (2026-08-06 이전, `market-liquidity-worker`+`pre-trade-risk-worker` 병합) | LangGraph StateGraph, Pydantic `RiskSkillContext/Result`·`OrderIntent/RiskContext`, TradingState·P1 snapshot adapter, Redis read model, `RiskEngine`, idempotency·fail-closed guard, Ollama advisory. PIT/freshness를 먼저 확인하고 HALTED·stale·timeout은 신규 진입 차단 방향으로 요약하며, LLM/RAG 없이 같은 입력에 같은 결과를 만들고 설명만 advisory로 생성 | `risk.trading_state.read`, `risk.p1.snapshot`, `risk.case.check`, 시장/포트폴리오 snapshot | freshness pass rate, stale-block rate, snapshot fan-in latency, replay completeness, deterministic decision consistency, gate latency, invalid-intent rejection rate, fail-closed coverage |
| `compliance-policy-worker` | PIT 정책 근거 분석; compliance evidence가 있을 때만 실행 | LangGraph conditional RAG, PIT/ACL filter, pgvector·BM25 hybrid retrieval, rerank, citation/provenance verifier, Ollama grounded summary. PIKE/LightRAG는 평가 후 도입할 후보 | `risk.compliance.check`, research documents/policies read-only | citation coverage, grounded rate, PIT violation catch rate, escalation precision, RAG latency |
| `derivatives-counterparty-worker` (강등, `risk-runner`로 흡수) | 파생·상대방·증거금 노출 분석; 관련 신호가 있을 때만 실행 (2026-08-06 이전) | LangGraph StateGraph, TradingState record adapter, deterministic exposure/state-uncertainty check, Broker/FCM reconciliation, Ollama advisory. Greeks·margin·counterparty 상태 누락은 승인으로 보간하지 않음 | `risk.trading_state.record.read` | missing-state detection rate, exposure reconciliation coverage, counterparty escalation rate, tool latency |

### AI QA·감사 본부

**2026-08-06 tool 강등**: `evidence-qa-worker`·`model-and-internal-audit-worker`·`ops-and-permission-worker`는 `qa-runner`(결정론, LLM 없음, `WORKER_SPECS` Registry 밖에서 매 케이스 항상 실행)로 합쳐졌다. 아래 세 행은 강등 전 설계의 이력 기록이다.

| Worker | 역할과 실행 조건 | 기술 스택과 사용 방식 | 입력·도구 | 성과 지표 |
|---|---|---|---|---|
| `qa-runner` | Evidence/Model Risk/Internal Audit/Ops/Permission 결과 조회; 항상 실행, LLM 없음 | 평범한 Python 함수(LangGraph 아님). `EvidenceQaEngine`·`ModelRiskEngine`·`InternalAuditEngine`·`OpsHealthMonitor`·`ToolPermissionCheck`가 만든 판정을 그대로 옮긴다 — `summary` 필드가 없다. `decided_by: deterministic`, `authoritative: False` | `qa.evidence.check`, `qa.model_risk.evaluate`, `qa.internal_audit.evaluate`, `qa.ops.evaluate`, `qa.tool_permission.check` | blockers 도출 정확도, 각 결정론 Engine 판정과의 일치율 |
| `evidence-qa-worker` (강등, `qa-runner`로 흡수) | 인용·근거 품질의 1차 검사; 항상 실행 (2026-08-06 이전) | LangGraph `EvidenceQAEngine`, Pydantic QA context/result, PIT·provenance·numeric temporal checker, RunJournal. PASS/WARN/FAIL은 결정론적 엔진이 정하고 LLM은 설명만 생성 | `qa.evidence.check`, claim checks, research packet, evidence refs | citation coverage, claim verification rate, unsupported claim rate, QA latency |
| `hallucination-critic-worker` | unsupported/contradicted claim 비판; 해당 claim이 있을 때만 실행 | LangGraph conditional graph, Agentic RAG retrieval/rerank, contradiction·prompt-injection·provenance guard, Ollama critique. 새 자료를 임의 수집하지 않고 제출된 evidence를 우선 비교하며 미해결이면 ESCALATE | `qa.evidence.rag`, claim checks, source evidence | unsupported detection rate, contradiction detection rate, false-clear rate, critique latency |
| `model-and-internal-audit-worker` (강등, `qa-runner`로 흡수) | 모델 위험·내부통제 점검; audit input이 있을 때만 실행 (2026-08-06 이전) | LangGraph, deterministic `ModelRiskEngine`·`InternalAuditEngine`, SoD/권한 검사, RunJournal append-only trace, Ollama 설명 | `qa.model_risk.evaluate`, `qa.internal_audit.evaluate` | model drift detection, audit finding coverage, SoD violation catch rate, audit latency |
| `ops-and-permission-worker` (강등, `qa-runner`로 흡수) | 운영 건강성·도구 권한 점검; ops/permission input이 있을 때만 실행 (2026-08-06 이전) | LangGraph, `OpsHealthMonitor`, `ToolPermissionCheck`, allowlist·scope·department·SoD·cost/latency 검사, Ollama 설명 | `qa.ops.evaluate`, `qa.tool_permission.check` | permission violation catch rate, unauthorized-call rate, queue/model health, check latency |
| `incident-postmortem-worker` | Incident timeline·재발방지 분석; incident가 있을 때만 실행 | LangGraph, `IncidentTimeline`, `RunJournal`, FACT/INFERENCE 분리, replay metadata, Ollama 요약. incident 기록은 append-only이며 Finding 종결 권한은 없음 | `qa.incident.record`, incident events | timeline completeness, root-cause evidence coverage, recurrence-control coverage, postmortem latency |

조건부 Worker는 자유 텍스트만으로 실행하지 않는다. 해당 구조화 입력과 trigger가 함께 있어야 실행하며, 입력이 없으면 `not_executed/SKIPPED_SAFE`로 남긴다. 이는 성능 저하가 아니라 근거 없는 QA·Risk 판정을 막는 안전장치다. 외부 쓰기(`risk.trading_state.write`, QA corrective-action close 등)는 별도 인증·SoD·idempotency API 경계에 있으며 이 Worker Registry와 포트폴리오 추천 파이프라인에서는 사용하지 않는다.

## 검증 명령

```bash
source ~/claude/bin/activate
python -m pytest tests/test_worker_architecture.py -q -rs
python -m pytest tests -q -rs
```

이 검증은 Registry 수, 독립 Graph topology, Worker 모델, Profile 메타데이터(`role`·`trigger`·`tools`) 일치를 확인한다. 외부 Notion/LangSmith/Redis 연결 성공은 별도 자격증명·네트워크 Smoke 결과로 기록한다.
