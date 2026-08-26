# CEO 부서 역할 위임 구현 지침 (ceo-runner 신설)

작성일: 2026-08-11 (KST)
담당: 영주 (CEO Office / 인사팀)
상태: **작업 A(`ceo-runner`) 구현 완료 2026-08-11 / 작업 B(만기 sweep) 미착수.**
이 문서는 구현 지침이며 설계 Source of Truth가 아니다
(상위 기준은 [WORKER_ROLE_BOUNDARIES.md](WORKER_ROLE_BOUNDARIES.md), 그 위는 마스터플랜).
같은 성격의 선례: [EVAL_RUNNER_SPEC.md](EVAL_RUNNER_SPEC.md), [PLATFORM_IAM_SPEC.md](PLATFORM_IAM_SPEC.md).

## 0. 왜 이 작업이 필요한가

Trading·Risk·QA·Accounting 네 부서는 2026-08-06~07에 "LLM이 판단하지 않는 일"을 결정론 러너로
분리했다(`desk-runner`/`risk-runner`/`qa-runner`/`back-office-runner`). **CEO는 이 정리를 한 번도
거치지 않은 유일한 부서다.** 그 결과가 [config.yaml:25](../../departments/00-ceo-office/hermes/config.yaml)의
`executive-orchestrator` 페르소나 한 문단에 Mandate 해석 + 6본부 라우팅 + 4개 위원회 소집 +
Chief-of-Staff 8개 업무가 전부 들어 있는 현재 상태다.

`staff_registry`(config.yaml:106-114)에 `deterministic_worker_count`가 없는 것이 그 증거다 —
Trading·Accounting은 이미 이 필드를 갖고 있다.

## 1. 역할 분리 결과

| | **CEO Head** | **ceo-runner** (신설) | **CEO Worker** |
|---|---|---|---|
| ID | `executive-orchestrator` | `ceo-runner` | `executive-briefing-worker` |
| 런타임 | Hermes Agent | 평범한 Python 함수 | 독립 LangGraph Graph |
| 모델 | Profile/Runtime 정본 참조 | **없음** | [Worker Model Matrix](WORKER_MODEL_MATRIX.md) 참조 |
| 호출 주체 | Hermes gateway | `run_employee_workers()`가 조건 없이 1회 | `run_worker_registry()` |
| 산출물 | `ceo_case_summary` (사용자 설명) | `facts` / `blockers` (**서술 필드 없음**) | `advisory_context` (서술) |
| 판단 | 함 (Mandate 해석·라우팅·재배분) | **안 함** (`decided_by: deterministic`) | 함 (부서 결과 종합·서술) |
| 권한 | 없음 (`binding_decision: false`) | 없음 (`authoritative: False`) | 없음 |
| 실패 시 | ESCALATE | blockers에 적고 통과 | ESCALATE |

### 현재 Head가 짊어진 업무의 이관 대상

| 현재 Head 업무 (config.yaml:25 / SOUL.md) | 이관 대상 | 근거 |
|---|---|---|
| Mandate 해석, 6본부 라우팅, 예산·SLA 배분 | **Head 유지** | 같은 입력에 다른 출력이 나오는 것이 산출물 = 판단 |
| 위원회 소집·정족수·veto | **이미 결정론** (`src/committee/`, Y2 완료) | Head는 API 호출만 |
| 각 부서 결과의 미완료·차단 상태 집계 | **ceo-runner로 이관** (작업 A) | Risk의 실행 전 verdict와 미완료 상태를 옮긴다. 일반 CEO 응답의 QA 결과는 사후 audit 관찰값이며 blocker가 아니다 |
| 만기 초과 항목 자동 escalate | **일반 결정론 모듈로 신설** (작업 B, 러너 아님) | 입력이 dispatch payload에 없다 (§4 참고) |
| 부서 결과 서술 종합·설명 | **Worker 유지** | 서술은 결정론화 대상이 아니다 |
| PM Pod/Book 실적 비교, capital efficiency | **보류** | 감쌀 결정론 모듈도, 계산할 원천 데이터도 아직 없다 (§5) |

## 2. 작업 A — `ceo-runner` 신설 (러너)

### 성립 근거 (WORKER_ROLE_BOUNDARIES.md §"판단 기준 두 개" — 둘 다 충족해야 함)

