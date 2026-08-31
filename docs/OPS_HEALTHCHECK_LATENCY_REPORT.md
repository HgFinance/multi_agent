# 운영 연결·헬스체크·지연도 점검 보고서

- 점검일: 2026-08-30 UTC
- 범위: 전체 Compose 런타임, Hermes, LangSmith 유지보수 작업, Discord notifier, Research/Risk MCP, 회계 원장 소비자
- 목적: 연결 단절과 반복 작업으로 인한 병목을 확인하고, PAPER 주문·fail-closed 안전 동작을 유지한 채 지연과 로그량을 줄이는 것

## 현재 판정

현재 Compose 런타임은 57개 서비스가 모두 `running`이며 `healthy`이다. 기본 Compose와 모델 오버레이 모두 `docker compose config` 검증을 통과했다. 기존 모델 컨테이너는 고아 컨테이너 경고만 발생했으며, 데이터 삭제를 피하기 위해 자동 제거하지 않았다.

이번 변경은 PAPER 주문을 끄지 않았고, 가격이 없거나 낡은 경우 NAV를 계산하지 않는 fail-closed 정책도 변경하지 않았다.

## 최종 워크플로 재검증: 2026-08-30 16:33 UTC

최종 배포본은 결정론적 BFF의 이미 확정된 route라도 root lifecycle 안전성을
위해 기존 CEO root worker 경계를 유지한다. 대신 Research fast-advisory의
discovery 호출과 CEO의 중복 LLM synthesis만 제거했다. 따라서 아래 E2E는 root → Research primary → 단일
CEO synthesis → 비동기 QA graph가 모두 존재하는 정상 완료만 PASS로 집계했다.

### Research 사용자-query E2E

명령:

```bash
python scripts/stress_test.py --scenario ceo_readonly_e2e --requests 1 \
  --concurrency 1 --base-url http://127.0.0.1:8001 \
  --e2e-query '리서치 부서에서 삼성전자 최근 사업 방향을 공식 자료와 뉴스로 검토해줘. 투자 추천과 주문은 하지 마.' \
  --allow-workflow --poll-interval 1 --workflow-timeout 240 --timeout 45 \
  --include-samples
```

최신 유효 실행 `t_c547d1d1`은 기능 PASS, 오류 0, terminal `completed`,
총 108.050초, server workflow 94초였다. graph 감사에서 root, Research
primary(`67초`), 단일 synthesis(`3초`), async QA가 확인됐고, supervisor 로그는
`research-primary-template`을 사용했음을 확인했다. 기존 기준선 157.929초 대비
약 49.9초를 줄였으며, 120초 SLA에 11.95초 여유가 있다. 단일 표본이므로
이 E2E의 p95는 통계적 p95가 아니라 해당 표본의 관측값이다.

Research의 남은 실행 시간은 DB readiness가 아니라 외부 source connector와
`gpt-5.6-luna` 모델 왕복이다. fast-advisory에는 Research/kanban 도구만 남겼고
`tool_describe` 같은 discovery를 금지했으며, 근거가 준비되면 기존 Research
답변을 CEO가 다시 쓰지 않고 canonical synthesis 카드에 그대로 전달한다.
근거가 없을 때도 `evidence_status=unverified`와 명시적 제한 문구가 있는 경우만
동일 template을 사용하므로, 근거 없는 답변을 PASS로 포장하지 않는다.

### 전체 부서 read-only 동시성: 최종 32-way 재검증, 16:50 UTC

`python scripts/stress_test.py --scenario read_only --requests 32 --concurrency 32`
최종 실행은 9개 시나리오 모두 32/32, HTTP 200, 오류 0, `stress_failure=false`였다.

| 시나리오 | 성공 | p50 / p95 / p99 (ms) | 판정 |
|---|---:|---:|---|
| Research readiness | 32/32 | 312.722 / 357.823 / 366.018 | PASS |
| Market deep readiness | 32/32 | 14.334 / 17.243 / 18.317 | PASS |
| Quant health | 32/32 | 17.929 / 20.971 / 23.226 | PASS |
| Risk observability | 32/32 | 20.068 / 25.553 / 26.659 | PASS |
| QA Audit readiness | 32/32 | 11.792 / 13.692 / 13.784 | PASS |
| Trading readiness | 32/32 | 6.551 / 28.871 / 32.618 | PASS |
| Accounting readiness | 32/32 | 21.946 / 26.605 / 27.553 | PASS |
| Governance readiness | 32/32 | 10.118 / 15.022 / 15.731 | PASS |
| Workforce readiness | 32/32 | 17.256 / 20.451 / 21.116 | PASS |

