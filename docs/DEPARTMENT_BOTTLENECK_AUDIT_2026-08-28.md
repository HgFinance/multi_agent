# 전 부서 병목·의존성·레거시 점검 기록

최초 점검 시각: 2026-08-28 UTC · 추가 검증/수정: 2026-08-29 UTC
범위: CEO Office, HR/Agent Workforce, Research, Quant/Backtest, Trading, Risk, Accounting/Portfolio, AI QA/Audit
제외: LangSmith 원격 조회·쓰기. 용량 초과 상태이므로 이번 판정의 근거로 사용하지 않았다.

## 결론

전 부서의 공통 런타임 경계와 최근 오류 반복은 개선을 적용했다.

- 모든 canonical Hermes profile과 Compose Gateway에 `DISCORD_COMMAND_SYNC_POLICY=off`를 고정했다. Discord 메시지 ingress는 유지하고, 재시작마다 발생하던 slash-command 등록/429 대기를 제거한다.
- Trading 조건주문 reporting consumer는 Trading 권위 응답이 결정론적 계약 오류를 내면 같은 Redis event를 계속 재시도하지 않고 terminal ack한다. 기존의 일시적 DB·Discord·Notion 오류 재시도는 유지한다.
- conditional-rule outbox relay의 빈 polling cycle은 `DEBUG`로만 기록하고, 실제 pick/publish/fail/lost가 있을 때만 `INFO`로 기록한다. 기능 오류 로그는 `ERROR`로 유지한다.
- Research MCP health probe는 설정된 Bearer token을 재사용한다. 인증을 끄거나 401을 숨기지 않으며, 인증 실패·5xx·연결 실패는 health failure로 판정한다.
- control/market DB는 canonical bootstrap으로 이력·체크섬·terminal schema를 재감사했다. outbox lease 컬럼 누락과 market compression `max_runtime=0` drift를 기존 정식 기준으로 복구했다.
- Trading runtime bootstrap은 Compose에 선언된 `svc_trading_api`, `svc_strategy_paper_executor`, `svc_trading_outbox_relay` 세 capability role을 하나의 membership 계약으로 보존한다. PAPER 역할을 삭제하거나 generic 권한으로 합치지 않는다.
- Quant는 이전 변경으로 실험 worker 병렬 실행·bounded batch·lease heartbeat와 별도 Compose worker를 사용한다. PAPER 주문은 활성 상태를 유지한다.
- Garbage collector는 정적 호출자 0인 `departments/02-trading/contracts/packet_gate.py`만 제거했고, 동적 import·테스트 호출이 확인된 후보는 보존했다.

이 문서는 “운영 병목이 모두 해소됐다”는 선언이 아니다. Stress p95/p99, 처리량, 복구시간, 전체 E2E와 일부 데이터 계약은 아직 증명되지 않았으며 아래 미충족 표에 남긴다.

## 수집 증거

### 런타임·로컬 로그

- `docker compose ps`: 전체 서비스가 `running`; `quant-experiment-worker`는 `running/healthy`.
- Redis conditional stream: 각 reporting/projection group의 pending은 동일 poison event 1건으로 관측됐다. reporting event `1787897543231-0`가 약 10분 동안 562회 오류 로그를 만들었고, 오류는 `conditional directive must contain exactly one order leg`였다. 이 이벤트는 새 데이터가 와도 같은 malformed snapshot이면 유효해지지 않으므로 terminal ack 대상으로 분류했다.
- `conditional-rule-outbox-relay`: 빈 outbox를 1초 간격으로 확인하지만 `picked=0 published=0 failed=0`이며 유실은 관측되지 않았다. 1초 poll은 PAPER 알림 지연 예산을 보존하기 위해 유지했다.
- 추가 검증에서 outbox relay는 `claim_token`/`claim_expires_at` lease 컬럼 및 인덱스를 가진 DB에 연결됐고 healthcheck와 1회 drain이 exit 0이었다. 빈 cycle은 더 이상 기본 INFO 로그를 만들지 않는다.
- 추가 검증에서 Research MCP/liaison은 healthy였고 internal probe의 401은 사라졌다. `/mcp`의 406은 인증 실패가 아니라 HTTP 협상 응답이며, 인증된 MCP 호출은 200/202로 확인됐다.
- 추가 검증에서 `aws_database_bootstrap.py`는 control 123개 및 market 12개 migration audit을 모두 통과했다. market compression 대상 5개 job의 `max_runtime`은 모두 20분이다.
- `ceo-mirror-projection-worker`: 변경 이벤트가 없을 때 `scanned=0 projected=0 failed=0`으로 short-circuit한다. 반복적인 full projection은 실패가 아니라 주기적 reconciliation이다.
- `strategy-hermes`: 최근 cycle은 16개 lab을 관리하며 `AWAITING_NEW_DATA`, `COMPLETED`, `CANDIDATE`, `BLOCKED`가 혼재한다. 잘못된 `hypothesis_id`, 빈 `expected_behavior`, 빈 statement, manifest 불일치, Hermes timeout이 데이터/입력 품질의 미충족 증거다.
- `workforce-snapshot-writer`: capacity 5건은 기록했지만 `trading`은 `NO_WORKERS_REGISTERED`, 8개 비용 항목은 `no_token_measurement`로 건너뛰어 cost 0건이다. 이는 LangSmith가 없어서 0이라고 추정한 값이 아니라 측정 공백이다.
- 저장 장치: `disk-guard.log` 기준 여유 공간 약 96.5~97.1GB, 디스크 압박 오류는 이번 점검에서 관측되지 않았다.

