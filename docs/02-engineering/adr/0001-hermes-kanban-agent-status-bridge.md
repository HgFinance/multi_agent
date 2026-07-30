# ADR-0001: Hermes Kanban을 AI Office Agent 상태의 Event 소스로 연결

> **Status: Proposed — 아직 승인되지 않았다.** 이 문서는 팀 검토·결정을 요청하는 제안서다.
> 승인 전까지 [AI_OFFICE_FRONTEND_PLAN.md](../AI_OFFICE_FRONTEND_PLAN.md) 등 확정 문서를 변경하지 않는다.
> 승인되면 CLAUDE.md 문서 규칙대로 이 ADR과 영향받는 문서(AI_OFFICE_FRONTEND_PLAN.md §5.2/§5.4)를
> 같은 PR에서 함께 갱신한다.
>
> - 작성: 동규님 (Risk/QA)
> - 작성일: 2026-07-30
> - 검토 요청 대상: 팀 리더, 특히 영주님(Live Office Business Owner), 공통 Frontend Platform 담당(미지정)

---

## 1. 배경 — 지금 뭐가 빠져 있나

[AI_OFFICE_FRONTEND_PLAN.md](../AI_OFFICE_FRONTEND_PLAN.md) §5.2는 이미 연결 순서를 정의해뒀다.

```
AI Office(프론트) → FastAPI BFF → Redis Streams(Domain Event) + Supabase Read Models
```

§5.4는 Agent 상태도 이미 8가지로 못박아뒀다: `OFFLINE | IDLE | QUEUED | RUNNING | WAITING_APPROVAL | BLOCKED | DEGRADED | ERROR`.

근데 이 Agent 상태가 **실제로 어디서 나오는지**는 아직 정의돼 있지 않다. §5.2 다이어그램의 Event 소스는 Redis Streams·Supabase Read Models·Market API뿐이고, "부서장 Agent가 지금 뭘 하고 있는가"를 만들어내는 소스가 없다. Phase UI-1의 완료 기준("Agent와 부서 상태가 공식 Event·Read Model에서만 생성된다")을 채우려면 이 소스가 있어야 한다.

## 2. 발견 — Hermes에 이미 이 상태를 들고 있는 기능이 있다

이번 주 Risk/QA 부서 작업 중 확인한 사실: 로컬에 설치된 Hermes Agent(v0.19.0)에 **`kanban`**이라는, "여러 Hermes Profile이 공유하는 SQLite 기반 영속 Task Board"가 이미 내장돼 있다.

- `hermes kanban assignees`로 확인한 결과, 8개 부서 Hermes Profile(`risk-management`, `qa-department` 등)이 이미 Assignee 후보로 등록돼 있다.
- Task는 `triage → todo → scheduled/blocked → ready → running → done`을 거치고, `--parent`로 의존성을, `block --kind`로 자동/수동 차단을 표현한다.
- `decompose`/`swarm`으로 Task를 병렬 Worker + Verifier + Synthesizer 그래프로 자동 분해할 수 있다.

이건 §5.2가 필요로 하는 "부서/Agent별 지금 상태" 데이터를 이미 구조화해서 들고 있는 시스템이다 — 새로 안 만들어도 된다.

## 3. 제안 — kanban 상태를 §5.4 Agent 상태 계약에 매핑

| kanban Task 상태 | → §5.4 Agent 상태 | 비고 |
|---|---|---|
| Assignee에게 배정된 Task 없음 | `IDLE` | |
| todo / scheduled (부모 Task 대기) | `QUEUED` | |
| running (claim됨, Worker 실행 중) | `RUNNING` | |
| blocked, kind=`needs_input`/`capability` | `WAITING_APPROVAL` | 사람 개입 필요 — CEO/Risk/QA 승인 게이트에 대응 |
| blocked, kind=`dependency` | `BLOCKED` | 부모 Task 끝나면 자동 해제, 사람 개입 불필요 |
| `--failure-limit` 소진으로 Dispatcher가 자동 block | `ERROR` | |
| Hermes Profile 프로세스/Gateway 자체가 안 떠 있음 | `OFFLINE` | kanban과 별개로 Profile 상태 자체를 확인해야 함 |

## 4. 제안 — §5.2 연결 순서에 추가할 것

새 컴포넌트 하나만 추가한다. **BFF·WebSocket·프론트는 무엇도 안 바뀐다.**

```
[신규] Kanban Event Publisher (작은 Adapter)
  - hermes kanban watch 또는 task_events 테이블 polling으로 상태 변화 감지
  - §5.3 UI Event Envelope 형식으로 변환해 Redis Streams에 publish
      event_type: "agent.status.v1"
      payload: { task_id, dept_id, agent_status, task_title }
  - 여기까지만 하고 끝 — 이후는 기존 §5.2 파이프라인(BFF → WebSocket)이 그대로 처리
```

