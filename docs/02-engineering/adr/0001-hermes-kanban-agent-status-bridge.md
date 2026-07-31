# ADR-0001: Hermes Kanban을 AI Office Agent 상태의 Event 소스로 연결

> **Status: Accepted — 2026-07-31 채택.**
> [AI_OFFICE_FRONTEND_PLAN.md](../AI_OFFICE_FRONTEND_PLAN.md) §5.2/§5.4와
> [PROJECT_IMPLEMENTATION_STATUS.md](../../PROJECT_IMPLEMENTATION_STATUS.md)에 같은 결정이 반영됐다.
>
> - 작성: 동규님 (Risk/QA)
> - 작성일: 2026-07-30
> - 결정일: 2026-07-31
> - Business Owner: 영주님
> - 기술 DRI·구현 PR Owner: 도현님
> - Risk·QA Contract Reviewer: 동규님

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

## 3. 결정 — kanban 상태를 §5.4 Agent 상태 계약에 매핑

| kanban Task 상태 | → §5.4 Agent 상태 | 비고 |
|---|---|---|
| Assignee에게 배정된 Task 없음 | `IDLE` | |
| todo / scheduled (부모 Task 대기) | `QUEUED` | |
| running (claim됨, Worker 실행 중) | `RUNNING` | |
| blocked, kind=`needs_input`/`capability` | `WAITING_APPROVAL` | 사람 개입 필요 — CEO/Risk/QA 승인 게이트에 대응 |
| blocked, kind=`dependency` | `BLOCKED` | 부모 Task 끝나면 자동 해제, 사람 개입 불필요 |
| `--failure-limit` 소진으로 Dispatcher가 자동 block | `ERROR` | |
| Hermes Profile 프로세스/Gateway 자체가 안 떠 있음 | `OFFLINE` | kanban과 별개로 Profile 상태 자체를 확인해야 함 |

`DEGRADED`는 Kanban Task 상태가 아니라 Runtime·Tool·Model Gateway의 Health Event에서 만든다.
한 Agent에 Task가 여러 개면 `ERROR > WAITING_APPROVAL > BLOCKED > RUNNING > QUEUED > IDLE`
순으로 대표 상태를 정하고, 화면에는 상태별 Task 수를 함께 제공한다. Runtime Heartbeat가 없으면
Task 상태와 무관하게 `OFFLINE`으로 표시한다.

## 4. 결정 — §5.2 연결 순서에 추가할 것

Kanban을 새 업무 시스템으로 다시 만들지 않는다. 읽기 전용 Bridge와 기존 Event Projection 경계를 추가한다.

```
[신규] Kanban Status Bridge (작은 Adapter)
  - hermes kanban watch 또는 task_events 테이블 polling으로 상태 변화 감지
  - §5.3 UI Event Envelope 형식으로 변환해 Redis Streams에 publish
      event_type: "agent.status.v1"
      payload: { task_id, parent_task_id, department_id, profile_id,
                 source_status, agent_status, blocked_kind, task_title,
                 board_updated_at }

[기존 공통 Projection 경계]
  - Redis Streams의 agent.status.v1을 멱등 소비
  - Supabase Agent Status Read Model 갱신
  - BFF의 GET /ui/snapshot과 /ws/operations가 같은 Version을 제공
```

Bridge는 **읽기 전용 상태 발행자**다. Kanban에 명령을 내리는 방향(Task 생성/할당)은 이 흐름과
분리한다. Browser는 SQLite를 직접 읽지 않으며, Redis Event 누락·재시작 후에는 Supabase Read Model로
복구한다. Event만 발행하고 Snapshot 원천을 만들지 않으면 Phase UI-1의 Gap Recovery를 만족할 수 없으므로
Projector와 Read Model을 필수 범위로 확정한다.

Event 멱등 키는 `kanban_board_id + task_id + source_version/status_updated_at`에서 만들고, 원문 Task 제목은
Secret·개인정보·미공개 주문 정보를 포함하지 않도록 작성 정책과 Payload Sanitizer를 적용한다.

## 5. 이 제안이 건드리지 않는 것 (경계 확인)