### 부서별 Hermes profile 로그

profile log는 최근 운영 기록을 보존한 historical evidence로 읽었고, 오래된 로그의 건수를 현재 장애율로 해석하지 않았다.

| 부서 | 확인된 로그 신호 | 이번 조치 | 잔여 확인사항 |
|---|---|---|---|
| CEO | task id 없는 `kanban_show`, gateway exit 1 | 전역 startup sync 차단, 공통 audit 기록 | 잘못된 task 입력을 호출 경계에서 더 일찍 거부할지 검토 |
| HR | artifact 누락, invalid base64, 30초대 terminal command | startup sync 차단, 관측 공백을 0으로 치환하지 않음 | artifact contract와 local latency 측정 보강 |
| Research | MCP session termination, Discord sync 429 | startup sync 차단 | MCP 재접속 p95와 데이터 대기 원인 분리 |
| Quant | 잘못된 종목 식별자, provider fail-fast, terminal 재시도 | startup sync 차단, bounded parallel worker 유지 | immutable report retry의 재현 케이스와 데이터 계약 정리 |
| Accounting | gateway exit, missing skill/task, Discord sync 429 | startup sync 차단 | 공식 NAV/Mark 공급원과 close 지연 E2E 증명 |
| Trading | MCP DNS/session 실패, Discord sync 429; 조건주문 poison event 반복 | startup sync 차단, poison event terminal ack | multi-leg 상태 보고 계약과 recovery replay |
| Risk | Discord connection reset/429, optional tool check 실패 | startup sync 차단, fail-closed 경계 유지 | pre-trade P99와 stress coverage 실측 |
| AI QA/Audit | malformed search, session scope close, artifact 오류 | startup sync 차단 | 전체 부서 E2E와 finding aging 실측 |

### Notion·Discord 교차 확인

Notion은 각 부서 DB를 읽기 전용으로 조회했다. 최신 목록은 CEO 46, Research 88, Risk 39, Quant 38, Trading 40, Accounting 56, QA 100, HR 7페이지였다. 최근 리포트에는 HR primary 누락, 일부 부서 timeout, Risk 데이터 오류, Accounting 공식 NAV 확정 대기, QA의 재현성 차단, HR p95/latency 미기록이 반복된다.

Discord 봇 8개는 현재 동일한 CEO 공유 채널을 읽는다. 최근 100개 메시지의 keyword scan에서 error 1건·blocked 1건이 보였지만, 같은 채널을 부서별 토큰으로 읽은 결과이므로 부서별 오류율로 집계하지 않았다. 부서별 병목을 증명하려면 `DISCORD_<DEPARTMENT>_CHANNEL_ID`를 분리하거나, 현재처럼 공통 채널을 유지할 경우 메시지의 canonical department metadata가 필요하다.

로컬 `/home/ubuntu/.hermes/discord_message_recovery.db`는 0바이트 빈 파일이라 복구 로그 증거로 사용할 수 없었다.

## 미충족 사항과 판정