따라서 현재 서비스 read-only 경로에는 Risk/Audit pool exhaustion 병목이 재현되지
않는다. 장애 주입은 하지 않았으므로 recovery는 `NOT_INJECTED`이며, 이것을 장애복구
인증으로 해석하지 않는다.

운영 상태 질의는 별도 결정론적 read-only route로 23.204초, server workflow
4초에 기능 PASS했다. 이 경로는 Research/Risk LLM primary를 호출하지 않는다.

## 이전 기준선 및 세부 병목 분석: 2026-08-30 병목 수정 전후

아래 결과는 최종 재검증 전 기준선이다. Risk/Audit는 소스에 있던
single-flight cache가 이전 이미지에 반영되지 않은 상태가 원인이었으므로 해당
이미지를 재빌드·재기동했다. Research는 readiness에서 상세 집계를 분리하고,
Research/Quant도 동일한 canonical `orchestration/readiness_cache.py`를 사용하도록
재배포했다. Market deep probe의 성공 cache TTL은 15초(healthcheck 주기와 정렬)로
두었다.

`python3 scripts/stress_test.py --scenario all --requests 32 --concurrency 32`를
실행해 10개 시나리오를 각각 동시성 32로 호출했다. CEO 사용자 E2E 시나리오는
실제 작업 생성을 방지하기 위해 query가 없으면 SKIPPED이며, 나머지 9개는
read-only HTTP probe다.

| 시나리오 | 성공 | p50 / p95 / p99 (ms) | 처리량/s | 오류율 | 판정 |
|---|---:|---:|---:|---:|---|
| Research readiness (cold) | 32/32 | 291.986 / 362.539 / 372.592 | 측정별 상이 | 0% | PASS |
| Market deep readiness (cold) | 32/32 | 12.564 / 15.756 / 16.326 | 측정별 상이 | 0% | PASS |
| Quant health | 32/32 | 22.245 / 23.751 / 24.045 | 측정별 상이 | 0% | PASS |
| Risk observability | 32/32 | 28.006 / 31.701 / 32.439 | 측정별 상이 | 0% | PASS |
| Audit readiness | 32/32 | 38.691 / 44.572 / 46.885 | 측정별 상이 | 0% | PASS |
| Trading readiness | 32/32 | 27.437 / 100.546 / 101.781 | 측정별 상이 | 0% | PASS |
| Accounting readiness | 32/32 | 12.196 / 14.636 / 14.956 | 측정별 상이 | 0% | PASS |
| Governance readiness | 32/32 | 9.129 / 11.220 / 11.495 | 측정별 상이 | 0% | PASS |
| Workforce readiness | 32/32 | 18.985 / 22.352 / 22.443 | 측정별 상이 | 0% | PASS |

Research `/health/ready`는 이제 relation 존재·SELECT 권한을 확인하는 bounded
probe만 수행하고, exact count를 포함한 상세 진단은 `/health`로 분리해 30초간
cache한다. 컨테이너 내부에서 readiness SQL 자체는 약 0.2–1ms로 측정됐다.
그럼에도 위 32-way 결과의 Research p95는 약 359ms였다. 동일 endpoint를 순차
호출하면 약 2ms이므로 이 차이는 DB 집계 지연으로 단정할 수 없고, 현재 runner의
32개 신규 TCP 연결 fan-out 비용을 포함한 transport/client 지연으로 분리 기록한다.
Quant는 기존 매 요청 `select 1` 연결 획득을 5초 성공 cache로 줄였고,
Risk/Audit는 blocking connection acquisition으로 pool exhaustion 재발을 막았다.

이 실행은 장애를 주입하지 않았으므로 모든 행의 `recovery_result`는
`NOT_INJECTED`다. 따라서 10개 PDF 시나리오의 운영 stress certification이나
장애복구 PASS로 해석하지 않는다.

기준선의 실제 CEO 사용자-query E2E도 별도로 실행했다. `t_100f5be4`는 상태 조회 오류 없이
최종 `result` HTTP 200과 terminal `completed`를 반환했지만, 전체 E2E는
157,929ms, 서버 workflow는 약 128,000ms로 120,000ms SLA를 초과해 `FAIL`이다.
Primary 작업은 Risk 약 84초, Research 약 91초가 걸렸고 이후 synthesis와 전달이
추가됐다. 따라서 이 경로의 첫 최적화 대상은 DB readiness가 아니라 부서 Hermes
worker/model 실행 시간이다. 장애 주입은 하지 않았으므로 recovery는 여전히
`NOT_INJECTED`다.