- **§6 명령과 권한 경계는 그대로다.** 이 Adapter는 Domain Event를 "발행"만 하지, Command를 처리하지 않는다. Risk 승인, Kill Switch, 주문 전송은 여전히 결정론적 Service가 하고, 프론트는 여전히 Backend의 허용된 Command만 호출한다.
- **kanban이 재무 통제 규칙을 대신하지 않는다.** kanban Task 배정·의존성은 "누가 언제 뭘 하는지"를 나르는 배송 수단일 뿐이다. Risk 승인 게이트(`risk_engine.py`)와 QA 독립 검증(`evidence_qa_engine.py`)이 여전히 유일한 권한 주체다. kanban Task graph를 설계할 때 지켜야 할 원칙(이미 Risk/QA 내부적으로 정리함):
  - `--parent`로 승인 선행 조건을 그래프로 강제 (예: Risk 승인 Task가 끝나야 OMS 전송 Task가 `ready`가 됨)
  - CEO/Risk/QA 승인이 필요한 Task는 `--initial-status blocked`로 시작해 사람이 직접 `unblock`
  - `swarm`의 `--verifier`는 그 Task를 만든 부서 자신이 될 수 없음 (자기 산출물 자기 검증 금지)
  - 각 부서 Hermes Profile의 `tool_allowlist`/`forbidden_tools`가 실제 실행 권한의 1차 방화벽 (Risk/QA엔 아직 이 필드가 없어 별도로 채워야 함 — 이 ADR과 별개 작업)

## 6. 소유권 결정 (§11 기준)

AI_OFFICE_FRONTEND_PLAN.md §11 소유권 표에 이 제안이 걸치는 영역:

| 영역 | Business Owner | 이 제안에서 필요한 것 |
|---|---|---|
| Live Office·CEO·Workforce | 영주님 | Agent 상태 Projection에 kanban 소스가 섞이는 것을 승인해야 함 |
| Risk·Audit·Incident | 동규님(본인) | Risk/QA Task의 Event Contract는 직접 설계 가능 (본 ADR 범위) |
| 공통 Frontend Platform | **도현님** | Kanban Status Bridge, Agent Status Projector, BFF/WebSocket와 Frontend Store 구현 |

결정 결과:

1. Hermes Kanban을 Agent 업무 상태 Source로 채택한다.
2. 영주님은 Live Office Business Owner, 도현님은 공통 Frontend Platform 기술 DRI를 맡는다.
3. 동규님은 상태 매핑, 권한 분리와 오표시 방지 Test를 Review한다.
4. 확정 문서 반영은 ADR 채택과 같은 변경에서 처리하고, 후속 구현 PR은 도현님이 낸다.

## 7. 구현 순서

1. Risk/QA `config.yaml`에 `tool_allowlist`/`forbidden_tools` 채우기 (선행 조건, 이 ADR과 무관하게 필요)
2. `agent.status.v1` Schema, 상태 우선순위, 멱등 키와 Supabase Projection Contract Test 확정
3. Risk/QA 부서부터 실제 Kanban Task 1~2개로 소규모 시범(예: Evidence QA Case 하나)
4. Kanban Status Bridge Prototype과 Redis Stream 발행
5. Agent Status Projector, `/ui/snapshot`과 `/ws/operations` 연결 및 Gap Recovery Test
6. 나머지 6개 조직으로 확장하되 각 담당자가 자기 부서 Task 의미·민감도를 검토

## 8. 대안과 기각 사유

- **프론트가 kanban.db(SQLite)를 직접 읽는다** — 기각. §5.1 원칙("Frontend State는 언제든 Snapshot과 Event로 재구축 가능한 Projection")과 §6 권한 경계를 깨고, Browser가 로컬 파일 시스템 기반 DB에 직접 접근하는 경로가 생겨 Auth·감사가 불가능해진다.
- **부서장 Hermes Profile끼리 Slack에서 자유 대화** — 기각(별도 논의에서 이미 결론). §19.19 "자유 형식 Agent 대화만으로 업무를 전달하지 않는다"를 어긴다. Slack은 kanban Task의 알림/구독(`notify-subscribe`) 창구로만 쓴다.
