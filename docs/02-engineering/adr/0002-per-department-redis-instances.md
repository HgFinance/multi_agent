# ADR-0002: 부서별 독립 Redis 인스턴스 도입 여부

> **Status: Proposed — 아직 채택되지 않음. 팀 검토 대기.**
>
> - 작성: 영주님 (CEO Office / Agent Workforce 인사팀)
> - 작성일: 2026-08-03
> - 제안 배경: "통합 Redis 하나만 쓰면 메모리가 너무 커질 것 같다"는 우려로 부서별 Redis 분리를 검토
> - 현재 결정(변경 대상): [TECH_STACK_DECISIONS.md §5](../TECH_STACK_DECISIONS.md#5-redis-사용-결정),
>   [DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md §5](../DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md#5-공통-platform-container)
>   — 둘 다 Redis를 "특정 부서 소유가 아닌 공통 Platform Container" 1개로 이미 확정해뒀다.
> - 공통 Platform Container 기술 DRI: 도현님 (본 ADR의 최종 결정권자)
> - 현재 유일한 실사용 부서: 동규님(Risk·QA) — `risk-api`/`audit-api`/`qa-worker`가 `redis` 서비스를
>   `risk_events`/`qa_events` Stream으로 공유 중 (docker-compose.yml)

---

## 1. 배경 — 지금 결정돼 있는 것

`TECH_STACK_DECISIONS.md` §5는 Redis를 P0 필수 도구로 확정하면서 "초기에는 Docker Redis를 사용한다"고만 정했고, 인스턴스를 부서별로 쪼갤지는 다루지 않았다. `DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md` §5는 한 걸음 더 나아가 `redis`를 **8개 부서 중 누구의 소유도 아닌 공통 Platform Container**로 명시했고, 현재 `docker-compose.yml`의 주석도 같은 근거를 든다.

```yaml
# ▶ redis: Risk↔QA Event Stream(risk_events/qa_events)이 쓰는 공통 Core 인프라다.
#   특정 부서 소유가 아니다(계획서 5절 "공통 Platform Container") - 다른 부서도
#   Redis Streams가 필요해지면 이 서비스를 재사용하면 된다.
```

지금 실제로 Redis를 쓰는 곳은 Risk·QA 둘뿐이다(`mem_limit: 256m`, 재시작 시 Stream 유실 감수 - AOF 미적용). CEO Office와 Agent Workforce 인사팀(이번 세션에서 컨테이너화한 `governance-api`/`workforce-api`)은 아직 Redis에 의존하지 않는다.

## 2. 문제 제기 — 왜 다시 검토하나

`DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md` §4 Topology와 §8.3 Stream 분리 표를 보면, 목표 상태에서는 8개 부서 전부가 최소 하나 이상의 Domain Event Stream(`hf:market`, `hf:case`, `hf:execution`, `hf:accounting`, `hf:governance`, `hf:workforce`, `hf:audit`, `hf:agent-status`)을 같은 Redis 인스턴스에서 발행·소비하게 된다. 여기에 §3.4가 정한 Hot Cache(최신 Quote/Feature), Rate Limit, WebSocket Pub/Sub, Job Lease까지 얹히면 하나의 프로세스가 감당하는 메모리·연결 수가 부서 수만큼 늘어난다는 우려는 근거가 있다.

다만 **지금 시점에는 이 우려를 뒷받침할 실측 데이터가 없다.** Risk·QA 둘만 쓰는 현재 Redis의 실제 메모리 사용량, 8개 부서가 다 붙었을 때의 예상 증가량 어느 쪽도 계측된 적이 없다(`otel-collector`/`prometheus`는 계획 단계, §5 표에 `observability` Profile로만 존재). 이 ADR은 그 데이터 공백을 인정한 채로 대안을 비교한다.

## 3. 대안 비교

### Option A — 현상 유지: 공통 Redis 인스턴스 1개

- **장점**
  - 운영 단위가 하나다. 모니터링, 백업(AOF/RDB 전환 시), 장애 대응 창구가 1곳.
  - §8.3의 8개 Stream이 이미 이름으로 논리 분리돼 있어, 같은 프로세스 안에서도 부서 간 데이터가 섞이지 않는다(물리 분리 없이도 격리 단위는 이미 있다).
  - 부서 간 Event(예: `execution.fill.v1`을 Accounting과 QA가 동시에 구독)가 같은 버스에서 자연스럽게 흐른다 - 지금 §4 Topology의 Fan-out 상당수가 여러 부서에 걸친 Consumer Group이다.
- **단점**
  - Noisy Neighbor: 한 부서(예: 리서치의 Tick Hot Cache)가 메모리·CPU를 과점하면 다른 부서의 Rate Limit·Cooldown 조회 지연에 영향을 줄 수 있다.
  - 장애 반경이 전사다 — Redis 하나가 죽으면 §13 장애표의 "Redis 장애" 행 전체가 동시에 발동한다.
  - 부서별 사용량이 안 보이면 사이징 근거를 세우기 어렵다(지금 상태).

### Option B — 부서별 독립 Redis 인스턴스 8개

- **장점**
  - 메모리·장애 반경이 부서 단위로 격리된다. 한 부서가 Redis를 과점해도 다른 부서에 전파되지 않는다.
  - 부서별 `mem_limit`을 Container 자원 상한과 동일한 방식(CLAUDE.md 개발 원칙과 일관되게 "위험 확대가 아니라 차단")으로 독립적으로 관리할 수 있다.
- **단점**
  - **교차 부서 Stream 처리가 애매해진다.** §4 Topology에서 `EVT --> ACC`, `EVT --> QAA`, `EVT --> GOV` 등 하나의 Producer를 여러 부서가 동시에 구독하는 경로가 다수다. 완전히 분리하면 Producer가 자기 부서 Redis에만 쓰고, 다른 부서가 구독하려면 인스턴스 간 Bridge/Relay가 추가로 필요하다 - 이건 "부서별로 Redis를 쪼갠다"가 아니라 "Redis마다 또 하나의 Event 릴레이 계층을 만든다"가 되어 §3.4가 이미 채택한 "Redis Streams + Transactional Outbox" 구조 위에 새 인프라를 얹는 셈이다.
  - 운영 부담이 8배: Health Check, 메모리 Alert, 재시작 정책, (향후) AOF 백업 대상이 8곳으로 늘어난다.
  - Redis 프로세스 자체의 기본 오버헤드(내부 자료구조, 연결 버퍼)가 인스턴스 수만큼 반복돼, 실제 데이터量이 늘지 않아도 총 메모리 합계는 오히려 늘어날 수 있다(파편화) - "메모리가 너무 커질 것 같다"는 우려의 해결 방향과 반대로 갈 위험.
  - §16 Phase 계획이 이미 "P0는 Redis Streams", "12개 이상 Consumer Group으로 늘면 NATS JetStream을 P1에서 평가"라고 못박아뒀다 - 지금 인스턴스를 쪼개는 결정이 그 P1 재평가 시점의 선택지(NATS 등)와 어떻게 이어질지도 같이 봐야 한다.

### Option C — 계측 우선, 조건부 선택적 분리 (이 ADR이 제안하는 방향)

Option A를 유지하되, 다음을 먼저 한다.

1. `prometheus`/`otel-collector`(§5, 이미 `observability` Profile로 계획됨)로 Redis `INFO memory`, Stream별 `XLEN`/`XINFO GROUPS` Consumer Lag을 부서·Stream 단위로 계측한다.
2. 계측 데이터가 쌓인 뒤, 다음 조건 중 하나를 넘는 부서만 **선택적으로** 별도 Redis(또는 별도 Redis DB Index/ACL User)로 분리한다.
   - 특정 부서(Stream)가 전체 Redis 메모리의 일정 비율(예: 50% 이상)을 지속 점유
   - 메모리 사용량이 현재 상한(`256m`)의 80% 이상을 반복 초과
   - 특정 부서의 장애 격리가 다른 부서의 SLA(예: Risk Pre-trade Check p95)에 실측으로 영향을 준 사례 발생
3. 그 전까지는 §8.3의 Stream 이름 분리(`hf:market`, `hf:case`, ...)와 Redis `maxmemory-policy`/Stream `MAXLEN` 트리밍만으로 논리적 격리를 강화한다.

이 방향은 "부서별 Redis를 쪼갤지"를 지금 결정하지 않고, §16 Phase B1("프로젝트 Redis 추가")이 이미 다음 종료 조건으로 잡아둔 계측 작업과 자연스럽게 이어진다.

## 4. 이 ADR이 제안하는 결정 (승인 대기)

1. **지금은 Option A(공통 Redis 1개)를 유지한다** — 부서별 분리를 뒷받침할 실측 데이터가 없는 상태에서 운영 복잡도만 8배로 늘리는 조기 분리는 하지 않는다.
2. Phase B1(§16.0)의 계측 작업에 "부서·Stream별 Redis 메모리/Lag 계측"을 명시적으로 포함시킨다.
3. Option C의 조건(3절 2번)을 만족하는 부서가 나오면, 그 부서에 한해 별도 ADR로 분리를 재검토한다 - 8개를 한 번에 바꾸지 않는다.
4. 최종 결정은 도현님(공통 Platform 기술 DRI)이 하고, 동규님(현재 유일한 실사용자)이 영향 평가를 리뷰한다.

## 5. 이 제안이 건드리지 않는 것

- Redis를 Canonical Ledger·Audit 원장으로 쓰지 않는다는 기존 원칙(§19)은 그대로다.
- P0 Event Backbone이 Redis Streams라는 결정(§3.4) 자체는 바꾸지 않는다 - 인스턴스 개수만 다루는 ADR이다.
- NATS JetStream 평가 조건(§3.4의 5개 조건)은 이 ADR과 별개로 그대로 유효하다.

## 6. 다음 행동

- [ ] 도현님·동규님 리뷰 및 Status를 `Accepted`/`Rejected`로 확정
- [ ] 승인 시 `DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md` §5, §16.0 Phase B1 항목에 계측 작업 반영
- [ ] 반려 시 이 문서에 반려 사유만 추가하고 Status를 `Rejected`로 변경(삭제하지 않음 - 재논의 시 근거 보존)

## 7. 관련 문서

- [TECH_STACK_DECISIONS.md §5](../TECH_STACK_DECISIONS.md#5-redis-사용-결정)
- [DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md §3.4, §5, §8.3, §16](../DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)
- [ADR-0001](0001-hermes-kanban-agent-status-bridge.md) — 같은 저장소의 ADR 형식 참고
