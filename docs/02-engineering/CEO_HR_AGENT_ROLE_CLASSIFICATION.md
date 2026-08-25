# CEO·HR 에이전트 역할 분류

검토일: 2026-08-10 (KST)
작성: 영주 (CEO/HR)

이 문서는 CEO 에이전트와 HR 부서장·직원을 세 가지 기준으로 분류한다.

1. **역할**: 무엇을 하는 주체인가
2. **로그/컨텍스트 위임 여부**: 부서장이 원본 데이터를 직접 읽어야 하는가, 하위 직원 LLM에게 위임해 컨텍스트를 줄이는 게 나은가
3. **형태의 근거**: 각 주체가 왜 에이전트(Hermes)/LLM Worker(LangGraph)/결정론 함수인가

관련 문서: [WORKER_ROLE_BOUNDARIES.md](WORKER_ROLE_BOUNDARIES.md)(전사 Worker 통합 판정), [WORKER_MODEL_MATRIX.md](WORKER_MODEL_MATRIX.md)(모델 배치)

---

## 0. 판단 기준 요약

이 문서에서 반복하는 잣대는 하나다.

> **판정이 비바인딩이고 하류에 검증 게이트가 있으면 LLM(위임 가능) — 판정이 곧 최종 결과이거나 정답이 이미 규칙으로 정해져 있으면 결정론 함수(위임 금지).**