1. **부서장의 `input_contract`가 하나로 고정인가?** → **문자 그대로는 미충족(2026-08-11 구현 중 정정).**
   투자 Case만 보면 CEO는 [investment-case.yaml:80-90](../../orchestration/workflows/investment-case.yaml)에서
   `accounting_snapshot`을 받아 `ceo_case_summary`를 내지만, **CEO는 4개 workflow에 등장하고
   계약이 넷이다** — `accounting_snapshot` / `strategy_qa_assessment`(strategy-research) /
   `permission_review`(workforce-management) / `revision_qa_assessment`(agent-evolution).
   그런데도 러너가 성립하는 이유는 기준의 취지가 봉투의 *개수*가 아니라 *만들 수 있는가*이기
   때문이다 — 네 흐름 전부 `paper_pipeline._store()`가 **계약 이름을 키로** 산출물을
   `context["artifacts"]`에 쌓고 그 dict가 CEO payload에 그대로 들어오므로, 러너는 어느 흐름인지
   묻지 않고 같은 6개 이름을 조회하면 된다. 근거는 [WORKER_ROLE_BOUNDARIES.md](WORKER_ROLE_BOUNDARIES.md)
   §"CEO는 왜 러너를 두는가"로 옮겨 적었다.
2. **러너가 쓸 입력이 dispatch payload 안에 이미 다 있는가?** → **충족(HR이 막힌 진짜 지점).**
   [employee_workers.py](../../departments/00-ceo-office/employee_workers.py)의 `STAGE_INPUTS`가
   `research_packet`, `order_intent`, `risk_decision`, `qa_assessment`, `accounting_snapshot`,
   `strategy_report` 6개다. 실제로는 payload 최상위·`artifacts`·`workflow_context.artifacts`
   세 자리에 나뉘어 오므로 `_artifact_sources()`가 그 셋을 순서대로 본다(직접 넘긴 값 우선).

### 무엇을 계산하나 — **새 판정을 만들지 않는다**

러너는 이미 다른 부서 결정론 엔진이 확정한 판정을 옮기기만 한다. `desk_runner()`가
`plan_feasibility`/`certification` 플래그를 읽어 blockers로 바꾸는 것과 같은 일이다.

| 근거 | 읽는 필드 | blocker 조건 | 원천(이미 존재) |
|---|---|---|---|
| Risk 판정 | `risk_decision.verdict` | `!= "APPROVE"` | `RiskVerdict` (risk_engine.py) |
| Risk 만료 | `risk_decision.expires_at` | 현재 시각 초과 | `RiskDecision` (contracts.py:232) |
| ↳ 2026-08-11 실측 | — | **필드가 봉투에 없다** | `departments/03-risk/scripts.py`가 만드는 assessment dict에 `expires_at`이 없다. 러너는 이때 "기한 안"이라고 말하지 않고 `expiry_checked: false`로 적으며 blocker로도 올리지 않는다(매 케이스 걸리면 escalate가 곧 의미를 잃는다). 실제 만료 검사를 켜려면 **리스크본부가 봉투에 그 필드를 실어야 한다** — CEO Office가 대신 만들지 않는다 |
| QA 감사 결과 | `qa_assessment.decision` | 일반 CEO 응답의 blocker로 사용하지 않음 | CEO 응답 후 `qa-audit`이 기록하는 관찰값. 전략 승격·권한 승인 같은 별도 governance workflow의 선행 게이트는 해당 workflow가 소유 |
| 단계 누락 | 6개 `input_fields` | 값이 없음 → `missing_inputs` | — |

**`missing_inputs`가 이 러너의 핵심이다.** CEO의 workflow상 임무는 "각 결과를 통합해 사용자 설명과
**미완료 상태**를 보고"(investment-case.yaml:84)인데, 그 "미완료"는 판단이 아니라 조회다.
`back_office_runner()`의 `missing_blocks`("없는 것을 없다고 적는다")와 같은 원칙을 그대로 쓴다.

### 구현 위치와 템플릿

- 파일: `departments/00-ceo-office/employee_workers.py`에 구현 완료. 아래 내용은 최초 구현 템플릿이며 현재 코드를 덮어쓰는 지침이 아니다.
- **템플릿으로 삼을 것**: `desk_runner()` —
  [departments/02-trading/employee_workers.py:140-191](../../departments/02-trading/employee_workers.py)
  그리고 그 호출부 202-222줄. `back_office_runner()`(05-accounting-portfolio/employee_workers.py:276-311)도
  같은 모양이다.
