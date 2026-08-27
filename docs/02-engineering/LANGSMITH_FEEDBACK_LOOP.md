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
| QA Discord `1541636723006775477` | Hermes 검토 + 사람 결정 | redacted artifact 제안, 승인/거부 | 업무 경로와 분리 |
| CEO/department advisory | 다음 요청의 bounded hint | `PASSED` benchmark를 통과한 finding만 대상 Hermes가 비권위 참고 | 실패 시 hint 없음 |

현재의 단일 정식 흐름은 다음과 같다.

```text
First/Metrics metadata
  → background evaluator
  → HgFinance-Evals
  → QA Discord card
  → qa-hermes review
  → authorized human APPROVED / REJECTED
  → benchmark_status=PENDING
  → offline benchmark PASSED
  → LANGSMITH_FEEDBACK_MODE=active인 다음 CEO/대상 부서 Hermes task의 advisory hint
```

`APPROVED`와 `PASSED`는 다른 상태다. `APPROVED`만으로 prompt, router,
model, Hermes 동작은 바뀌지 않는다. `PASSED` 뒤에도 hint는 비권위 참고자료일
뿐이며, 자동 prompt/model 배포는 하지 않는다.

### Evolution Skill 분기

관리자가 1차 승인에서 `SKILL_CREATE` 또는 `SKILL_EVOLVE`로 분류한 artifact만
benchmark PASS 뒤 Evolution bridge로 전달된다. 다른 유형은 기존 feedback 원장에
남고 Skill 후보를 만들지 않는다. Evolution은 별도 원장을 새로 만들지 않고 QA
SQLite의 artifact/decision/benchmark ID를 기존 occurrence provenance에 투영한다.

```text
QA artifact → QA Hermes → 관리자 1차 승인 + 유형
  → redacted baseline-evidence admission benchmark PASS
  → 서로 다른 semantic QA artifact 3건
  → Qwen2.5-14B-Instruct-AWQ 제안
  → 결정론적 구조·경계·provenance 검증
  → Discord exact Skill/provenance hash 2차 승인
  → 비-LLM control worker 정본 승격
  → ACTIVE_PENDING_FEEDBACK
  → 운영 성과 3건 → VERIFIED_IMPROVED / REGRESSION_CANDIDATE
```

이 admission benchmark는 원문 재실행이나 해결책 효과 검증이 아니라 artifact ID,
독립 source lineage, redaction, finding, 담당 귀속을 검사하는 진입 게이트다. 첫 승인,
benchmark PASS, Skill 활성화, 문제 해결 확인은 서로 다른 상태다. 제안
worker는 정본 skills를 읽기만 하며, control worker만 승인된 hash를 재검증한 뒤
쓸 수 있다. `scripts/evolution_skills.py report <proposal-id>`와 내부
`GET /qa/v1/evolution/proposals/{proposal_id}`가 문제 증거, 변경 hash, 승인자,
활성화, 후속 성과를 한 묶음으로 보여준다.

### Internal QA Discord boundary

AI Office에는 feedback artifact, 부서 comment, 승인/반려 UI를 노출하지 않는다.
BFF의 `/ui/**/observability/feedback` 경로도 없다. 브라우저에는 QA LangSmith
집계처럼 read-only한 운영 상태만 남고, decision API는 `audit-api` 내부 서비스
경계에서만 존재한다.

`portfolio-worker`의 기존 evaluator가 actionable artifact마다 QA 봇 identity로
내부 Discord 채널 `1541636723006775477`에 metadata-only card를 한 번 게시한다. 별도 봇이나
두 번째 evaluator는 없다. 정확한 marker와 QA 봇 self identity를 확인한 기존
`qa-hermes` gateway만 그 card를 Agent 검토로 넘긴다. Agent는 조치와 재검증 방법을
제안할 뿐 승인하거나 설정을 바꾸지 않는다.
이 전용 채널은 `QA_DISCORD_FREE_RESPONSE_CHANNELS`에도 등록되어 self-authored
marker가 일반 self-message/mention 규칙 때문에 유실되지 않는다.