## 별도 헬스체크

기존 체크가 없던 서비스에 서비스 성격에 맞는 체크를 추가했다.

- HTTP API: accounting, quant, MCP 보조 API 등은 loopback health endpoint 또는 포트 확인
- 워커/스케줄러: PID·필수 스크립트·상태 디렉터리·Kanban DB 확인
- 데이터 경로: trading outbox relay는 read-only DB 연결과 Redis `PING` 확인
- 모델 워커: registry와 state directory 확인
- MCP: `/mcp` GET을 임의로 호출하지 않고 서버의 명시적 `--healthcheck`를 사용

설정 검증 결과 root Compose 51개 서비스의 healthcheck 누락은 0개이며, 모델 오버레이의 Evolution worker 2개도 healthy이다.

## 확인된 병목과 조치

### CEO mirror projection

Kanban 전체 snapshot이 약 1,175행·25MB로 커진 상태에서 snapshot 실패 시 workflow별 `kanban show`를 무제한으로 반복하는 fallback이 CPU를 점유했다. 관측 당시 mirror worker CPU가 약 100%까지 올라갔다.

조치 내용:

- snapshot 실패 시 per-workflow fallback을 제거하고 15초 bounded retry로 전환
- watermark가 변하지 않는 0건 projection은 full reconcile 주기까지 반복하지 않도록 no-op fence 추가
- 새 watermark 또는 full reconcile 시에는 정상적으로 다시 확인

재배포 후 mirror worker는 반복 subprocess 없이 동작했고 CPU는 측정 시 0% 수준으로 내려왔다. 기존의 fail-closed projection 의미와 eventual retry는 유지된다.

### 회계 원장 NAV 로그

낡은 가격 때문에 NAV 계산을 보류하는 것은 안전상 필요한 동작이다. 다만 같은 book/reason을 매초 INFO로 반복할 필요는 없으므로 동일 상태는 최초 1회와 60초 요약만 기록하도록 억제했다. 정상 valuation이 되면 억제 상태를 초기화한다.

새 컨테이너에서 90초 동안 동일 `NAV 보류` 로그는 1회였으며, NAV 보류 자체를 숨기거나 주문 안전 조건을 완화하지 않았다.

### Market API 상세 readiness

`market-api /health/ready`는 거래 경로가 아닌 사람용 상세 집계 endpoint였지만, 호출마다 대용량 hypertable의 exact count를 다시 계산해 측정 p50 약 634ms, p95 약 2.9초, 일부 5초 timeout을 보였다. 응답 형식과 exact count 의미는 유지하고 30초 single-flight cache를 추가했다. `/ready` orchestration probe와 `/snapshot`, `/bars` 시세 경로는 변경하지 않았다.

### Market deep readiness 동시성

`market-api /ready`는 control DB의 심볼 권위 확인과 TimescaleDB 관계 확인을 매 요청마다
각각 새 연결로 수행하고 있었다. 32개 동시 probe에서 연결·statement timeout으로 31건이
503이 되는 재현이 있었다.

조치 내용:

- 성공한 deep readiness만 기본 2초 동안 single-flight cache로 공유
- cache miss는 한 요청만 DB를 확인하고 동시 호출자는 같은 결과를 기다림
- 실패 결과는 cache하지 않아 복구가 다음 probe에 즉시 반영됨
- 기존 두 DB의 read-only·3초 statement timeout과 503 fail-closed 계약은 유지

재배포 후 32개 동시 호출은 cold probe에서도 32/32가 200이었다(p95 약 362ms). 한 번
warm된 뒤에는 32/32가 200, p95 약 83ms였다.

### Trading readiness pool 경합

`trading-api /health/ready`가 `list_orders()`를 호출해 최대 200건의 order/fill을 hydrate하고
있어, 동시 health probe에서 `ThreadedConnectionPool` 고갈로 500이 발생했다.

조치 내용:

- `execution.order_intents`와 `execution.orders`의 count만 읽는
  `PostgresOrderStore.readiness_counts()`를 추가
