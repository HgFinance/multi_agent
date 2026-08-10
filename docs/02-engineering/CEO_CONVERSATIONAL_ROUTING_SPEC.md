# CEO 대화형 라우팅 명세

검토일: 2026-08-10 (KST)
작성: 영주 (CEO Office)
상태: **설계 초안** — §3 이후는 미구현. §2는 구현·문서화 완료분 정리

> 상위 기준: [HEDGE_FUND_MASTER_PLAN.md](../HEDGE_FUND_MASTER_PLAN.md)
> 관련: [USER_INPUT_SPEC.md](../01-product/USER_INPUT_SPEC.md)(온보딩 1회), [MAS_PIPELINE_CONTRACTS.md](MAS_PIPELINE_CONTRACTS.md)(부서 연결 계약), [UNIFIED_DOMAIN_API_SPEC.md](UNIFIED_DOMAIN_API_SPEC.md) 10.5(자유 질의 라우팅)

---

## 1. 이 문서가 다루는 제품 구조

사용자는 **최초 1회 Mandate를 입력**하고, 이후에는 등록된 Mandate와 현재 포트폴리오를 기반으로 **비서 챗봇과 상시 소통**한다. 사용자 입력의 성격에 따라 호출해야 할 본부가 달라지므로, **CEO 에이전트(Hermes 통합장)가 의도를 해석해 필요한 본부만 호출**한다.

```
[최초 1회]  온보딩 → Mandate 확정            ← USER_INPUT_SPEC.md 담당
                    ↓
[상시]      사용자 질문 → CEO 의도 라우팅 → 필요한 본부만 → 답변
                                              ↓
                                   (전략 생성 시) 포트폴리오 편입 승인
```

이 문서는 **[상시] 구간**을 다룬다. 온보딩 구간은 [USER_INPUT_SPEC.md](../01-product/USER_INPUT_SPEC.md)가 소유한다.

### 1.1 사용자 입력 유형과 필요한 본부

| 사용자 입력 예시 | 성격 | 필요한 본부 | 결과물 |
|---|---|---|---|
| "이 주식 지금 사도 될까?" | 단건 조회 | research → qa → ceo | 리서치 결과 설명 (advisory) |
| "요즘 반도체 어때?" | 시장 조사 | research → qa → ceo | 시장 분석 설명 |
| "내 포트폴리오 위험한가?" | 리스크 점검 | research → risk → qa → ceo | 리스크 평가 설명 |
| "세금 얼마나 나와?" | 세무·유동성 | research → risk → accounting → qa → ceo | 회계 분석 설명 |
| "리밸런싱 해줘" | 전체 검토 | research → trading → risk → accounting → qa → ceo | 리밸런싱 제안 (advisory) |
| **"이런 전략 어때?"** | **전략 검증** | **quant-backtest → qa → ceo** | **백테스트 결과 + 편입 제안** |
| **"전략 추천해줘"** | **전략 발굴** | **quant-backtest → qa → ceo** | **전략 후보 + 편입 제안** |

**굵게 표시한 두 줄이 현재 라우터에 연결되어 있지 않다** (§3).

---

## 2. 현재 구현된 것 — 결정론적 의도 라우터

### 2.1 구현 위치

[orchestration/workflows/portfolio_recommendation.py:130](../../orchestration/workflows/portfolio_recommendation.py) `build_ceo_task_plan()`

### 2.2 라우팅 방식 — 3단계

**1단계: 카테고리 → Workflow 소속 판정** (2026-08-10 추가)

카테고리는 먼저 **어느 흐름이 이 요청을 소유하는가**를 정한다. 부서 선택보다 이것이 앞이다.

```python
CATEGORY_WORKFLOWS = {
    "PORTFOLIO_RECOMMENDATION": "portfolio-recommendation",
    "MARKET_RESEARCH":          "portfolio-recommendation",
    "RISK_REVIEW":              "portfolio-recommendation",
    "TAX_LIQUIDITY":            "portfolio-recommendation",
    "REBALANCING_PROPOSAL":     "portfolio-recommendation",
    "STRATEGY_PROPOSAL":        "strategy-research",   # 이 그래프가 처리 주체가 아님
}
```

