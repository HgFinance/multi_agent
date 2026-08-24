# LangSmith → QA feedback loop

## Team quick reference (canonical)

이 문서의 용어와 순서가 현재 구현의 기준이다. 예전의 단순
`publish_root_trace()` 경로, LangSmith UI에서 직접 읽어 CEO에 넣는 방식,
QA 승인만으로 런타임을 바꾸는 방식은 현재 경로가 아니다.

| 단계 | 저장 위치 | 역할 | 업무 경로 영향 |
| --- | --- | --- | --- |
| `First` | workflow root/worker trace | 한 요청의 metadata-only 관측 | 없음 |
| `HgFinance-Metrics` | 고빈도 metric trace | 5분 집계의 입력 | 없음 |
| `HgFinance-Evals` | redacted evaluation artifact | QA가 검토할 finding | 없음 |
| local SQLite ledger | shared runtime volume | 중복 제거·lease·QA decision·benchmark gate | 읽기 실패 시 no-op |
| CEO advisory | 다음 요청의 bounded hint | `PASSED` benchmark를 통과한 finding만 비권위 참고 | 실패 시 hint 없음 |

현재의 단일 정식 흐름은 다음과 같다.

```text
First/Metrics metadata
  → background evaluator
  → HgFinance-Evals
  → QA APPROVED
  → benchmark_status=PENDING
  → offline benchmark PASSED
  → LANGSMITH_FEEDBACK_MODE=active인 다음 CEO 요청에만 advisory hint
```

`APPROVED`와 `PASSED`는 다른 상태다. `APPROVED`만으로 prompt, router,
model, Hermes 동작은 바뀌지 않는다. `PASSED` 뒤에도 hint는 비권위 참고자료일
뿐이며, 자동 prompt/model 배포는 하지 않는다.

### Department self-review

각 부서 화면에는 자기 부서의 `department_key`와 일치하는 redacted finding만
표시된다. 부서 의견은 `langsmith_feedback_reviews`에 append-only로 저장되며
`reviewer_user_id`, `reviewer_department`, 대상 부서, 시각, 의견을 남긴다.
이 의견은 QA 승인이나 benchmark를 대신하지 않는다. 즉 부서는 자기 trace에
설명·이슈·수정 방향을 남길 수 있고, QA만 최종 승인/반려와 benchmark gate를
수행한다.

부서별 경로:

- `GET /ui/departments/{department}/observability/feedback`
- `POST /ui/departments/{department}/observability/feedback/{artifact_id}`
- `GET /qa/v1/observability/feedback/department/{department}`
- `POST /qa/v1/observability/feedback/{artifact_id}/department-review`

운영 모드에서는 BFF가 유효한 fund membership을 확인한 사용자만 이 경로를
사용할 수 있다. fixture 모드는 local/test에서만 허용된다.

### Active 전환 runbook

1. `shadow`를 유지한 상태로 자연 trace가 `HgFinance-Evals`에 쌓이는지 확인한다.
2. QA가 UI/API에서 finding을 승인한다. 그러면 benchmark candidate가 `PENDING`이 된다.
3. [export script](../../scripts/export_langsmith_feedback_benchmark.py)로
   redacted candidate를 offline benchmark에 넣는다. 원문 prompt/result는 이 경로에 없다.
4. 고정된 benchmark의 `PASSED` 결과만 QA benchmark API로 기록한다.
5. 그 다음에만 CEO가 실행되는 서비스의
   `LANGSMITH_FEEDBACK_MODE=active`를 설정하고 해당 서비스만 recreate한다.
6. 다음 자연 요청에서 advisory hint가 보이는지 확인한다. `off` 또는 `shadow`로 되돌리면
   즉시 hint가 사라지고 업무 흐름은 그대로다.

최소 서비스 역할은 `portfolio-worker`(평가 daemon), `audit-api`(QA decision/benchmark
gate), `portfolio-bff`(CEO advisory read), AI Office(검토 UI)다. 설정을 한 번에 전체
compose에 복사하지 말고 역할별로 주입한다. 특히 `active`는 BFF에 필요하고,
worker는 `shadow`로 계속 평가를 수행해도 된다.

### 병목·보존 정책

`active` CEO 요청은 LangSmith endpoint를 호출하지 않는다. 30초 local cache가
유효하면 SQLite도 읽지 않고, cache miss 때만 bounded local read를 수행한다. SQLite
잠금/오류/잘못된 데이터는 hint를 버리고 원래 CEO 경로를 계속 실행한다. 따라서
QA 대기나 LangSmith network latency가 CEO, Discord finalization, Kanban, provider를
막지 않는다.