- 성공 결과를 기본 2초 single-flight cache로 공유
- 저장소 예외는 500이 아니라 구조화된 503 `TRADING_PAPER_DB_UNAVAILABLE`로 fail-closed

주문 제출·브로커 이벤트·PAPER 체결 경로는 변경하지 않았다. 재배포 후 32개 동시
readiness는 32/32가 200이고 warm p95 약 13ms였다.

### Accounting readiness pool 경합

`accounting-api /health/ready`도 매 요청마다 `_repo.counts()`를 실행해 32개 동시 호출에서
pool exhaustion 500을 재현했다.

조치 내용:

- 성공한 원장 count 결과만 기본 2초 single-flight cache로 공유
- `counts()` 예외는 구조화된 503 `ACCOUNTING_STORE_UNAVAILABLE`과 `HOLD` action으로
  반환하며 성공으로 위장하지 않음
- OFFLINE fixture 동작과 PAPER_DB durable 모드를 모두 유지

재배포 후 32개 동시 readiness는 32/32가 200이고 warm p95 약 14ms였다.

### Risk/Audit readiness pool 경합

기준선에서는 Risk가 10/32, QA Audit이 6/32만 성공했다. Risk는 pool exhaustion에
따른 500, Audit은 같은 경합을 fail-closed 503으로 반환했으며, 단일 요청 p95는
각각 약 12.83ms와 1.71ms였다. 계산 지연이 아니라 readiness probe가 동시에
각각 DB 연결을 확보하려던 구조가 원인이었다.

조치 내용:

- `orchestration/readiness_cache.py`의 성공 결과 전용 single-flight TTL을
  Research/Market/Trading/Accounting/Risk/QA의 readiness 경로가 공통으로 사용
- Risk lazy repository 생성에 초기화 lock을 추가해 동시 요청의 중복 pool 생성을 제거
- Risk와 QA의 pool 연결 획득 실패를 기존 persistence error/503 경계로 정규화
- 실패 결과는 cache하지 않아 복구가 다음 probe에 반영되도록 유지
- QA의 legacy 문서/임베딩 write 경로와 전용 플래그를 삭제하고 request-time MCP 및
  Research 소유 read-only evidence 경계만 유지

재배포 후 동일한 32-way raw HTTP smoke는 Risk 32/32 200(p50 13.80ms, p95
19.20ms, p99 19.91ms), QA Audit 32/32 200(p50 16.18ms, p95 21.08ms, p99
22.09ms)이었다. 이 결과는 pool exhaustion 재발이 없음을 확인한 bounded smoke이며,
전체 stress certification을 의미하지 않는다.

## 부서별 현재 readiness 지연 측정

2026-08-30 UTC에 각 부서의 대표 read-only endpoint를 측정했다. 순차
측정은 16회, 동시 측정은 32회이며, 아래 동시 p95/p99는 실제 서비스에 32개
요청을 동시에 보낸 결과다. 사용자 쿼리 POST나 주문 제출은 호출하지 않았다.

| 부서·대표 서비스 | 동시 성공 | 순차 p50/p95 (ms) | 동시 p50/p95/p99 (ms) | 판정 |
|---|---:|---:|---:|---|
| CEO·portfolio-bff | 32/32 | 2.36 / 4.29 | 46.71 / 52.57 / 52.84 | 정상 |
| Research·research-api | 32/32 | 별도 측정 | 291.99 / 362.54 / 372.59 | 신규 TCP fan-out 포함 |
| Research·market-api | 32/32 | 별도 측정 | 12.56 / 15.76 / 16.33 | 수정 후 정상 |
| Trading·trading-api | 32/32 | 별도 측정 | 27.44 / 100.55 / 101.78 | 일시적 cold fan-out |
| Risk·risk-api | 32/32 | 별도 측정 | 28.01 / 31.70 / 32.44 | pool 수정 후 정상 |
| Quant·quant-api | 32/32 | 별도 측정 | 22.25 / 23.75 / 24.05 | 수정 후 정상 |
| Accounting·accounting-api | 32/32 | 별도 측정 | 12.20 / 14.64 / 14.96 | 정상 |
| QA·audit-api | 32/32 | 별도 측정 | 38.69 / 44.57 / 46.89 | pool 수정 후 정상 |
| QA·governance-api | 32/32 | 별도 측정 | 9.13 / 11.22 / 11.50 | 정상 |
| Workforce·workforce-api | 32/32 | 별도 측정 | 18.99 / 22.35 / 22.44 | 정상 |