이 Adapter는 **읽기 전용 상태 발행자**다. kanban에 명령을 내리는 방향(Task 생성/할당)은 이 흐름과 분리한다.

## 5. 이 제안이 건드리지 않는 것 (경계 확인)

- **§6 명령과 권한 경계는 그대로다.** 이 Adapter는 Domain Event를 "발행"만 하지, Command를 처리하지 않는다. Risk 승인, Kill Switch, 주문 전송은 여전히 결정론적 Service가 하고, 프론트는 여전히 Backend의 허용된 Command만 호출한다.
- **kanban이 재무 통제 규칙을 대신하지 않는다.** kanban Task 배정·의존성은 "누가 언제 뭘 하는지"를 나르는 배송 수단일 뿐이다. Risk 승인 게이트(`risk_engine.py`)와 QA 독립 검증(`evidence_qa_engine.py`)이 여전히 유일한 권한 주체다. kanban Task graph를 설계할 때 지켜야 할 원칙(이미 Risk/QA 내부적으로 정리함):
  - `--parent`로 승인 선행 조건을 그래프로 강제 (예: Risk 승인 Task가 끝나야 OMS 전송 Task가 `ready`가 됨)
  - CEO/Risk/QA 승인이 필요한 Task는 `--initial-status blocked`로 시작해 사람이 직접 `unblock`
  - `swarm`의 `--verifier`는 그 Task를 만든 부서 자신이 될 수 없음 (자기 산출물 자기 검증 금지)
  - 각 부서 Hermes Profile의 `tool_allowlist`/`forbidden_tools`가 실제 실행 권한의 1차 방화벽 (Risk/QA엔 아직 이 필드가 없어 별도로 채워야 함 — 이 ADR과 별개 작업)

## 6. 소유권과 열린 질문 (§11 기준)

AI_OFFICE_FRONTEND_PLAN.md §11 소유권 표에 이 제안이 걸치는 영역:

| 영역 | Business Owner | 이 제안에서 필요한 것 |
|---|---|---|
| Live Office·CEO·Workforce | 영주님 | Agent 상태 Projection에 kanban 소스가 섞이는 것을 승인해야 함 |
| Risk·Audit·Incident | 동규님(본인) | Risk/QA Task의 Event Contract는 직접 설계 가능 (본 ADR 범위) |
| 공통 Frontend Platform | **미지정** | Kanban Event Publisher를 실제로 만들 담당자가 없다 — 이 ADR 승인 전에 정해져야 함 |

**팀 리더에게 요청하는 결정 3가지:**
1. 이 방향(kanban을 Agent 상태 소스로 씀)을 채택할지 여부.
2. 채택한다면 "공통 Frontend Platform" 담당자를 지정할지.
3. 채택 시 AI_OFFICE_FRONTEND_PLAN.md §5.2/§5.4에 이 내용을 반영하는 후속 PR을 누가 낼지 (본 ADR과 같은 PR로 처리하는 게 CLAUDE.md 문서 규칙에 맞다).

## 7. 채택 시 구현 순서 (제안)

1. Risk/QA `config.yaml`에 `tool_allowlist`/`forbidden_tools` 채우기 (선행 조건, 이 ADR과 무관하게 필요)
2. Risk/QA 부서 것부터 실제 kanban Task 1~2개로 소규모 시범(예: Evidence QA Case 하나) — Task graph에 §5 원칙 적용
3. Kanban Event Publisher Adapter 프로토타입 (읽기 전용, Redis Streams에 `agent.status.v1` 발행까지만)
4. AI_OFFICE_FRONTEND_PLAN.md §5.2 다이어그램·§5.4 표에 반영 + 이 ADR을 `Accepted`로 갱신
5. 나머지 6개 부서로 확장은 각 담당자가 자기 부서 것부터

## 8. 대안과 기각 사유

- **프론트가 kanban.db(SQLite)를 직접 읽는다** — 기각. §5.1 원칙("Frontend State는 언제든 Snapshot과 Event로 재구축 가능한 Projection")과 §6 권한 경계를 깨고, Browser가 로컬 파일 시스템 기반 DB에 직접 접근하는 경로가 생겨 Auth·감사가 불가능해진다.
- **부서장 Hermes Profile끼리 Slack에서 자유 대화** — 기각(별도 논의에서 이미 결론). §19.19 "자유 형식 Agent 대화만으로 업무를 전달하지 않는다"를 어긴다. Slack은 kanban Task의 알림/구독(`notify-subscribe`) 창구로만 쓴다.
