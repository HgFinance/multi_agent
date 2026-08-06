# ADR-0005: Bull/Bear Worker 분리와 3라운드 독립 토론

- 상태: Accepted
- 날짜: 2026-08-05
- 제안: 도현 (트레이딩본부)
- 영향 본부: 트레이딩
- 관련: [WORKER_ROLE_BOUNDARIES.md](../WORKER_ROLE_BOUNDARIES.md),
  [HEDGE_FUND_MASTER_PLAN.md](../../HEDGE_FUND_MASTER_PLAN.md) 5.5 #2,
  [AGENT_EMPLOYEE_PROFILES.md](../../04-organization/AGENT_EMPLOYEE_PROFILES.md) TRD-01/TRD-02

## 배경

트레이딩본부 직원 레지스트리에 `market-thesis-worker` 하나가
`role: Bull and bear market-thesis debate analyst` 로 등록돼 있었다. 직원 **한 명**이
강세 논지와 약세 논지를 **둘 다** 생성하는 구조다.

같은 저장소의 `departments/02-trading/scripts.py` 는 이미 반대 구조를 쓰고 있었다 —
`bull_researcher`(TRD-01)와 `bear_researcher`(TRD-02)를 병렬 독립 노드로 두고,
`_check_bear_never_sees_bull()` 이 프롬프트가 아니라 **payload 배선**을 직접 검사해
한쪽이 다른 쪽 출력을 못 보게 강제한다. 두 페르소나 원문에도
"never see the Bear output" / "never receive the Bull output" 이 명시돼 있다.

즉 조직 설계(TRD-01/TRD-02 분리)와 토론 파이프라인은 독립성을 요구하는데,
직원 레지스트리만 그것을 합쳐놨다.

## 결정

### 1. `market-thesis-worker` 를 두 직원으로 나눈다

| 신규 worker_id | role | 대응 |
|---|---|---|
| `bull-thesis-worker` | Independent bull-case thesis analyst | `scripts.py` `bull_researcher`, 페르소나 `bull-researcher`(TRD-01) |
| `bear-thesis-worker` | Independent bear-case thesis analyst | `scripts.py` `bear_researcher`, 페르소나 `bear-researcher`(TRD-02) |

트레이딩 Worker Registry: **6 → 7** (always 3 / conditional 4).
새 페르소나를 쓰지 않는다 — `config.yaml` 의 기존 TRD-01/TRD-02 원문을 공유한다.

### 2. 3라운드 토론. 상대 원문은 어느 라운드에도 넘기지 않는다

```
R1  독립 병렬 생성 — 같은 Claim 색인만 받는다. 서로 못 본다
R2  쟁점 목록만 받아 보강 — 넘기는 것은 Claim id 뿐이고 상대 문장이 없다
R3  결정론 종합 — 코드가 계산한다. verdict 를 만들지 않는다
```

R2 에서 넘기는 것은 `contested_refs` / `opponent_only_refs` / `my_only_refs` /
`untouched_refs` 네 개의 **Claim id 목록**이다. 프롬프트도 "상대를 반박하라"가 아니라
"이 Claim 들을 네 입장에서 아직 안 다뤘다면 다뤄라"다 — 상대 논거가 아니라 근거
커버리지를 요구한다.

### 3. 직원 계층에서도 상대 원문을 차단한다

`skills/worker_evidence.py` 의 `bull_debate_evidence` / `bear_debate_evidence` 가
자기 쪽 산출과 Claim id 목록만 evidence 에 싣고 상대 문장을 제외한다.
두 직원 모두 `rag_route: NO_RAG` 다 — 한쪽만 검색을 얻으면 그것이 곧 비대칭 근거다.

## 근거

**확증편향은 산출물이 아니라 입력 구조에서 생긴다.** 한 직원이 양쪽을 다 쓰면 먼저
세운 논지가 나중 논지의 앵커가 되고, 두 논지의 상관이 0 이 아닌 것을 사후에 측정할
방법도 없다. 두 직원으로 나눠 같은 근거를 주고 **서로를 못 보게** 해야
"독립성 위반 0 / 문장 복제 0"(TRD-01/TRD-02 KPI)이 측정 가능한 값이 된다.

상대 출력을 입력으로 주는 진짜 대화형 토론도 검토했으나 채택하지 않았다. 먼저 말한
쪽이 앵커가 되어 위 KPI 와 두 페르소나의 금지 문장을 동시에 포기해야 한다.
대신 R2 에서 **id 만** 넘겨 반박의 실익(빠진 근거를 메우게 하는 것)은 얻고 앵커링은
피한다.

## 대가

- **부서 밖 계약 5곳이 같이 바뀐다.** Worker Registry 수를 적은 문서
  (마스터플랜 포함)와 `tests/test_worker_architecture.py` 의 개수 계약.
  마스터플랜 변경이라 이 ADR 이 그 근거다.
- **LLM 호출이 는다.** 토론 2회 → 4회(R1 2 + R2 2). 직원 레지스트리는 로컬
  `qwen3:1.7b` async 병렬이라 부담이 작고, R1 이 실패하면 R2 를 건너뛴다.
- Bull/Bear 가 서로 못 보므로 **직접 반박은 없다.** 쟁점 대조가 그 자리를 대신하며,
  두 논지의 충돌 지점은 R3 의 결정론 종합이 계산한다.

## 지키는 경계

이 변경으로 판정 권한이 이동하지 않는다.

- R3 종합은 **결정론이고 verdict·수량·방향을 만들지 않는다.** 산출은 근거 커버리지,
  미해결 쟁점, 인용 정확도, 라운드별 독립성 위반 수, `grounded` 뿐이다.
- 방향과 수량은 여전히 Strategy Signal 의 `target_weight` 와 리스크본부가 정한다.
- 토론 산출은 `propose_intent` 를 거쳐 OrderIntent **제안**까지만 간다.
  `risk_decision_id` 가 없고 `submittable: false` 이며 OMS 가 제출을 거부한다.
- 직원 산출은 `binding: false` 다.

## 영향 파일

- `departments/02-trading/employee_workers.py` — `WORKER_SPECS` 7명
- `departments/02-trading/hermes/config.yaml` — `workers`, `staff_registry`,
  `runtime_personalities`(2곳)
- `departments/02-trading/skills/` — 신규 Skill/RAG 경계 패키지
- `departments/02-trading/scripts.py` — 3라운드 그래프
- `tests/test_worker_architecture.py` — 개수 계약 `(7, 3, 4)`
- Registry 수를 적은 문서: `HEDGE_FUND_MASTER_PLAN.md`, `CLAUDE.md`,
  `PROJECT_IMPLEMENTATION_STATUS.md`, `AGENT_EMPLOYEE_PROFILES.md`,
  `OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md`, `WORKER_ROLE_BOUNDARIES.md`