Risk와 Audit은 기준선에서는 단일 요청과 32-way 결과의 차이가 컸으나, 위 조치
후에는 pool 상한을 임의로 늘리거나 요청을 무제한으로 받지 않고 성공 probe를 짧게 공유해
동시 DB 획득 자체를 줄였다. persistence 오류와 fail-closed 503 계약은 그대로
유지한다.

이 표는 32회 smoke 측정이지 PDF의 전체 stress certification은 아니다. 워커·Hermes
처럼 외부 HTTP 포트가 없는 서비스는 Compose healthcheck 실행시간과 별도 업무
처리 latency를 분리해서 측정해야 한다.

Compose healthcheck 최근 1회 실행시간도 보조 지표로 확인했다. 모든 대상은 현재
healthy이고 최근 check exit code는 0이었지만, 이는 업무 요청 latency가 아니다.

| 부서 | healthcheck 대상 수 | 최근 실행시간 범위 |
|---|---:|---:|
| CEO | 4 | 37.93–1,587.47ms |
| Research | 9 | 38.98–386.28ms |
| Trading | 9 | 39.97–7,698.36ms |
| Risk | 3 | 39.48–1,083.92ms |
| Quant | 3 | 41.36–507.00ms |
| Accounting | 7 | 42.76–2,276.02ms |
| QA/Audit | 7 | 33.44–960.83ms |
| Workforce | 3 | 40.94–209.57ms |

Trading의 conditional outbox/retention healthcheck가 약 7.4–7.7초, Accounting
close scheduler가 약 2.28초로 가장 길다. 현재 healthy이므로 즉시 기능 오류는
아니지만, healthcheck timeout·DB pool 점유가 실제 업무 처리와 겹치는지 다음
측정에서 확인할 병목 후보다.

## 외부·부서 연결 점검

Evolution 상태 조회 결과(2026-08-30 14:46 UTC)는
`active_skills=1`, `candidate_count=1`, `proposal_count=1`, `occurrence_count=4`다.
후보는 서로 다른 3개 `qa-benchmark` artifact를 바탕으로 만들어졌지만, 연결된
proposal의 상태가 `REJECTED`, `qa_verdict=FAIL`이므로 운영 registry로 승격하지
않았다. 현재 active project skill은 `mandate-dynamic-risk-controls` 하나이고,
canonical registry 검증은 통과했다. 따라서 `conditional-paper-evolution-e2e`의
`qa-core-e2e` 승인과 이 후보는 모두 운영 Skill 승인/승격의 근거가 아니다.

2026-08-30 12:10 UTC fail-closed production preflight도 `BLOCKED`였다
(`external_writes=false`). `RISK_QA_RESEARCH_PACKET_URL`, production QA
persistence/analytics/context/broker 설정, Postgres·Redis·Ollama probe가
준비되지 않아 운영 QA 근거를 생성하거나 skill을 승격하지 않았다.

- Hermes 9개 주요 서비스: 최근 6시간 `ERROR`, traceback, timeout, 인증 오류 0건
- Research MCP, Research liaison MCP, Risk MCP, Paper Order MCP: 명시적 healthcheck 모두 성공
- Discord notifier: 최근 6시간 오류·인증·timeout 0건
- LangSmith: 최근 6시간 runs query 성공 120건, HTTP/auth/timeout 오류 0건. LangSmith 연결은 유지했고 비활성화하지 않았다.
- Notion: 현재 Compose에 별도 Notion runtime 서비스가 없고 최근 6시간 Notion 관련 런타임 로그가 확인되지 않았다. 따라서 연결 성공을 추정하지 않고, 별도 증거 없음으로 기록한다.
- Trading API: 최근 로그의 readiness 응답은 200이며 실제 500 응답은 확인되지 않았다.

MCP 로그에서 과거 관측된 406은 MCP protocol의 `Accept` 헤더가 맞지 않는 GET probe로 판단된다. 유효한 MCP healthcheck와 서버 프로세스는 정상이며, 해당 로그를 없애기 위해 MCP 기능이나 인증을 제거하지 않았다.

## 검증 증거

- CEO mirror unit tests: 5 passed
- maintenance retention tests: 6 passed
- Ledger Consumer self-check: 8개 점검 통과
- Mark Provider self-check: 11개 영역 점검 통과
- readiness/market 조회 동시 smoke: 6개 endpoint에 각 32건, 전부 200
  - cold: market `/ready` p95 362ms, market 상세 `/health/ready` p95 1.82s,
    market snapshot p95 164ms, bars p95 116ms, trading readiness p95 26ms,
    accounting readiness p95 22ms