"판정이 곧 최종 결과"라는 말은 함수 안에 임의의 파라미터가 있다는 뜻이 아니다. 함수의 **리턴값(또는 예외)이 완충 단계 없이 그대로 시스템 상태가 된다**는 뜻이다. 자세한 코드 근거는 [4절](#4-부록-산출물이-곧-최종-판정이란-정확히-무엇인가)에 정리한다.

---

## 1. CEO 에이전트

CEO도 HR처럼 **부서장(Hermes) + 직원(LangGraph Worker) 2계층**이다.

### 1-1. CEO 부서장 (`executive-orchestrator`)

**역할**: 6개 투자본부 + HR Shared Service의 산출물을 종합해 사용자에게 하나의 결정과 설명으로 제시한다. Mandate 번역, 예산·SLA 라우팅, 위원회 소집, 에스컬레이션, HR 예산·조직 승인.

**tool_allowlist**: `governance.mandate.read`, `governance.case.create/decide`, `governance.approval.request`, `governance.committee.convene` — 전부 거버넌스 절차를 진행시키는 도구다. 원장·주문·리스크 판정 도구는 `forbidden_tools`에 명시적으로 막혀 있다.

코드: [departments/00-ceo-office/hermes/config.yaml](../../departments/00-ceo-office/hermes/config.yaml)

### 1-2. CEO 직원 (`executive-briefing-worker`, 1명, ALWAYS)

**역할**: `research_packet`·`order_intent`·`risk_decision`·`qa_assessment`·`accounting_snapshot`·`strategy_report` — 5개 부서의 원본 보고서를 읽어 부서장이 소화할 수 있는 `ceo.worker-context.v1`(advisory_context)로 종합한다.

코드: [departments/00-ceo-office/employee_workers.py](../../departments/00-ceo-office/employee_workers.py)

### 1-3. 컨텍스트 위임 판단 — CEO는 왜 위임이 맞나

| 판단 요소 | CEO의 경우 |
|---|---|
| 부서장이 직접 읽으면? | 5개 부서 원본을 매 턴 전부 읽어야 함 — 컨텍스트 부담 큼 |
| 환각이 나면 실제로 위험한가? | 아니다. Worker가 종합한 내용은 비바인딩이다. 실제 주문·리스크 승인·원장 기록은 Worker의 서술이 나오기 **이전에 이미 각 부서의 결정론 게이트**(Risk Engine, Evidence QA Engine, Ledger)를 통과한 뒤다 |
| 안전망이 있는가? | 있다. SOUL.md: *"Every claim you present to the user must trace back to a department's structured output, not your own inference."* Worker가 틀려도 부서장이 원본 구조화 데이터를 대조할 수 있다 |

**결론**: 위임이 맞다. 판정 권한이 없는 종합 작업이고, 실제 위험한 판정은 이미 하류에서 끝난 데이터를 다루기 때문에 환각이 사고로 번질 경로가 없다.

### 1-4. 왜 CEO 부서장은 "자율성 에이전트"인가

판단 잣대는 [2-6](#2-6-왜-부서장은-자율성-에이전트인가)에서 세운 것과 같다 — **무엇을 할지 스스로 선택하는가, 정해진 변환만 하는가, 규칙표를 적용만 하는가.** CEO 부서장도 첫 번째다.

1. **할 일 자체가 매번 다르다.** SOUL.md가 나열한 책임이 7개다 — Mandate 번역, 라우팅·예산 배정, 위원회 소집, 부서 산출물 통합, 에스컬레이션, Chief-of-Staff(Pod 성과 요약·재배분 후보 생성·의제 초안·Drawdown 조사 등 그 자체로 여러 하위 판단), HR 예산·조직 승인. "입력 X → 출력 Y"가 아니라 이번 요청이 이 7개 중 무엇에 해당하는지부터 정하는 게 역할이다.
2. **"무엇을 부를지" 자체가 선택이고, 이미 코드로 구현·실측됐다.** [CEO_CONVERSATIONAL_ROUTING_SPEC.md](CEO_CONVERSATIONAL_ROUTING_SPEC.md)의 `build_ceo_task_plan()`/`ceo_task_planner.py`가 정확히 이 선택을 다룬다 — 사용자가 "삼전 지금 사도 될까"라고 물으면 `research`·`qa`·`ceo` 3곳만, "전략 추천해줘"라면 `research`·`quant`·`risk`·`qa`·`ceo` 5곳을 부른다. 어느 부서를 부를지는 카테고리 기본값 + 자유 질의 해석으로 매번 다시 정해지며, opt-in LLM 라우터(`PORTFOLIO_CEO_TASK_PLANNER_MODE=llm`)를 켜면 이 선택 자체를 CEO Hermes 프로필이 직접 내린다.
3. **선택지가 규칙표로 미리 못 정해진다.** 사용자의 자유 질의는 조합이 무한해 `approve_request()`류의 "허용된 전이 목록"으로 미리 나열할 수 없다. 그래서 결정론 코드는 **최소한만** 강제하고(아래), 세부 선택은 부서장(또는 LLM 라우터)의 판단으로 남긴다.

**이 자율성이 위험하지 않은 이유**는 두 겹으로 막혀 있다.

- **바인딩 권한이 없다.** `forbidden_tools: [oms.submit, ledger.write, accounting.nav.confirm, audit.finding.close, workforce.permission.grant, iam.identity.create]` — 어느 부서를 부르든, 그 결과로 주문을 내거나 원장을 고치거나 NAV를 확정하는 힘 자체가 없다. 무엇을 볼지는 자유지만 무엇을 바꿀지는 자유가 아니다.
- **부서 선택 자체에도 상·하한이 코드로 강제된다.** LLM 라우터를 켜도 `REQUIRED_DEPARTMENTS = frozenset({"qa", "ceo"})`(`ceo_task_planner.py:39`)가 `qa`·`ceo`를 항상 되살리고, allow-list가 정의 밖 부서 호출을 거부한다. "어느 부서를 부를지"라는 자율성과 "그 결과가 최종 판정이 되는 힘"이 분리되어 있다는 점에서 HR 부서장(2-6)과 같은 안전장치 패턴이다.

---

## 2. HR 부서장

### 2-1. 실행 구조 — 5명이 아니라 1개 Head

`config.yaml`에 페르소나 5개(HR-00~04)가 적혀 있지만, 실제로 실행되는 것은 `head_persona: agent-workforce-supervisor` **하나**다. 나머지 4개는 `legacy_personalities_are_aliases: true` — 호환·감사용 이름표일 뿐 별도로 실행되는 에이전트가 아니다.

> CLAUDE.md: "기존 `agent.personalities` 목록은 런타임 직원 수가 아니라 호환·감사 카탈로그다."

### 2-2. 5개 페르소나가 원래 나눠 맡던 책임

| 페르소나 | 책임 | tool_allowlist |
|---|---|---|
| HR-00 `agent-workforce-supervisor` | 6개 본부 Queue·Roster·Skill-Gap·이탈 신호 종합 → 주간 계획 | `roster.read`, `scorecard.read`, `hiring_request.propose` |
| HR-01 `workforce-planning-agent` | Queue 깊이·SLA 위험·비용·Capacity로 채용 우선순위 산정 | `scorecard.read`, `skill_gap.read` (읽기 전용) |
| HR-02 `profile-architect` | Job Profile 설계 — Mission·Skill·금지 권한·Eval 기준 | `profile_version.submit`, `improvement.propose` |
| HR-03 `selection-performance-agent` | Golden/Adversarial Eval 관리, Shadow 검증, 저성과 탐지 | `improvement.propose`, `eval_run.read` |
| HR-04 `lifecycle-coordinator` | 입사/이동/퇴사 이벤트 — Queue·Memory·권한 **요청**만 | `agent_status.change`, `access_request.create` |

코드: [departments/07-agent-workforce/hermes/config.yaml](../../departments/07-agent-workforce/hermes/config.yaml)

### 2-3. HR-00 실제 입력·산출물 — 어디서 오고 어디로 가는가

실제로 실행되는 것은 `agent-workforce-supervisor`(HR-00) 하나다(2-1). 이 페르소나의 `tool_allowlist`([config.yaml:40](../../departments/07-agent-workforce/hermes/config.yaml)) 세 개를 코드로 추적한 결과다.

**입력 3종**

| 도구 | 경로 | 실제로 읽는 것 | 원본 발행 주체 |
|---|---|---|---|
| `workforce.roster.read` | `GET /workforce/v1/roster` → `_roster_repo.list_roster()` | 6개 투자본부 소속 Agent 명부 | HR 자신 — 입사·개정이 완료될 때 HR의 lifecycle 처리가 직접 씀([app.py:686](../../departments/07-agent-workforce/api/app.py)) |
| `workforce.scorecard.read` | `GET /workforce/v1/departments/{code}/scorecard` → `build_department_scorecard()` | Capacity·비용·품질을 결정론 함수가 조립한 집계 | 원본 신호는 6개 투자본부의 Queue·비용 사용량(`cost_snapshots`, 별도 `POST .../cost_snapshots`로 기록). 품질 쪽 `eval_score`는 QA 소유 `audit.eval_runs`를 값으로 복제하지 않고 `eval_run_id`로만 참조한다([cost.py:206](../../departments/07-agent-workforce/scorecard/cost.py)) |
| `workforce.idle_agents.read` (2026-08-10, 2026-08-26 통합) | `GET /workforce/v1/departments/observability` (`idle_agents` 필드) | 6개 투자본부 Worker의 최근 실행 timestamp | 각 투자본부의 Worker 실행 자체 — `portfolio_recommendation.py`가 Worker 실행마다 Langfuse에 이벤트를 발행하고([llm_observability.py](../../orchestration/llm_observability.py)), HR은 timestamp만 조회(원문은 절대 안 읽음) |

**산출물 1종과 실측 공백**

`workforce.hiring_request.propose`가 계약상 해야 할 일: `workforce.hiring_request.v1` 이벤트를 `hf:workforce` Redis Stream에 발행해, [workforce-management.yaml](../../orchestration/workflows/workforce-management.yaml)의 다음 단계(`hr-profile`, `input_contract: hiring_request`)로 넘긴다. 목적지는 `profile-architecture-worker`가 Job Profile 초안을 만드는 진입점이다.

**⚠️ 이 발행 코드가 저장소에 없다.** `workforce.hiring_request.v1`이 등장하는 곳은 [app.py:435](../../departments/07-agent-workforce/api/app.py) `_KNOWN_NON_EVAL_EVENTS` 하나뿐이고, 이건 워크포스 이벤트 컨슈머가 "Eval 이벤트는 아니지만 이름은 안다"고 걸러내는 소비자 측 allowlist다. **발행하는 코드는 어디에도 없다.** 즉 `hiring_request.propose`는 **목적지(다음 계약)만 정의됐고 전송 메커니즘은 미구현**이다 — 부서장이 이 도구를 "쓸 수 있다"는 것과 그 결과가 실제로 다음 단계에 도달하는 것은 다른 문제다.

### 2-4. 이중 방어 — 페르소나 allowlist를 부서 공통 forbidden이 덮어씀

`profile-architect`(HR-02)의 개별 allowlist엔 `workforce.profile_version.submit`이 있는데, 부서 공통 `forbidden_tools`가 같은 도구를 다시 막아놨다.

```yaml
forbidden_tools: [..., workforce.profile_version.submit, workforce.agent_status.change, ...]
```

`tool_gateway.py`의 규칙은 "forbidden이 allowlist보다 우선"이므로, 페르소나 이름이 `submit` 권한을 가진 것처럼 보여도 부서 차원에서 한 번 더 막는다. "제안만 하고 실행은 못 한다"는 원칙을 페르소나별 허용 목록 하나에만 맡기지 않고 부서 레벨에서 이중으로 강제한 것이다.

### 2-5. 컨텍스트 위임 판단 — HR 부서장은 "로그"를 안 읽는다

부서장이 부르는 도구(`scorecard.read`, `roster.read`, `skill_gap.read`)는 이미 결정론 함수가 계산해 놓은 숫자다.

```
Queue 원본 로그
    ↓ (결정론 집계: quality.py aggregate_quality(), cost.py assess_budget())
scorecard.read → {"queue_depth": 12, "sla_breach_rate": 0.03, "quality_score": None, ...}
    ↓ (HR 부서장이 이 JSON을 그대로 읽음)
```

**"로그를 직접 읽을지, 직원 LLM에게 위임해 요약시킬지"라는 선택지 자체가 없다.** 원본 로그가 HR 부서장 근처에 오지 않기 때문이다. 원본 텔레메트리(개별 실행 trace)가 필요해지면 그건 별도의 결정론 Telemetry Pipeline이 집계 계층을 새로 만들 문제지, HR 직원이 파싱할 일이 아니다 — LLM이 "품질이 좋아 보인다"고 뭉뚱그리면 `aggregate_quality()`의 `None`(데이터 없음)과 `0`(진짜 결함)을 하나로 섞어버려 오히려 위험해진다.

**결론**: HR 부서장 계층엔 위임할 "로그"가 없다. 위임 여부를 고민할 필요 자체가 없는 경우다.

### 2-6. 왜 부서장은 "자율성 에이전트"인가

세 형태를 가르는 잣대는 하나다 — **무엇을 할지 스스로 선택하는가, 정해진 변환만 하는가, 규칙표를 적용만 하는가.**

| 형태 | 입출력 | 판단 주체 |
|---|---|---|
| 자율성 에이전트 | 고정 안 됨 — 매 실행마다 "이번엔 뭘 할지"부터 정함 | 에이전트 자신 |
| 고정 입출력 LLM | 정해진 입력 → 정해진 출력 (예: 요청 하나 → 제안서 하나) | 호출자가 이미 정함, LLM은 내용만 채움 |
| 결정론 함수 | 정해진 입력 → 규칙표대로 계산된 출력 | 아무도 없음 — 규칙이 답 |

HR 부서장은 첫 번째다. 근거는 이미 2-2·2-5에서 확인한 사실 셋이다.

1. **할 일 자체가 매번 다르다.** 2-2절 표가 보여주듯 부서장 하나가 원래 5개 페르소나의 서로 다른 책임(주간 계획 종합·채용 우선순위·Profile 설계·Eval 관리·입퇴사 조율)을 흡수했다. "입력 X가 오면 출력 Y를 낸다"는 고정 계약이 없다 — 이번 사이클에 6개 본부 중 어느 신호가 의미 있는지, 채용을 제안할지 말지, 어느 Agent를 저성과로 볼지는 매번 다시 판단해야 하는 문제다.
2. **"무엇을 읽을지"조차 선택이다.** `roster.read`·`scorecard.read`·`idle_agents.read`(2026-08-10 추가) 여러 읽기 도구가 있지만, 이번 사이클에 어느 부서 데이터를 근거로 쓸지 정하는 규칙표가 없다. SOUL.md가 요구하는 것도 정확히 이 선택이다 — *"Flag underperforming or idle existing Agents for retraining or deactivation **as readily as** you propose new hires"*: 유휴 신호와 채용 신호 중 이번엔 뭘 우선할지 정하는 게 역할 자체다.
3. **선택지가 규칙표로 미리 못 정해진다.** 결정론 함수를 못 쓰는 이유가 이것이다 — `approve_request()`(4절)처럼 "허용된 전이 목록"을 미리 나열할 수 있는 문제가 아니라, "이 조합의 신호면 채용을 제안해야 하는가"라는 매번 다른 질문이다. 정답이 코드로 고정되지 않는다.

**그런데 이 자율성이 위험하지 않은 이유**는 2-3(산출물은 제안뿐)·2-5(입력은 결정론 집계뿐)·§4(완충 단계)에서 이미 확인한 구조 때문이다. 부서장이 읽는 값은 전부 결정론 함수가 만든 집계(원본 로그가 아님)이고, 부서장이 낼 수 있는 산출물은 `workforce.hiring_request.propose` — **제안뿐**이다. `forbidden_tools`가 `profile_version.submit`·`agent_status.change`·`permission.grant`를 전부 막아, "무엇을 할지 자유롭게 정하되 그 결정이 시스템 상태를 직접 바꾸는 힘은 없는" 자율성만 허용한다. 자율성(선택권)과 바인딩 권한(실행력)을 분리했기 때문에, 선택을 잘못해도 사고가 아니라 다음 완충 단계(QA 검증·CEO 승인)에서 걸러진다.

---

## 3. HR 직원

### 3-1. 지금 실제로 도는 것: `profile-architecture-worker` (1명)

| 항목 | 내용 |
|---|---|
| 입력 | `hiring_request.read`, `improvement.read`, `profile_version.read`, `tool_catalog.read`, `policy_boundary.read` (전부 읽기) |
| 출력 | `workforce.profile-architecture-context.v1` (Job Profile 초안 + Golden/Adversarial Eval Case 제안) |
| 하는 일 | **창작** — "이 Agent가 거부해야 할 요청이 뭘까"를 새로 설계 |

이 직원의 산출물이 `workforce.profile_version.submit`으로 실제 반영되기 전에 QA가 별도로 검증해야 한다(부서 공통 `forbidden_tools`가 이 워커에게도 submit 자체를 막아놨다). 즉 제안서만 쓰고, 그 제안서가 실행되려면 QA라는 안전망을 반드시 거친다.

코드: [departments/07-agent-workforce/employee_workers.py](../../departments/07-agent-workforce/employee_workers.py)

### 3-2. 결정론 함수로 대체된 4명

| 예전 직원 | 지금 담당 함수 | LLM이면 위험한 이유 |
|---|---|---|
| 선정·성과(workforce-planning류) | `scorecard/quality.py` `aggregate_quality()` | Snapshot 부재(`None`)와 실제 결함(`0`)을 LLM은 뭉뚱그려 서술한다 |
| lifecycle-coordinator | `lifecycle/access.py` `approve_request()`/`provision()`/`revoke()`/`find_expired()` | 함수 호출 자체가 최종 판정. "승인해도 되나요"에 LLM이 틀리면 그게 곧 사고 |
| selection-performance(개정 판정) | `improvements/workflow.py` `transition()` | 작성자 자기승인을 코드가 예외로 차단 |
| 워크포스 거버넌스 | `roster/activation_evidence.py` | 문자열이 비었는지가 아니라 그 ID가 DB에 실재하는지 조회해야 한다 — LLM이 원리적으로 못 하는 검사 |

이 넷의 공통점: **산출물이 그 자체로 최종 판정**이라 하류에 QA 같은 안전망이 없다. 환각이 곧바로 사고가 되므로 애초에 LLM을 두면 안 되는 자리였다.

---

## 4. 부록: "산출물이 곧 최종 판정"이란 정확히 무엇인가

### 4-1. 임의 파라미터가 아니다

`approve_request()`([departments/07-agent-workforce/lifecycle/access.py:154](../../departments/07-agent-workforce/lifecycle/access.py))를 예로 든다.

```python
def approve_request(request, *, approver, approval_id, at):
    if RequestStatus.APPROVED not in ALLOWED_REQUEST_TRANSITIONS.get(request.status, frozenset()):
        raise IllegalTransition(f"{request.status.value} 에서 승인할 수 없다")
    if approver == request.requested_by:
        raise SelfApprovalError("요청자는 자기 권한 요청을 승인할 수 없다")
    if at >= request.expires_at:
        raise IllegalTransition("이미 만료된 요청은 승인할 수 없다")
    return AccessRequest(**{..., "status": RequestStatus.APPROVED, ...})
```

함수 안에 조정 가능한 숫자 임계값 같은 "임의 파라미터"가 있는 게 아니다. **불변식(invariant)이라는 이름의 고정 규칙**이 코드로 박혀 있다.

- 상태 전이표(`ALLOWED_REQUEST_TRANSITIONS`)에 없는 전이 → 무조건 예외
- 요청자==승인자 → 무조건 예외 (자기승인 금지)
- 만료 시각 지남 → 무조건 예외

### 4-2. "최종 판정"이 가리키는 것 — 완충 단계의 유무

이 함수가 리턴한 `AccessRequest(status=APPROVED)` 객체가 **그대로 DB에 저장되는 사실(fact)**이 된다. 그 사이에 "이 결과를 받아들일지 다시 판단하는 계층"이 없다.

반대로 `profile-architecture-worker`의 출력은 advisory다 — QA가 받아서 또 판단해야 한다.

```
LLM Worker 출력 → (QA 검증) → (승인) → 그제서야 상태 반영     [2단계 완충]
결정론 함수 출력 → 바로 DB 상태                              [0단계 완충]
```

완충 단계가 없으니 함수가 틀리면 그 즉시 시스템 상태가 틀린 채로 확정된다. 이것이 "산출물이 곧 최종 판정"이라는 표현이 가리키는 정확한 지점이다 — **임의성의 문제가 아니라 완충 단계의 문제.**

---

## 5. 종합 비교표

| 주체 | 형태 | 판단 근거 |
|---|---|---|
| CEO 부서장 | Hermes Agent (LLM) | 판정 권한이 없는 종합·설명·라우팅 — 실수해도 하류 게이트가 이미 걸렀다 |
| CEO 직원 | LangGraph LLM | 원본이 방대하고(5부서), 결과가 비바인딩이라 위임 위험이 낮다 |
| HR 부서장 | Hermes Agent (LLM) | 제안·종합만, 최종 승인은 CEO 몫. 애초에 읽는 데이터가 이미 집계된 숫자라 위임할 로그가 없음 |
| HR 직원(1명, profile-architecture) | LangGraph LLM | 창작이 필요(정답이 없는 문제) + QA 재검증이라는 완충 단계 존재 |
| HR 직원(4명 → 함수) | 결정론 Python 함수 | 산출물이 곧 최종 판정(완충 단계 0) — 환각이 그대로 사고가 됨 |