QA trace dashboard도 최근 집계 결과를 짧게 캐시하고, LangSmith rate limit 동안에는
마지막 성공 집계를 반환한다. 첫 조회량을 줄이기 위해 AI Office 기본 범위는 최근
2일이며, rate limit이 발생하고 성공 캐시가 없으면 가짜 수치 대신 `DEGRADED` 상태를
표시한다.

유입이 처리량을 넘으면 업무 흐름을 보호하기 위해 pending 상한(기본 500)을 넘는
관측 finding을 버린다. Metrics는 개별 event를 approval queue에 넣지 않고 5분당
최대 1개 artifact로 축약한다. local ledger는 기본 30일 후 정리되고, 외부 LangSmith
trace 보존은 workspace retention 정책으로 별도 관리된다.

### Legacy compatibility

- `orchestration.llm_observability.publish_root_trace()`는 기존 호출자/테스트를 위한
  standalone metadata metric 호환 함수다. 현재 BFF user-query의 lifecycle root는
  `start_root_trace()`와 terminal close 경계를 사용한다.
- `apps/api/langsmith_traces.py`는 read-only QA timeseries 조회기다. feedback evaluator,
  approval ledger, CEO advisory source가 아니다.
- 새 producer는 `First`/`HgFinance-Metrics`/`HgFinance-Evals`의 역할을 섞지 않는다.
  `LANGCHAIN_*` 같은 legacy 환경변수나 임의 project 이름을 새 경로에 추가하지 않는다.

## Contract

```text
First (workflow metadata) ─┐
                           ├─ bounded background evaluator ─→ HgFinance-Evals
Metrics (high-frequency) ──┘             │
                                        ↓
                              QA pending-review ledger
                                        ↓
                              QA APPROVED / REJECTED
                                        ↓
                              offline benchmark gate
                                         ↓
                         optional bounded CEO advisory hint
```

The evaluator reads LangSmith run metadata only. It does not fetch `inputs`,
`outputs`, prompts, answers, provider payloads, credentials, or headers. The
published evaluation run has empty inputs and outputs and contains only the
allowlisted correlation, status, latency, error-count, and structured-score
fields.

## Runtime modes

- `off`: no evaluator and no feedback hint.
- `shadow` (default): evaluate asynchronously and publish redacted findings to
  `HgFinance-Evals`; no business behavior changes.
- `active`: the CEO boundary may read only QA-approved bounded findings from
  the local ledger and place them in the root body as non-authoritative advice.

The active hint is never read from LangSmith on the CEO hot path. It is a
short-lived local cache backed by the shared feedback ledger. A missing, locked,
or malformed ledger returns no hint.

## Backpressure and retention

The worker uses a bounded lookback, idempotent `source_run_id`, a bounded batch,
one background evaluator loop, SQLite WAL mode, and a maximum pending-job cap.
Duplicate polls do not create duplicate evaluation artifacts. A LangSmith
outage retries with bounded backoff and never affects the business worker.

`HgFinance-Metrics` is not evaluated one run at a time. The worker closes one
configurable metric window (default five minutes), reduces at most
`LANGSMITH_FEEDBACK_METRICS_MAX_RUNS` records to a single p95/error/count
observation, and creates at most one corresponding Evals artifact per window.
This keeps high-frequency metrics useful for QA without turning them into a
per-event approval queue.

The local coordination ledger removes expired jobs, redacted artifacts, and
their approval decisions after `LANGSMITH_FEEDBACK_RETENTION_DAYS`. LangSmith
project retention is controlled separately by the workspace retention policy;
the application does not delete external traces automatically.

## Approval boundary

QA reviews redacted findings through:

- `GET /qa/v1/observability/feedback/pending`
- `GET /qa/v1/observability/feedback/department/{department}`
- `POST /qa/v1/observability/feedback/{artifact_id}/department-review`
- `POST /qa/v1/observability/feedback/{artifact_id}/decision`
- `GET /qa/v1/observability/feedback/approved`
- `GET /qa/v1/observability/feedback/benchmark/candidates`
- `POST /qa/v1/observability/feedback/{artifact_id}/benchmark`

Decisions are append-only and one-shot per artifact. Approval creates a
benchmark candidate but does not deploy a prompt, router, model, or worker
change. Only an explicit offline benchmark `PASSED` result unlocks the bounded
advisory hint in a later active-mode CEO request. Actual changes still require
the existing release gates.

## What this evaluates

The current privacy contract permits deterministic operational evaluation:
terminal status, error count, latency threshold, correlation completeness,
privacy marker, and an already-emitted structured `eval_score`. It intentionally
does not claim semantic answer quality from hidden payloads. Semantic QA must
emit an approved structured score or be evaluated in a separate redacted
benchmark path.