현재 Discord guild 구성원은 사람 팀원과 내부 부서 봇뿐이다. QA 봇에는
`Manage Channels` 권한이 없으므로 채널 visibility 정책은 서버 소유자가 관리한다.
외부 멤버를 guild에 추가할 때는 channel overwrite를 사람 팀원,
`HERMES-QA`, `HERMES-CEO`로 제한해야 한다. 요청 marker는 `HERMES-QA` self
identity만 생성할 수 있다. QA 전용 채널의 승인·거부 명령은 `qa-hermes`만
처리하고, CEO를 포함한 다른 프로필은 일반 사용자 질의로 전달하지 않는다.
사람은 QA Agent 응답에 Reply해
`승인 유형=<개선유형> <사유>` / `거부 <사유>`를 쓰거나,
`승인 feedback-... 유형=<개선유형> <사유>` 형식을 쓴다.
artifact ID만 입력하거나 사유를 생략한 결정은 fail-closed로 기록하지 않는다.
게이트웨이는 `QA_DISCORD_APPROVER_USER_IDS` 또는
`QA_DISCORD_APPROVER_ROLE_IDS`의 명시적 allowlist를 확인하며 Discord guild owner도
local administrator로 허용한다. 봇 계정과 미등록 사람은 fail-closed다.
같은 Discord message는 durable inbound ledger로,
같은 artifact decision은 `langsmith_feedback_decisions`의 PK로 중복 적용되지 않는다.
승인 직후 상태는 적용 완료가 아니라 `benchmark_status=PENDING`이다.

카드는 원문 prompt/answer 없이 project, `source_run_id`, correlation ID, 관측 구간,
latency scope, 관측값과 기준값을 증거 참조로 제공한다. End-to-end 지연 카드는
Kanban의 완료된 primary task 실행시간으로 `주요 병목`을 정하고,
`ceo-workflow / observability`를 공동 개선 대상으로 표시한다. `ceo-ingress`는
타이머의 관측 시작 지점일 뿐 원인 부서로 표시하지 않는다. 단계별 실행시간을
읽을 수 없으면 담당을 추측하지 않고 `미확정`으로 남긴다. Metrics 집계의 `metric_count`는
API 호출량으로 과장하지 않고 "집계된 metric trace 수"로 표기한다. QA Hermes는 이
증거만으로 사실과 추론을 분리하고 담당 부서, 한 가지 조치, 재검증 방법을 제안한다.
한 Agent 응답은 triggering message의 artifact 한 건만 다루며 다른 대기 카드를 합치지 않는다.
Discord에는 artifact마다 두 역할만 나타난다. `① 자동 감지 · QA 검토 요청`은
immutable evidence card이고, `② QA Hermes 검토 결과`는 이를 반복하지 않는 구조화된
검토 의견이다. Hermes의 `승인 검토 권고`, `보류 권고`, `거부 검토 권고`는 사람을 위한
비구속 의견이며 실제 결정은 허용된 사람의 명시적 명령만 기록한다.

### Active 전환 runbook

1. `shadow`를 유지한 상태로 자연 trace가 `HgFinance-Evals`에 쌓이는지 확인한다.
2. QA Discord에서 qa-hermes 제안을 검토하고 허용된 관리자가 승인/거부한다.
   승인하면 benchmark candidate가 `PENDING`이 된다.
3. [export script](../../scripts/export_langsmith_feedback_benchmark.py)로
   redacted candidate를 offline benchmark에 넣는다. 원문 prompt/result는 이 경로에 없다.
4. 고정된 benchmark의 `PASSED` 결과만 QA benchmark API로 기록한다.
5. 그 다음에만 CEO가 실행되는 서비스의
   `LANGSMITH_FEEDBACK_MODE=active`를 설정하고 해당 서비스만 recreate한다.