- 반환 봉투 (desk_runner와 동일 형태, 필드 이름까지 맞춘다):

```python
{
    "worker_id": RUNNER_ID,          # "ceo-runner"
    "role": RUNNER_ROLE,
    "tools": list(RUNNER_TOOLS),
    "status": "COMPLETED",
    "attempts": 1,
    "llm": False,                    # ← 계약으로 박는다
    "output": {
        "worker_id": RUNNER_ID,
        "facts": facts,
        "missing_inputs": missing,
        "blockers": blockers,
        "escalate": bool(blockers),
        "decided_by": "deterministic",
        "authoritative": False,      # 판정은 Risk/QA/Accounting이 한다
    },
    "error": ";".join(errors) or None,
    "output_contract": "ceo.ceo-runner.v1",
}
```

- 호출부: `run_employee_workers()` 안에서 레지스트리 실행 뒤 직접 append.

```python
result = run_worker_registry(WORKER_SPECS, payload, tools=..., llm=llm)
runner = ceo_runner(payload)
result["workers"].append(runner)
result["executed"].append(RUNNER_ID)
result["llm_worker_count"] = len(WORKER_SPECS)
return result
```

## 3. 필수 안전장치 (WORKER_ROLE_BOUNDARIES.md:114-119 그대로)

러너는 이름만 Worker이고 LLM이 없다. **그 사실을 프롬프트 문장이 아니라 코드로 강제한다.**

- **`WORKER_SPECS` 레지스트리 밖에 둔다.** 공용 런타임(`run_worker_registry`)은 그래프마다 LLM을
  부르므로, 레지스트리에 넣으면 "LLM 없음"이 프롬프트 문장이 되고 실행 경로로는 뚫린다.
  네 부서 러너 전부 이 방식이다.
- **`summary` 같은 서술 필드를 만들지 않는다.** 문장을 만들 자리가 없으면 그 자리에서 환각도 안 생긴다.
- **RAG 정책표는 해당 없음.** CEO는 `agentic_rag: framework: null`(config.yaml:43-46)이라
  Trading의 `rag_router.py` 같은 정책표가 없다. 나중에 도입하면 그때 러너 항목을 비워
  fail-closed를 함께 가져간다.
- **`tool_allowlist`를 러너에 명시한다.** 감사에서 "이 직원이 무엇을 읽었나"가 남아야 한다.
  CEO Worker가 쓰는 `ceo.department_reports.read` 기준으로 시작한다.

## 4. 작업 B — 만기 초과 sweep (**러너가 아니라 일반 결정론 모듈**)

이걸 러너에 넣으면 안 된다. WORKER_ROLE_BOUNDARIES.md §89-98이 구분하는 대로,
**러너와 일반 결정론 모듈의 차이는 LLM 유무가 아니라 결과가 어디로 가는가**다.
만기 대상은 dispatch payload가 아니라 DB에 있으므로 판단 기준 2를 충족하지 못한다
(HR이 러너를 못 둔 것과 같은 이유).

### 현재 공백

- [escalation.py:80](../../departments/00-ceo-office/src/escalation/escalation.py)의
  `EscalationRecord.due_at`은 **필드로만 존재한다.** `open_escalation()`(97줄)이
  `due_at > created_at`을 검증까지 하는데, **그 뒤로 아무도 "지금 지났는지"를 보지 않는다.**
- `api/app.py`의 `GET /governance/v1/escalations`는 `list_open()`으로 status/target만 거르고
  만기는 계산하지 않는다. `find_expired` 계열 엔드포인트가 없다.
- `action_item`이라는 식별자는 저장소 전체에 0건이다 — 회의 결정·액션 아이템 추적은
  SOUL.md의 서술로만 있고 스키마도 코드도 없다.

### 만들 것

1. **순수 함수 `find_overdue(escalation, at) -> EscalationRecord | None`** —
   `escalation.py`에 `transition()` 옆에 추가. **템플릿은 이미 저장소 안에 있다**:
   [approval.py:296 `expire()`](../../departments/00-ceo-office/src/approval/approval.py) —
   "대상이 아니면 None을 준다, 이미 결정된 건은 건드리지 않는다"는 그 모양을 그대로 따른다.
   - 터미널 상태(RESOLVED/CANCELLED)면 `None`
   - `due_at`이 `None`이면 `None` (만기 없음과 만기 지남을 구분한다 — HR
     `aggregate_quality()`의 None/0 구분과 같은 원칙)
   - `due_at <= at`인 OPEN/ACKNOWLEDGED만 대상