- warm: market `/ready` p95 83ms, market 상세 `/health/ready` p95 19ms,
    trading readiness p95 13ms, accounting readiness p95 14ms
- 최신 32-way raw HTTP smoke: Research `/health/ready`, Market `/ready`,
  Quant/Risk/Audit/Trading/Accounting/Governance/Workforce의 read-only 경로가
  모두 32/32 200. 위 표의 p95는 신규 연결 fan-out을 포함한 최신 측정값이다.
- Risk runtime observability와 QA Audit readiness post-deploy 32-way raw HTTP smoke:
  각각 32/32 200, p95 19.20ms와 21.08ms
- 전체 격리 E2E: 45 passed
- 전체 pytest: 3,735 passed, 31 skipped, 239 subtests passed, 2 warnings
- Risk/QA pool 경계·single-flight 회귀 및 조건부 Evolution 영향 테스트: 통과
- `git diff --check`: 통과
- root/model Compose config check: 통과

## 전체 pytest 감사 결과

2026-08-30 UTC 최신 전체 실행 결과는 `3,735 passed, 31 skipped, 239 subtests
passed, 2 warnings`였다. 경고는 외부 라이브러리 deprecation 2건이며 실패가
아니다.

전체 pytest PASS는 코드 회귀 증거다. 사용자 query-to-result runtime, 장애 주입·복구,
rollback, image/OS CVE scan은 별도의 운영 게이트이므로 pytest PASS만으로 릴리스
승격하지 않는다.

## 아직 “전체 스트레스 테스트 통과”라고 판정할 수 없는 항목

전용 bounded runner와 수동 CI job은 추가됐다. `scripts/stress_test.py`가 시나리오별
부하량(기본 32), 동시성(기본 32), 실행시간, p50/p95/p99, 처리량, 오류율,
SLA 판정을 JSON artifact로 남기며 `.github/workflows/stress-evidence.yml`에서
재실행할 수 있다. 사용자 E2E는 `--e2e-query`와 `--allow-workflow`를 함께 줘야
실행되며, terminal status 뒤 `GET /ui/ceo/tasks/{id}/result`까지 측정한다.

남은 릴리스 게이트는 120초 이내의 사용자 E2E runtime 샘플과 장애 주입·복구·
rollback 결과, 그리고 별도 Container image/OS scan 결과다. 이번 로컬
read-only matrix의 PASS를 전체 stress certification으로 표현하지 않는다.

## 변경 파일과 되돌리기

핵심 최적화 변경은 `apps/api/ceo_mirror_projection_worker.py`,
`departments/01-research/api/market_api.py`, `departments/02-trading/api/app.py`,
`departments/02-trading/oms/store_postgres.py`,
`departments/05-accounting-portfolio/api/app.py`,
`departments/05-accounting-portfolio/ledger/consumer.py`,
`orchestration/readiness_cache.py`, Risk/QA repository·API와 관련 테스트·Compose 설정에
있다. QA의 legacy evidence ingestion 모듈과 전용 환경변수는 제거했다. 현재 작업 트리는
기존 기능 개발 변경도 함께 포함한 dirty 상태이므로, 되돌릴 때는
파일 전체를 무차별 복원하지 말고 해당 변경을 별도 커밋 단위로 분리한 뒤 검증된 이전
이미지 또는 해당 커밋으로 rollback해야 한다.

## 2026-08-30 CPU/DB read-path 진단 및 단일 최적화

이번 단계는 CPU 제한·polling·vLLM·조건주문·PAPER/Risk 실행 경로를 변경하지 않고,
읽기 전용 진단 후 DB 인덱스 한 건만 적용했다.

- 대상: `api.portfolio_snapshot_latest`의 `book_id` 조회
- 진단: 216,390행을 병렬 순차 스캔하고 외부 정렬했으며, `EXPLAIN ANALYZE` 618.642ms,
  temp read/write 29,513/29,520 blocks였다. 기존 `fund_id` 선두 PIT 인덱스의 사용 횟수는
  0이었다.
- 적용: `accounting_portfolio_snapshots_book_pit_idx`
  (`book_id, fund_id, as_of DESC, created_at DESC`)를 `CREATE INDEX CONCURRENTLY`로 생성했다.
