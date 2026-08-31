# Release Readiness Audit

검수일: 2026-08-30 UTC

최신 재검증 부록: 2026-08-30 UTC

2026-08-30에 Risk/Audit 이미지를 재배포하고 Research/Quant readiness 경합을
canonical single-flight cache로 줄였다. 최종 32-way read-only matrix는 9개
시나리오 모두 32/32 성공, 오류율 0%, p95 13.692–357.823ms였다. 유효한 CEO
사용자 E2E `t_c547d1d1`도 root → Research → 단일 synthesis → async QA graph와
최종 HTTP 200을 확인했고, 108.050초로 120초 SLA를 통과했다. 상세 수치는
[OPS_HEALTHCHECK_LATENCY_REPORT.md](OPS_HEALTHCHECK_LATENCY_REPORT.md)의 최종
재검증 절에 있다.

전용 `scripts/stress_test.py`와 `.github/workflows/stress-evidence.yml`을
추가했지만, 이번 실행은 장애를 주입하지 않아 recovery는 `NOT_INJECTED`다.
사용자 E2E runtime은 단일 샘플에서 terminal `completed`와 최종 결과 HTTP 200을
확인했고, 최신 샘플은 108,050ms로 120,000ms SLA를 통과했다. 장애복구/rollback,
10개 시나리오 전체 certification, Container image/OS scan은 여전히 릴리스 승인
전 미검증이다.

## 결론

현재 상태는 **릴리스 승인 불가(BLOCKED)**다. Risk/Audit pool 경합과 readiness
지연은 수정·측정됐고 사용자 query-to-result 최신 샘플도 SLA를 통과했지만,
PDF의 전체 stress certification, 장애복구/rollback, image/OS scan 및 지속 운영
E2E 승인이 남아 있다.
이 문서는 미충족 상태를 기록하는 감사 결과이며, 통과 보고서가 아니다.

## 변경·검증된 항목

| 영역 | 상태 | 증거 |
|---|---|---|
| PAPER 주문 | PASS | `STRATEGY_PAPER_ORDERS_ENABLED=true`를 Compose·`.env.example` 기본값으로 유지하고, Bundle → runtime-control → Trading PAPER directive → idempotent gateway 경로 테스트 통과 |
| LIVE 차단 | PASS | 전략 배포 요청의 `mode=live`는 계속 `BLOCKED` |
| Quant 병목 | PASS | 상주 `quant-experiment-worker` Compose 서비스 추가, 배치 기본 2, 작업별 DB 연결, worker 상한 8, lease heartbeat 유지 |
| 워커 헬스 | PASS | 로컬 health timestamp와 Compose `--healthcheck` 추가 |
| Garbage collection | PASS | 등록된 3개 레거시 소스 후보의 파일·운영 참조·테스트 참조가 모두 0이고 모두 `REMOVED` |
| 직접 의존성 | PASS (Python baseline) | `langsmith`, `psycopg[binary]`, `pyarrow` 명시; `requirements.lock`, Python SBOM, pip-audit clean. Container image/OS scan은 잔여 |

PAPER 검증은 fixture/mock을 이용한 결정론적 테스트다. 외부 계좌에 PAPER 주문을 실제로
제출하지 않았으며, 운영 계좌 health·credential·broker 응답까지 통과했다는 뜻은 아니다.

## 검증 스냅샷

2026-08-28 UTC 기준 결과:

- Python 전체: `3747 passed, 31 skipped, 239 subtests passed`, 실패 0
- 프론트엔드: `53 passed`; lint 통과; production build 통과
- `docker compose config --quiet` 통과
- 로컬 PAPER AWS overlay Compose config 통과(필수 fixture UUID를 주입한 검증)
- 실제 로컬 `quant-experiment-worker`: `running / healthy`
- 로컬 워커 이미지 ID: `sha256:addab94683c33856d4f38da51fc100c7de6a8960d65e45de9b250e493763dabe`

마지막 항목은 로컬 이미지 식별자이며 레지스트리 push digest가 아니다. 외부 브로커 주문은
이 검증에서 제출하지 않았다.

최신 전체 회귀: `PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q` →
`3747 passed, 31 skipped, 2 warnings, 239 subtests passed`.

## PDF 기준 미충족 항목

### Stress Scenario 10개 — NOT VERIFIED