2. **Repository 조회** — `postgres_escalation_repository.py`에 만기 후보 조회 추가.
3. **API 엔드포인트** — `GET /governance/v1/escalations?overdue=true` 또는 sweep 트리거.
4. **스케줄 실행** — `notification-worker`(`governance_events/worker.py`, 이미 컨테이너로
   등록됨)와 같은 패턴. 새 컨테이너를 만들지 말고 기존 worker에 얹을지 먼저 검토한다.

**자동 escalate까지 갈지는 별도 판단이다.** 만기를 *탐지*하는 것과 그걸 근거로 새 escalation을
*생성*하는 것은 다른 권한이다. 1~3단계(탐지·노출)를 먼저 하고, 자동 생성은 그 다음 PR로 분리한다.

## 5. 지금 하지 않는 것

- **PM Pod/Book 실적 요약, capital efficiency·Capacity 비교** — SOUL.md:12와 마스터플랜
  2128-2129에 CEO 임무로 적혀 있으나, 계산할 결정론 모듈이 없다.
  `05-accounting-portfolio`의 `value_portfolio()`는 단일 스냅샷, `build_accounting_sections(repo, book_id)`는
  단일 book이라 **여러 Pod/Book을 가로질러 비교하는 원천 자체가 없다.** 회계본부 소유
  영역이므로 CEO Office가 대신 만들지 않는다(CLAUDE.md 담당자 경계).
- **CEO Worker를 여러 개로 쪼개는 것** — 판단 기준 1을 이미 충족(단일 계약)하므로 Worker 1개
  구조가 맞는 설계다. 문제는 Worker 수가 아니라 Head 페르소나에 부기 작업이 섞인 것이었다.

## 6. 함께 갱신할 것

- **`departments/00-ceo-office/hermes/config.yaml`**
  - `staff_registry`에 `deterministic_worker_count: 1`, `headcount_total: 2` 추가.
    **`worker_count`는 1로 유지한다** — LLM 직원 수이고 러너는 따로 센다
    (Trading `worker_count: 2 / deterministic_worker_count: 1`, Accounting `1 / 1`과 같은 규칙).
  - 러너를 둔 근거를 주석으로 남긴다(Trading·Accounting config가 그렇게 하고 있다).
- **`multi-agent-workflow.yaml`** — `worker_counts.ceo-agent` 주석에 러너가 별도임을 명시
  (`trading-department: 2  # LLM Worker only; deterministic desk-runner is separate` 참고).
- **`docs/02-engineering/WORKER_ROLE_BOUNDARIES.md`**
  - §"확정 Worker Registry" 표의 CEO 행에 결정론 러너 반영.
  - §"결정론 Worker(러너)를 두는 부서와 두지 않는 부서"에 CEO 판단 근거 추가
    (현재 이 절에 CEO가 아예 없다).
- **`docs/02-engineering/WORKER_MODEL_MATRIX.md`** — 러너는 모델을 부르지 않으므로 표에
  넣지 않되, 부서별 배치 표의 CEO 행 각주를 맞춘다.

## 7. 검증

```bash
source ~/claude/bin/activate
python departments/00-ceo-office/employee_workers.py       # 자체 점검(assert) 추가 필요
python -m pytest tests/test_worker_architecture.py -q -rs  # Registry 수·topology·Profile 메타데이터 대조
python -m pytest tests -q -rs
```

`tests/test_worker_architecture.py`가 config의 `staff_registry` 값과 런타임 `WORKER_SPECS`를
대조하므로, §6의 config 갱신과 코드 변경은 **같은 커밋에 함께 들어가야 한다.**

## 8. 커밋·PR 규칙

- 브랜치: `ceo-agent` (CEO Office 작업). 새 브랜치를 만들지 않는다.
- 커밋 메시지: 한국어 개조식, conventional prefix (`feat(ceo):` / `fix(ceo):`).
- 작업 A(러너)와 작업 B(만기 sweep)는 **별도 PR로 나눈다** — 전자는 payload 전달이고
  후자는 새 비즈니스 로직이라 리뷰 성격이 다르다.