| 항목 | 판정 | 근거/재현 |
|---|---|---|
| PAPER 주문 실행 | PASS | `tests/api/test_strategy_research.py`, PAPER flag true; LIVE는 여전히 차단 |
| Quant 직렬 병목 | IMPROVED | bounded worker/batch, `quant-experiment-worker` healthy |
| 소스 garbage 정리 | PASS (보수적) | `scripts/source_garbage_collector.py`, packet gate만 삭제; 동적 후보 보존 |
| Discord startup 429 | IMPROVED | 8 profile config + 8 Gateway wiring에 sync off 적용 |
| 조건주문 poison retry storm | IMPROVED | `ConditionalStatusError` terminal ack 테스트 추가 |
| 의존성 lock/SBOM/CVE | PASS (Python shared runtime baseline) | `requirements.lock` hash 고정, CycloneDX SBOM, `pip-audit` 결과를 추가했다. 별도 pinned Dockerfile의 OS/image scan은 잔여 |
| outbox no-op 로그량 | IMPROVED | 빈 cycle은 DEBUG, 실제 처리/실패만 INFO; 1초 polling과 retry 의미는 유지 |
| Research MCP 401 probe | IMPROVED | configured Bearer 재사용; 401/5xx는 실패, 404/406은 server response로만 허용 |
| DB schema/role drift | PASS (local audit) | canonical bootstrap 통과, outbox lease·compression runtime 복구, Trading 3개 capability SET ROLE 연결 검증 |
| conditional rule v1 전체 상태머신 | PARTIAL | dynamic sizing, 순차 조건, trailing/high-water, staged exit 증거 부족 |
| Stress 10 scenarios | BLOCKED | 부하량·동시성·지속시간·SLA·pass/fail 결과와 전용 CI job 없음 |
| p50/p95/p99 latency/throughput/error rate | NOT VERIFIED | 현재 로그는 일부 duration만 있고 전체 경로 percentile 집계 없음 |
| 장애복구/rollback | NOT VERIFIED | 서비스 health는 확인했지만 장애주입·복구시간·rollback acceptance 없음 |
| 전 부서 E2E | NOT VERIFIED | Notion/Discord 리포트에 primary 누락·timeout·관측 미기록 반복 |

## 검증 명령

```bash
python3 -m pytest -q tests/api/test_conditional_rule_notification_consumer.py \
  tests/contracts/test_discord_gateway_wiring.py
docker compose config --quiet
python3 scripts/release_readiness_audit.py
docker compose ps
```

### 이번 변경 후 실행 결과

- `.venv/bin/pytest -q`: **3597 passed, 31 skipped, 232 subtests passed** (174.89초, 경고 2건은 외부 라이브러리 deprecation).
- `docker compose config --quiet`: 통과.
- `python3 -m py_compile ...`: 통과.
- `git diff --check`: 통과.
- `conditional-rule-notification-consumer`: `running/healthy`; 기존 poison event의 두 consumer group pending은 0이며 terminal ack 로그를 확인했다.
- 8개 Hermes Gateway: 모두 `running`, 모두 `DISCORD_COMMAND_SYNC_POLICY=off`; 재기동 직후 Discord sync/429 오류는 관측되지 않았다.
- `requirements.lock`: `requirements.txt`에서 98개 Python package를 hash 포함으로 고정했다. `pip-audit 2.10.1` 결과는 **No known vulnerabilities found**이며 CycloneDX JSON SBOM을 생성했다.
- `docker compose build portfolio-bff`: `apps/api/Dockerfile`의 `--require-hashes -r requirements.lock` 설치와 Hermes CLI smoke check가 통과했다.
- `docker compose exec ... conditional-rule-outbox-relay --healthcheck/--once`: 둘 다 exit 0. `trading-api`, `paper-order-orchestrator-mcp`, `conditional-rule-worker`는 role membership 복구 후 healthy다.
- `scripts/aws_database_bootstrap.py`: control/market migration audit 통과. 재실행 가능성을 보장하도록 bootstrap membership 계약도 테스트로 고정했다.
- `python3 scripts/release_readiness_audit.py`: 전체 판정은 여전히 `BLOCKED`이며 `latency_sla`, `runtime_e2e`, `stress_evidence`가 미충족으로 남아 있다. 따라서 전 회귀 테스트 통과를 전용 stress 통과로 과장하지 않는다.

전체 release readiness 판정은 별도 문서인 `docs/RELEASE_READINESS_AUDIT.md`를 정본으로 한다. 현재 상태는 latency SLA, runtime E2E, stress evidence 때문에 `BLOCKED`이며, 이 문서의 개선 PASS는 그 세 항목을 임의로 통과시키지 않는다.

## 다음 승인 게이트

1. LangSmith를 사용하지 않고 Hermes/Compose 구조화 로그만으로 worker latency p50/p95/p99, queue wait, error/retry, throughput을 수집한다.
2. PDF p.64의 10개 시나리오에 부하량·동시성·지속시간·SLA·복구 기준을 부여하고 dedicated stress runner와 CI job을 추가한다.
3. 별도 pinned Dockerfile은 root lock을 무리하게 공유하지 말고 각 image의 SBOM/CVE scan을 추가한다.
4. HR 비용 측정은 token이 실제로 관측된 경우에만 기록하고, 미측정은 `UNKNOWN`으로 유지한다. local boundary latency를 cost 대용으로 쓰지 않는다.
5. 부서별 Notion/Discord report에는 `department`, `run_id`, `source`, `observed_at`, `latency_ms`를 필수로 묶어 공통 CEO 채널의 혼합 로그를 부서별 수치처럼 집계하지 않는다.