PDF p.64에는 시나리오 설명만 있었고, 현재는 bounded runner와 수동 CI job을
추가했다. 로컬 read-only 9개 시나리오는 32-way에서 오류율 0%로 실행됐다. CEO
사용자 E2E도 별도 1회 실행해 terminal `completed`/최종 결과 HTTP 200을
확인했고, 최신 유효 샘플은 108,050ms로 120,000ms SLA를 통과했다. recovery는
주입하지 않았다.

아직 다음 릴리스 게이트 증거가 없다.

- workload, 동시성, 지속시간, SLA 정의
- p50/p95/p99 지연도
- 처리량, 오류율, 자원 사용량
- 장애 주입 및 복구 결과
- 장애복구·rollback과 연결된 10개 시나리오 전체 결과

따라서 10개 전체를 `PASS`로 승격하지 않으며, 일반 pytest 또는 read-only smoke
통과를 운영 stress 통과로 간주하지 않는다.

### 현재 지연도·병목 증거 — NOT VERIFIED

read-only runtime의 현재 p50/p95/p99와 사용자 query-to-result 최신 단일 샘플은
OPS 보고서와 stress runner 실행 결과로 기록했다. 다만 단일 로컬 표본은 지속
운영 SLA certification이나 장애복구 증거가 아니다. PDF p.51의 백테스트 약
6시간, 에이전트 응답 약 60초는 과거 실험·발표 수치이므로 현재 SLA 증거로
승격하지 않았다.

### 조건주문 v1 — PARTIAL

현재 계약은 AND/OR, indicator/timeframe, 제한된 portfolio predicate를 지원한다. 다음은
지원되지 않거나 부분 구현이다.

- 동적 최대금액(notional) sizing
- 순차/시간적 조건과 일반 상태머신
- trailing/high-water 상태
- staged/multi-step exit
- portfolio 값의 CROSS는 durable 이전 봉 snapshot 부재로 거부

### 런타임 E2E — NOT VERIFIED

공식 상태 문서와 현재 아키텍처 문서가 continuously operated E2E를 runtime-verified로
표시하지 않는다. Compose 설정 검증은 실제 AWS·브로커·시장 데이터 운영 검증이 아니다.

## 의존성·레거시 잔여 작업

- Python `requirements.lock`, CycloneDX SBOM, `pip-audit` 결과는 확보됐고 현재
  알려진 Python 취약점은 없다.
- Docker image/OS 계층 CVE 스캔은 아직 CI 실행 결과가 없으며,
  `.github/workflows/container-security.yml`에서 이미지별 scan을 수행하도록
  추가했다.
- 운영 참조와 테스트 참조가 모두 0인 `fact_router.py`, `ceo_hermes_client.py`,
  `packet_gate.py`는 제거했다. 상세 결과는
  [SOURCE_GARBAGE_COLLECTION.md](SOURCE_GARBAGE_COLLECTION.md)에 있다.
- Quant 워커의 실제 처리량과 DB pool 포화 여부는 부하 실행 후 다시 측정해야 한다.

## 릴리스 판정·rollback 기준

최신 로컬 stress/latency/E2E 관측은 확보했지만 지속 운영·장애복구 certification이 없으므로
릴리스 승인하지 않는다. 다음 증거가 모두
추가되기 전에는 `READY`로 변경하지 않는다.

1. PDF 10개 시나리오별 workload/concurrency/duration/SLA와 p50/p95/p99 결과
2. 오류 주입·복구 결과 및 commit/image digest 연결
3. 부서별 E2E와 실제 런타임 health 증거
4. Python lockfile, SBOM, CVE 결과
5. garbage/legacy 호출 그래프와 승인된 삭제 목록

운영 중 duplicate order, lease 소유권 상실, DB pool 포화, healthcheck 연속 실패가 하나라도
관찰되면 `quant-experiment-worker`를 먼저 중지하고 마지막 승인 digest로 rollback한다.
단, 구체적인 수치 SLA와 rollback 자동화는 아직 이 저장소에 구현·검증되지 않았으므로
운영 승인 전에 별도로 확정해야 한다.

## 재현 명령

```bash
python scripts/release_readiness_audit.py
python scripts/source_garbage_collector.py
PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -q -p no:cacheprovider
docker compose config --quiet
```

자동 감사 테스트는 `tests/scripts/test_release_readiness_audit.py`에서 BLOCKED 상태를
고정해 둔다. 미충족 항목이 사라지지 않았는데 문서만 녹색으로 바뀌는 것을 방지하기 위한
의도적인 실패-게이트다.