6. 다음 자연 요청의 root와 해당 부서 primary task에 advisory hint가 보이는지 확인한다. `off` 또는 `shadow`로 되돌리면
   즉시 hint가 사라지고 업무 흐름은 그대로다.

최소 서비스 역할은 `portfolio-worker`(단일 평가 daemon/Discord card publisher),
`qa-hermes`(Agent 검토/사람 명령 ingress), `audit-api`(decision/benchmark gate),
`portfolio-bff`와 CEO supervisor(advisory projection)다. AI Office는 승인 경로가 아니다.

### 병목·보존 정책

`First`의 lifecycle root 이름은 `hgfinance.user-query`, 독립 Worker graph는
`worker.<worker_id>`다. Root의 `latency_scope=end_to_end`는 접수부터 최종 사용자
응답 전달까지의 전체 시간이고, Worker의 `latency_scope=worker_execution`은 해당
graph 실행 시간만 뜻한다. 둘을 같은 종류의 모델 latency로 비교하지 않는다.
과거 `parent` 표시는 별도 Worker가 아니라 SDK의 분산-context placeholder를 종료
patch에 재사용하면서 root 이름을 덮어쓴 것이었다. 종료 시에는 run ID와 terminal
metadata만 갱신해 원래 이름과 시작 시각을 보존한다.

긴 root를 삭제하거나 Metrics로 옮겨 P99를 낮추지 않는다. 먼저 Kanban의 root,
primary, synthesis 실행 시간을 나눠 본다. 사용자 질의 planning/response synthesis와
`fast_advisory`에는 task-scoped 12-turn, medium-reasoning 기본 예산을 적용하며,
standard analysis/full experiment와 명시적 per-task override는 그대로 둔다. 단일
primary가 계약된 `final_answer`를 내면 기존 passthrough 경로로 CEO 재종합을 생략한다.

`active` CEO 요청은 LangSmith endpoint를 호출하지 않는다. 30초 local cache가
유효하면 SQLite도 읽지 않고, cache miss 때만 bounded local read를 수행한다. SQLite
잠금/오류/잘못된 데이터는 hint를 버리고 원래 CEO 경로를 계속 실행한다. 따라서
QA 대기나 LangSmith network latency가 CEO, Discord finalization, Kanban, provider를
막지 않는다.

QA trace dashboard도 최근 집계 결과를 짧게 캐시하고, LangSmith rate limit 동안에는
마지막 성공 집계를 반환한다. 첫 조회량을 줄이기 위해 AI Office 기본 범위는 최근
2일이며, rate limit이 발생하고 성공 캐시가 없으면 가짜 수치 대신 `DEGRADED` 상태를
표시한다. 집계 응답의 `cached`는 첫 정상 조회에서 `false`, 캐시 재사용·rate-limit
fallback에서 `true`로 명시되며, `cache_age_seconds`와 `cache_reason`으로 캐시의
경과 시간과 재사용 사유(`ttl`, `rate_limit`, `inflight`, `error`)를 함께 알린다.

유입이 처리량을 넘으면 업무 흐름을 보호하기 위해 pending 상한(기본 500)을 넘는
관측 finding을 버린다. Metrics는 개별 event를 approval queue에 넣지 않고 5분당
최대 1개 Evals 관측으로 축약하며, 같은 finding의 QA artifact는 6시간 incident
bucket 안에서 다시 합친다. local ledger는 기본 30일 후 정리되고, 외부 LangSmith
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
                   optional bounded CEO + department advisory hint