이 계층을 따로 둔 이유는 [3.1](#31-전략-요청은-부서-추가가-아니라-workflow-선택-문제다)에 적는다 — 요약하면 **전략 요청을 quant 부서 추가로 풀면 안 되기 때문**이다.

**2단계: 카테고리 → 부서 집합 (구조화 입력)**

```python
CATEGORY_DEPARTMENTS = {
    "PORTFOLIO_RECOMMENDATION": ("research", "risk", "qa", "ceo"),
    "MARKET_RESEARCH":          ("research", "qa", "ceo"),
    "RISK_REVIEW":              ("research", "risk", "qa", "ceo"),
    "TAX_LIQUIDITY":            ("research", "risk", "accounting", "qa", "ceo"),
    "REBALANCING_PROPOSAL":     ("research", "trading", "risk", "accounting", "qa", "ceo"),
    "STRATEGY_PROPOSAL":        ("research", "qa", "ceo"),   # 자문 전용 축소 집합
}
```

**3단계: 자유 질의 키워드 (부서 집합 확장만)**

```python
keywords = {
    "trading":    ("주문", "매수", "매도", "체결", "리밸런싱", "거래"),
    "accounting": ("세금", "수수료", "원장", "nav", "현금", "대사", "회계"),
    "research":   ("종목", "주식", "etf", "뉴스", "시장", "업종", ...),
    "risk":       ("위험", "리스크", "손실", "변동", "헤지", "레버리지", ...),
    "qa":         ("검증", "근거", "신뢰", "감사", "오류", "출처"),
}
```

카테고리가 정한 기본 집합에 키워드로 부서를 **추가만** 한다. 빼지 않는다 — 안전 방향(더 많은 검증)으로만 움직인다.

#### 2.2.1 키워드 층은 실측상 거의 작동하지 않는다 (2026-08-10 측정)

`build_ceo_task_plan()`을 직접 호출해 확인한 결과, **현실적인 질의에서 키워드 매칭이 0건**이다.

| 질의 | `matched_terms` |
|---|---|
| `"삼성전자 어때?"` | `{}` |
| `"모멘텀 전략 백테스트 해줘"` | `{}` |

사용자는 "종목", "리스크" 같은 사전 등재어로 말하지 않는다. **실제 라우팅은 거의 전부 카테고리 기본값이 수행하고 있으며, 키워드 층은 사실상 비어 있다.**

무해한 이유는 두 가지다 — 기본 집합이 이미 넉넉하고(4~6개 부서), 키워드는 추가만 하기 때문이다. 그러나 **"키워드가 사용자 의도를 파악한다"는 설명은 사실이 아니므로** 이 층의 정확도를 전제로 다른 설계를 쌓지 않는다.

개선 방향은 [3.5](#35-자연어-의도-분류는-표-앞에-두되-표-대신-두지-않는다)를 따른다.

### 2.3 이 라우터가 LLM이 아닌 이유

`build_ceo_task_plan()`의 docstring이 근거를 적어놨다.

> *"This deterministic first pass is a safety guard: it limits which department may be called, while the CEO worker can explain and refine the plan. It never creates orders or changes financial state."*

**"어느 부서를 호출할 수 있는가"는 권한 경계 문제**다. LLM이 이 결정을 하면 프롬프트 조작으로 부서 호출 범위가 바뀔 수 있다. 그래서 **기본 경로에서는** 호출 가능 부서 집합을 결정론 코드가 정하고, LLM(CEO 부서장)은 그 결과를 설명하고 다듬는 역할만 한다.

> 2026-08-10부터 CEO 프로필이 부서를 직접 고르는 opt-in 경로가 있다([3.5](#35-자연어-의도-분류는-표-앞에-두되-표-대신-두지-않는다)). 그 경로도 이 원칙을 버리지 않는다 — 상한(allow-list)과 하한(`qa`·`ceo` 필수)을 코드가 강제하고, 어떤 실패든 결정론으로 되돌아간다. 기본값은 여전히 결정론이다.

### 2.4 감사 추적

응답의 `task_plan`에 다음이 기록된다.

| 필드 | 내용 |
|---|---|
| `original_query` | 사용자 원문 (보존) |
| `rewritten_query` | 부서 전달용 정규화 문장 |
| `requested_departments` | 실제 호출한 부서 목록 |
| `matched_terms` | 어느 키워드가 어느 부서를 켰는지 |
| `routing_basis` | `category_default` / `bounded_query_intent_router` 등 |
| `workflow` | 이 요청을 소유하는 흐름. `portfolio-recommendation`이 아니면 이 그래프가 정식 처리 주체가 아니라는 뜻 (2026-08-10 추가) |
| `category_recognized` | 카테고리가 알려진 값이었는지. `false`면 더 넓은 부서 집합으로 fallback 했다는 표시 (2026-08-10 추가) |

호출되지 않은 부서는 `SKIPPED_SAFE`로 기록된다 — 침묵하지 않는다.

이 계약은 [apps/api/portfolio_schemas.py](../../apps/api/portfolio_schemas.py) `PortfolioTaskPlan`이 `extra="forbid"`로 강제한다. 라우터가 새 필드를 내보내려면 스키마도 함께 고쳐야 하며, 그러지 않으면 BFF 경계에서 검증 실패한다 — 조용히 흘려보내지 않는다.

### 2.5 알 수 없는 카테고리는 거절하지 않고 넓게 떨어진다

API의 `category`는 `Literal`이 아니라 `str`이다. 표에 없는 값이 와도 **422가 아니라 fallback**한다.

| 입력 | 결과 |
|---|---|
| 알 수 없는 카테고리 + 질의 없음 | 6개 부서 **전부** 호출, `category_recognized: false` |
| 알 수 없는 카테고리 + 질의 있음 | 기본 4개(research·risk·qa·ceo), `category_recognized: false` |

**`Literal`로 좁히지 않은 이유**: 대화형 제품에서 새 의도는 표보다 먼저 도착한다. 표에 없다고 422를 던지면 사용자 질문이 통째로 실패하지만, fallback은 부서를 더 부를 뿐이고 이 그래프에는 주문·원장 권한이 없어 확대의 비용이 지연·비용에 그친다.

**대신 조용히 넘기지 않는다** — `category_recognized: false`가 응답에 남아 "왜 6개 부서를 다 불렀나"를 설명할 수 있다. 이 값이 자주 `false`로 관측되면 그것이 표에 카테고리를 추가하라는 신호다.

---

## 3. 미구현 — 설계가 필요한 4가지

### 3.1 전략 요청은 "부서 추가"가 아니라 "Workflow 선택" 문제다

> **검토 중 정정된 항목.** 이 절의 초안은 해결책으로 *"`DEPARTMENTS` 튜플에 `quant-backtest`를 추가"*만 제시했는데, 그것만으로는 아키텍처 제약(아래 인용)을 만족하지 못한다. 부서 추가가 아니라 **Workflow 선택 계층**이 먼저라는 결론으로 대체했다.

**현황**: `portfolio_recommendation.py`의 `DEPARTMENTS`는 `(research, trading, risk, qa, accounting, ceo)` 6개이고 quant가 없다. "이런 전략 어때?"가 백테스트 본부로 갈 경로가 없다.

**주의해야 할 경계**: 두 문서가 제약을 건다.

> MAS_PIPELINE_CONTRACTS.md: *"Quant와 HR은 포트폴리오 추천 그래프에 **암묵적으로 끼워 넣지 않고** 각 선언된 Workflow에서 별도로 검증한다."*
> CLAUDE.md: *"5개 흐름 — 서로 분리, 섞지 않는다."*

금지 대상은 **암묵적 삽입**이다. [3.6](#36-요청-시점-quant-호출--2026-08-10-팀-합의로-해결)의 팀 합의에 따라 quant를 **선언된 단계**로 넣은 것은 이 제약을 위반하지 않는다 — 다만 그것으로 전략 요청이 해결되는 것은 아니다. 전략의 **승격**(Champion 교체·Production 반영)은 여전히 `strategy-research`가 소유하며, 그 경계는 워크플로 선택 계층이 지킨다.

**필요한 구조**: 부서 선택 **위에** 워크플로 선택 계층을 둔다.

```
사용자 질의
    ↓
[1] CATEGORY_WORKFLOWS  — 어느 흐름이 소유하는가?
    ├─ portfolio-recommendation → research/trading/risk/qa/accounting/ceo
    └─ strategy-research        → quant-backtest → qa → ceo   (별도 그래프)
    ↓
[2] CATEGORY_DEPARTMENTS — 그 흐름 안에서 어느 부서를 부를까?
```

**구현된 것** (2026-08-10):

- `CATEGORY_WORKFLOWS` 신설 — 카테고리 → 워크플로 매핑
- `STRATEGY_PROPOSAL` 카테고리 추가 → `strategy-research` 소속으로 선언
- `task_plan.workflow`로 호출부에 소속 흐름을 전달
- quant-backtest를 `DEPARTMENTS`·`_MODULE_PATHS`·그래프 엣지에 **선언된 단계**로 배선([3.6](#36-요청-시점-quant-호출--2026-08-10-팀-합의로-해결))
- `STRATEGY_PROPOSAL`은 `research → quant → qa → ceo`로 실행 — 백테스트 근거는 만들되 주문·원장 부서는 넣지 않는다

**남은 것 — BFF 디스패치**: `task_plan.workflow`가 `strategy-research`여도 **현재 BFF는 여전히 portfolio-recommendation 그래프를 실행한다.** 실제로 다른 그래프로 보내려면 두 가지가 더 필요하다.

1. `apps/api/main.py`가 `workflow` 값을 보고 디스패치 분기 (도현님 BFF 영역)
2. `strategy-research`의 `input_contract`가 `strategy_hypothesis`라 **자유 형식 질의를 그대로 받지 못한다.** 자연어 → `strategy_hypothesis` 변환 계약이 먼저 정의돼야 한다 (재일님 퀀트 영역)

지금은 `task_plan.workflow`가 **"이 요청은 여기가 처리 주체가 아니다"를 기록으로 남기는 단계**까지 와 있다. 침묵하던 공백이 관측 가능해진 것이 이번 변경의 범위다.

### 3.2 전략 → 포트폴리오 편입 승인 흐름이 없다

**현황**: `strategy-research.yaml`은 3단계로 끝난다.

```
quant-backtest → qa-release-review → ceo-promotion-review
   (strategy_candidate)  (strategy_qa_assessment)  (promotion_decision)
```

`promotion_decision`은 **Shadow/Paper 승격 여부**만 정한다. "이 전략을 사용자의 현재 포트폴리오에 추가한다"는 단계가 **존재하지 않는다.**

**필요한 설계**: 사용자 대화에서 생성된 전략은 다음 흐름을 타야 한다.

```
[1] quant-backtest : 사용자 요청 → Strategy Candidate + 백테스트 결과
[2] qa             : 재현성·근거·Model Risk 독립 검증
[3] risk           : 현재 포트폴리오와 합쳤을 때의 한도 위반 여부 (신규)
[4] accounting     : 현재 포트폴리오와의 중복·집중도 (신규)
[5] ceo            : 편입 제안서 작성 (advisory)
[6] 사용자 승인    : ← HITL. required_role=USER (신규)
[7] accounting     : strategy_allocations 반영 (승인된 경우만)
```

3·4번이 새로 필요한 이유: **기존 `strategy_research_cycle`은 전략 자체의 품질만 본다.** "이 전략이 좋은가"와 "이 전략을 *내 포트폴리오에* 넣어도 되는가"는 다른 질문이다. 후자는 현재 보유 종목과의 상관관계, 섹터 집중도, Mandate 한도 여유를 봐야 한다.

**기존 자산 활용**: `strategy_allocations.governance_allocation_id`가 [accounting config.yaml:88](../../departments/05-accounting-portfolio/hermes/config.yaml)에 이미 정의돼 있다 — *"CEO 승인 결정과 회계 적용 분리 추적"*. 편입 승인 기록을 여기 연결하면 된다.

### 3.3 이 흐름에 HITL 지점이 없다

**현황**: `required_role=USER`(사용자 승인)는 현재 **Mandate 변경에만** 존재한다([CEO api/app.py:1895](../../departments/00-ceo-office/api/app.py)).

포트폴리오 추천·전략 제안은 전부 `advisory`로 끝나고 사용자 승인 단계가 없다. 이는 "주문을 만들지 않으니 승인이 필요 없다"는 현재 설계에는 맞지만, **전략을 포트폴리오에 편입하는 것은 advisory가 아니라 상태 변경**이므로 승인이 필요하다.

**필요한 설계**:
- `governance.approvals`에 `approval_type = STRATEGY_ALLOCATION` 추가
- `required_role = USER`
- 거절·만료 시 안전 기본값: **편입하지 않고 기존 포트폴리오 유지**(개발 원칙 9)
- 기존 `MandateChangeWorkflow`의 `interrupt()`/`Command(resume=...)` 패턴 재사용 가능 (LangGraph checkpointer)

### 3.4 상시 대화의 세션 연속성이 없다

**현황**: `mandate_assistant.py`는 **Stateless**이고 온보딩 전용이다. 헤더 주석이 명시한다.

> *"이 모듈은 자연어 대화에서 구조화 값 '제안'만 만든다. **어떤 상태도 저장하거나 바꾸지 않는다**(Stateless)"*

`POST /ui/portfolio-recommendations`도 매 요청이 독립이다. "아까 그 종목 말인데"라는 후속 질문을 처리할 문맥이 없다.

**필요한 설계**:
- 대화 세션 개념 (`conversation_id`) — Hermes Memory Namespace 활용 검토
- 직전 `run_id`·`task_plan` 참조로 후속 질문 해석
- **주의**: 대화 이력이 판정에 영향을 주면 안 된다. 마스터플랜 §2334 — *"자유 형식 Agent 대화만으로 업무를 전달하지 않는다"*. 세션은 **질의 해석 보조**에만 쓰고, 각 부서로 가는 handoff는 항상 구조화 계약(`DepartmentHandoff`)이어야 한다.

### 3.5 자연어 의도 분류는 표 *앞*에 두되, 표 *대신* 두지 않는다

[2.2.1](#221-키워드-층은-실측상-거의-작동하지-않는다-2026-08-10-측정)에서 확인했듯 키워드 층은 실질적으로 비어 있다. 자연어 이해를 개선하려면 LLM이 필요하지만, **어디에 넣느냐**가 안전을 가른다.

```
사용자 자유 질의
    ↓
LLM 분류기 → 알려진 카테고리 중 하나로 매핑     ← 출력이 enum 으로 제한됨
    ↓
CATEGORY_WORKFLOWS / CATEGORY_DEPARTMENTS 표    ← 권한 경계, 결정론 유지
    ↓
부서 호출
```

| 방식 | 판정 |
|---|---|
| LLM이 **카테고리를 제안** | ✅ 안전 — 출력이 알려진 값 목록으로 제한되고, 모르면 fallback이 받는다 |
| LLM이 **부서 목록을 직접 생성** | ⚠️ 조건부 — 상·하한을 둘 다 강제해야만 성립한다(아래) |

#### 실제 구현 (2026-08-10, opt-in)

[orchestration/adapters/ceo_task_planner.py](../../orchestration/adapters/ceo_task_planner.py)가 **CEO Hermes 프로필을 직접 호출해 부서를 고르는** 경로를 제공한다. 위 표의 두 번째 방식이며, 다음 세 장치가 함께 있을 때만 안전하다.

| 장치 | 내용 |
|---|---|
| **상한** (allow-list) | 호출부가 넘긴 `valid_departments` 밖을 요청하면 `ValueError` → 결정론 fallback |
| **하한** (`REQUIRED_DEPARTMENTS`) | `qa`·`ceo`가 빠지면 **되살린다.** 결정론 표가 6개 카테고리 전부에 이 둘을 두고 있어 실질적 불변식이고, 빠지면 인용·환각 검증 없이 자문이 나간다. 거부가 아니라 보강인 이유는 그쪽이 안전 방향이기 때문 |
| **fail-closed** | 바이너리 부재·타임아웃·JSON 오류·빈 rationale — 어떤 실패든 결정론 계획으로 되돌아가고 `planner_fallback_reason`에 이유를 남긴다 |

**기본값은 결정론이다.** `PORTFOLIO_CEO_TASK_PLANNER_MODE=llm`일 때만 켜진다.

**응답 봉투는 결정론 계획을 기반으로 만든다.** planner 모듈은 `workflows`를 import 하지 않아(단방향 유지) `CATEGORY_WORKFLOWS` 같은 호출부 전용 값을 계산할 수 없다. 그래서 결정론 계획을 먼저 만들고 LLM이 정한 것만 덮어쓴다 — 이렇게 하지 않으면 `workflow`·`category_recognized`가 LLM 경로에서 기본값으로 덮인다.

계약은 [tests/test_ceo_task_planner.py](../../tests/test_ceo_task_planner.py) 8건이 고정한다. opt-in 경로는 평소 CI에서 돌지 않아 결함이 조용히 쌓이므로, 켜는 순간 드러나는 것들을 테스트로 잡아둔다.

이 패턴은 저장소에 이미 선례가 있다. [mandate_assistant.py](../../departments/00-ceo-office/src/mandate/mandate_assistant.py)의 `ALLOWED_SUGGESTION_FIELDS`가 같은 구조다 — LLM이 제안하되 허용 목록 밖 필드는 `dropped_fields`로 버리고 조용히 통과시키지 않는다.

**우선순위**: 이 작업은 [3.1](#31-전략-요청은-부서-추가가-아니라-workflow-선택-문제다)의 BFF 디스패치보다 **뒤**다. 갈 곳(워크플로 분기)이 없는 상태에서 분류 정확도를 올리면 정확히 분류된 요청이 여전히 같은 그래프로 가는 결과만 남는다.

### 3.6 요청 시점 quant 호출 — 2026-08-10 팀 합의로 해결

**배경이 된 문제**: `CATEGORY_DEPARTMENTS` 어디에도 quant가 없었고, [portfolio_recommendation.py](../../orchestration/workflows/portfolio_recommendation.py)에 `strategy.versions` 참조도 0건이었다. 즉 검증된 전략을 쓰는 것도, 요청 시점에 백테스트를 하는 것도 아닌 상태였다 — 추천에 백테스트 근거가 아예 없었다.

이는 [USER_INPUT_SCOPE_ANALYSIS.md](../01-product/USER_INPUT_SCOPE_ANALYSIS.md) §J *"포트폴리오 후보 카탈로그를 누가 만드는가 — 제품 최대 이슈"*의 미결정 상태가 낳은 공백이다.

**팀 결정**: 검증된 전략 카탈로그를 미리 구축하는 대신(§J의 J-1/J-2), **요청 시점에 `research → quant`를 호출**한다(J-3에 가까움).

**적용 범위 — 모든 요청이 아니다.** 응답성 때문에 카테고리로 나눈다.

| 카테고리 | quant | 이유 |
|---|:---:|---|
| `PORTFOLIO_RECOMMENDATION` | ✅ | 새 포트폴리오를 구성 |
| `REBALANCING_PROPOSAL` | ✅ | 포트폴리오를 재구성 |
| `STRATEGY_PROPOSAL` | ✅ | 전략 자체를 평가 |
| `MARKET_RESEARCH` | ❌ | "삼전 지금 사도 돼?" — 구성이 아니라 단건 질의. 백테스트를 끼우면 3단계가 4단계 + 실제 연산으로 늘어 대화 응답성이 무너진다 |
| `RISK_REVIEW` / `TAX_LIQUIDITY` | ❌ | 기존 보유분에 대한 질문이라 새 후보를 만들지 않는다 |

**MAS_PIPELINE_CONTRACTS와 충돌하지 않는 이유**: 그 문서가 금지한 것은 *"**암묵적으로** 끼워 넣는 것"*이다. 여기서는 `DEPARTMENTS` 튜플과 그래프 엣지에 **선언된 단계**로 넣었다. `strategy_research_cycle`(전략 승격용 quant → qa → ceo)은 그대로 별도 흐름으로 남으며, 한 부서가 두 흐름에 등장하는 것은 research·risk도 마찬가지다.

**실행 순서**: `research → quant → trading → risk → qa → accounting → ceo`. quant가 research 바로 다음인 이유는 백테스트 입력(가격·유니버스·피처)이 리서치 산출물이고, 그 결과가 trading/risk/qa 판단의 근거가 되어야 하기 때문이다.

**실제로 도는 quant 워커는 2명**이다 — `strategy-hypothesis-worker`, `dataset-feature-worker`(둘 다 `trigger: always`). 나머지 5명은 `backtest_request`·`release_candidate` 같은 전용 신호가 있을 때만 켜지며, 자유 질의에서는 `not_executed`로 기록된다.

**남은 것**: `expected_return`은 여전히 PIT 검증 근거가 없으면 `null`이다. 요청 시점 백테스트가 붙었다고 해서 자동으로 수익률을 주장하지 않는다 — 그 연결은 quant 산출물이 `instrument_recommendations`로 흘러가는 배선이 생긴 뒤의 일이다.

---

## 4. 권한 경계 — 이 흐름에서 절대 깨면 안 되는 것

대화형이라고 해서 완화되지 않는다.

| 경계 | 근거 |
|---|---|
| CEO 라우터는 **주문·Risk 승인·원장 변경을 만들지 않는다** | `build_ceo_task_plan()` docstring, MAS_PIPELINE_CONTRACTS §10.5 |
| 부서 선택은 **결정론 코드**가 한다. LLM은 설명·정제만 | §2.3 |
| 사용자 질의 원문은 **판정 근거가 아니다** | USER_INPUT_SPEC §5 — "자연어는 판정에 쓰지 않는다. 저장되고 맥락으로 전달될 뿐" |
| 전략 편입은 **사용자 승인 없이 실행되지 않는다** | §3.3 |
| 실패 시 안전 기본값은 **편입하지 않음 / HOLD** | 개발 원칙 9 |
| 부서 간 전달은 항상 `DepartmentHandoff` 구조화 계약 | MAS_PIPELINE_CONTRACTS §51 |

---

## 5. 문서 정합성 — 함께 고쳐야 할 것

### 5.1 CLAUDE.md의 "5개 흐름" 표에 `portfolio_recommendation_cycle`이 없다

`multi-agent-workflow.yaml:56`에는 정의돼 있는데 CLAUDE.md 흐름 표에는 빠져 있다. 실제로는 **6개 흐름**이다. 이 문서의 §3이 확정되면 CLAUDE.md를 함께 갱신한다.

### 5.2 흐름 간 다리(bridge)를 어떻게 볼 것인가

CLAUDE.md는 "5개 흐름은 서로 분리, 섞지 않는다"고 못 박았다. §3.2의 설계는 `portfolio_recommendation_cycle`에서 `strategy_research_cycle`을 호출하는 형태라 이 원칙과 충돌해 보인다.

**해석**: 섞지 말라는 것은 *"한 흐름 안에서 다른 흐름의 게이트를 건너뛰지 말라"*는 뜻으로 읽는 것이 타당하다. 전략 편입 흐름은 `strategy_research_cycle`의 QA·CEO 게이트를 **그대로 거친 뒤** 사용자 승인을 추가하는 것이므로 게이트를 우회하지 않는다. 다만 이 해석은 **ADR로 확정해야 한다** — 마스터플랜 변경에 해당할 수 있다.

---

## 6. 아직 정하지 않은 것

이 문서는 아래를 정하지 않는다. 담당자 확정 후 갱신한다.

1. **`STRATEGY_PROPOSAL` 카테고리의 정확한 부서 순서** — quant 다음에 risk를 넣을지, qa를 먼저 통과시킬지
2. **전략 편입 승인의 만료 시간** — Mandate 변경은 24시간 기본값을 쓴다(`api/app.py:349`). 전략도 같게 할지
3. **대화 세션 저장소** — Hermes Memory / Redis / Postgres 중 무엇인지
4. **동시 편입 제한** — 사용자가 여러 전략을 연속 제안받았을 때 몇 개까지 동시에 승인 대기로 둘지
5. **§5.2의 흐름 간 다리 허용 여부** — ADR 필요