- 검증: 인덱스 유효/준비 상태 true, 크기 14MB, 동일 `EXPLAIN ANALYZE` 248.334ms,
  temp write 없음. 실제 7회 read-only 조회는 170–230ms였고, 핵심 서비스는 재시작 0,
  OOM 0, health 정상이었다.
- 범위: 위 수치는 DB read path 관측이며 사용자 query-to-result 전체 E2E p50/p95/p99가
  아니다. 전체 E2E 성능 개선으로 확대 해석하지 않는다.
- 재현 migration: `supabase/migrations/20260830000100_accounting_portfolio_snapshot_book_read_path.sql`
- 롤백: 회귀가 확인될 때에만 `DROP INDEX CONCURRENTLY IF EXISTS
  accounting.accounting_portfolio_snapshots_book_pit_idx`를 실행하고, 동일 조회 계획·오류·
  health를 재검증한다. migration 파일은 삭제하지 않고 이전 배포와 함께 롤백한다.

진단 중 인덱스 크기 출력용 보조 쿼리에서 스키마 한정자를 빠뜨려 DB ERROR 로그 1건이
발생했으나, 데이터 변경·서비스 재시작·OOM은 없었다. 이후 보정 쿼리는 성공했다.

## 2026-08-30 Audit idle-claim 최적화

Audit 재현 큐는 작업·요청·결과가 모두 0건인데도 15초마다 무결성 조인 기반 claim
함수를 호출하고 있었다. 누적 36,867회, 평균 37.9ms로 확인되어 idle 경로를 별도로
최적화했다.

- 직접 테이블 pre-check는 `svc_qa_reproducer`의 직접 SELECT 권한을 침해하므로
  적용하지 않고 즉시 롤백했다. 이 시도에서 worker error가 발생했지만 데이터 변경은
  없었고, 이전 이미지로 worker를 복구한 뒤 최종 방식만 적용했다.
- 최종 변경은 `audit.has_intraday_forward_reproduction_work()` SECURITY DEFINER
  read-only helper와 worker 호출부다. `svc_qa_reproducer`의 직접 테이블 SELECT는
  계속 false이고 helper EXECUTE만 true다.
- 빈 큐 helper는 평균 약 0.57ms로 관측됐다. 최종 35초 동안 idle worker의 무거운
  claim 호출은 0회 증가했고, 별도 Docker healthcheck의 claim 검증 1회만 실행됐다.
- 기존 작업이 있을 때는 pre-check가 true가 되어 기존 claim/lease/complete/fail 경로를
  그대로 사용한다. 관련 worker 테스트 14개와 스키마 계약 테스트 49개(157 subtests)가
  통과했다.
- 재현 migration: `supabase/migrations/20260830000200_audit_reproduction_empty_queue_probe.sql`
- 롤백: helper migration을 역순으로 되돌리고 `qa-reproduction-worker`를 이전 이미지로
  재배포한 뒤, helper 권한·claim 호출·worker health를 다시 확인한다. 조건주문, PAPER,
  Risk, vLLM 경로는 변경하지 않았다.

## 2026-08-30 Market census 후속 판정

- `conditional-rule-outbox-relay`의 no-op cycle 로그는 이미 기존 코드에서 DEBUG로
  처리되어 있었다. 실제 이벤트·실패 cycle은 INFO로 남기므로, 중복 로그 기능을 추가하지
  않았다. 관련 테스트 3개가 통과했다.
- 누적 지연이 큰 Market 쿼리는 `bottleneck_census`의 10년치 데이터 품질 분석이다.
  26회 평균 16.9초였고, 2,696종목·5,801,165봉을 556개 Timescale chunk에서
  `lag()` 계산한다. 이는 사용자 주문/E2E 경로가 아니다.
- 부모 hypertable 통계가 비어 있어 `ANALYZE market.market_bars`를 한 번 실행했다.
  데이터 행 수는 5,801,165개로 유지됐고 서비스 재시작/OOM은 없었다.
- 갱신 후에도 계획은 약 550만 행 전체 스캔과 window 정렬을 유지했다. 따라서 인덱스
  추가·10년 범위 축소·결과 캐시는 분석 의미를 바꾸거나 압축 chunk 비용을 줄이지 못하므로
  이번 단계에서는 적용하지 않았다. 이 경로의 다음 개선은 별도 사전집계 설계와 결과
  동등성 검증이 필요하다.