```

The evaluator reads LangSmith run metadata only. It does not fetch `inputs`,
`outputs`, prompts, answers, provider payloads, credentials, or headers. The
published evaluation run has empty inputs and outputs and contains only the
allowlisted correlation, status, latency, error-count, and structured-score
fields.

### Correlation and semantic QA (v1)

Worker graph runs now receive the same bounded correlation envelope at the
invoke boundary: `request_id`, `root_id`, `task_id`, and `trace_id`. Existing
department-specific `case_id`/`artifact.trace_id` values are reused; legacy
callers receive deterministic hash-based fallback IDs. This removes the former
case where the worker result had a context ID but its LangSmith root run had no
request/root link.

At the terminal CEO response boundary, the application evaluates the prompt and
final answer locally with `orchestration.semantic_qa.evaluate_prompt_answer()`.
The v1 contract checks completeness, evidence/grounding, as-of consistency,
uncertainty disclosure, and a conservative prompt/answer relevance signal.
LangSmith receives only the bounded score, verdict, dimension scores, and
finding codes; prompt/answer text remains outside the trace. This is a
semantic-quality contract signal, not a factual-truth judge.
The `semantic_qa_version` and `semantic_qa_evaluator` fields make that scope
explicit so a future model-judge or offline benchmark can be added without
changing the QA approval API.

## Runtime modes

- `off`: no evaluator and no feedback hint.
- `shadow` (default): evaluate asynchronously and publish redacted findings to
  `HgFinance-Evals`; no business behavior changes.
- `active`: the CEO boundary may read only QA-approved and benchmark-passed
  bounded findings from the local ledger. The root carries the advice and the
  supervisor projects only the matching department item into that department's
  next primary Hermes task.

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
observation, and creates at most one corresponding Evals observation per
window. Equivalent findings in the same six-hour UTC incident bucket share one
local QA artifact and one Discord delivery. This keeps high-frequency metrics
useful for QA without turning them into a repeated approval queue.

The local coordination ledger removes expired jobs, redacted artifacts, and
their approval decisions after `LANGSMITH_FEEDBACK_RETENTION_DAYS`. LangSmith
project retention is controlled separately by the workspace retention policy;
the application does not delete external traces automatically.

QA Discord cards are a separate action-inbox retention policy. The existing
`portfolio-worker` runs one bounded pass per day and deletes only cards whose
recorded QA feedback decision is `APPROVED`/`REJECTED` or whose benchmark is
terminal (`PASSED`/`FAILED`) and whose latest terminal transition is older than
seven days. Skill-review cards follow the same seven-day rule after final
`APPROVED`/`REJECTED`. Pending cards are preserved; the local SQLite ledger,
proposal state, provenance, and audit history are not deleted. The pass is
disabled when `LANGSMITH_FEEDBACK_DISCORD_RETENTION_ENABLED=false` and is
bounded by `LANGSMITH_FEEDBACK_DISCORD_RETENTION_MAX_MESSAGES` (default 100).
Discord/API failures are isolated and retried on the next daily pass.

No response is not a rejection. An artifact remains `PENDING` until an allowed
human explicitly enters `승인`, `거부`, or `반려`; an unanswered artifact is
eventually removed by the local retention cleanup without creating a rejection
decision.

## Approval boundary

Internal services retain these audit endpoints:

- `GET /qa/v1/observability/feedback/pending`
- `POST /qa/v1/observability/feedback/{artifact_id}/decision`
- `GET /qa/v1/observability/feedback/approved`
- `GET /qa/v1/observability/feedback/benchmark/candidates`
- `POST /qa/v1/observability/feedback/{artifact_id}/benchmark`

Decisions are append-only and one-shot per artifact. Approval creates a
benchmark candidate but does not deploy a prompt, router, model, or worker
change. Only an explicit offline benchmark `PASSED` result unlocks the bounded
advisory hint in a later active-mode CEO request. Actual changes still require
the existing release gates.

The AI Office and BFF expose none of these decision routes. Discord is the
human ingress; the existing audit API remains the sole decision writer.
과거 frontend self-review용 `department/{department}`와 `department-review`
endpoint, review writer, 빈 legacy review table은 제거됐다. 승인·benchmark 이력은
각각의 단일 ledger table에만 보존한다.

## What this evaluates

The current privacy contract permits deterministic operational evaluation:
terminal status, error count, latency threshold, correlation completeness,
privacy marker, structured `eval_score`, and the redacted semantic QA contract
fields above. It still does not claim factual truth from hidden payloads; that
requires an approved structured score or a separate redacted offline benchmark.
