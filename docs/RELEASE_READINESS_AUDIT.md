# Release Readiness Audit

검수일: 2026-08-28 UTC

## 결론

현재 상태는 **릴리스 승인 불가(BLOCKED)**다. PAPER 주문 경계와 Quant 큐 병목 처리는
검증했지만, PDF의 스트레스 시나리오를 실제 부하로 실행한 증거와 현재 지연도 증거가
없다. 이 문서는 미충족 상태를 기록하는 감사 결과이며, 통과 보고서가 아니다.

## 변경·검증된 항목

| 영역 | 상태 | 증거 |
|---|---|---|
| PAPER 주문 | PASS | `STRATEGY_PAPER_ORDERS_ENABLED=true`를 Compose·`.env.example` 기본값으로 유지하고, Bundle → runtime-control → Trading PAPER directive → idempotent gateway 경로 테스트 통과 |
| LIVE 차단 | PASS | 전략 배포 요청의 `mode=live`는 계속 `BLOCKED` |
| Quant 병목 | PASS | 상주 `quant-experiment-worker` Compose 서비스 추가, 배치 기본 2, 작업별 DB 연결, worker 상한 8, lease heartbeat 유지 |
| 워커 헬스 | PASS | 로컬 health timestamp와 Compose `--healthcheck` 추가 |
| Garbage collection | PASS | 호출자·테스트 참조가 모두 0인 `packet_gate.py` 제거; 테스트가 쓰는 두 후보는 보류 |
| 직접 의존성 | PARTIAL | `langsmith`, `psycopg[binary]`, `pyarrow`를 명시; Python lockfile/SBOM/CVE는 미완료 |

PAPER 검증은 fixture/mock을 이용한 결정론적 테스트다. 외부 계좌에 PAPER 주문을 실제로
제출하지 않았으며, 운영 계좌 health·credential·broker 응답까지 통과했다는 뜻은 아니다.

## 검증 스냅샷

2026-08-28 UTC 기준 결과:

- Python 전체: `3591 passed, 31 skipped, 232 subtests passed`, 실패 0
- 프론트엔드: `53 passed`; lint 통과; production build 통과
- `docker compose config --quiet` 통과
- 로컬 PAPER AWS overlay Compose config 통과(필수 fixture UUID를 주입한 검증)
- 실제 로컬 `quant-experiment-worker`: `running / healthy`
- 로컬 워커 이미지 ID: `sha256:addab94683c33856d4f38da51fc100c7de6a8960d65e45de9b250e493763dabe`

마지막 항목은 로컬 이미지 식별자이며 레지스트리 push digest가 아니다. 외부 브로커 주문은
이 검증에서 제출하지 않았다.

## PDF 기준 미충족 항목

### Stress Scenario 10개 — BLOCKED

PDF p.64에는 시나리오 설명이 있으나 다음 실행 증거가 없다.

- workload, 동시성, 지속시간, SLA 정의
- p50/p95/p99 지연도
- 처리량, 오류율, 자원 사용량
- 장애 주입 및 복구 결과
- 전용 stress/load 실행기와 CI job

따라서 10개 시나리오는 모두 `NOT VERIFIED`이며, 일반 pytest 통과를 스트레스 통과로
간주하지 않는다.

### 현재 지연도·병목 증거 — NOT VERIFIED

현재 런타임의 p50/p95/p99 보고서와 단계별 trace가 없다. PDF p.51의 백테스트 약 6시간,
에이전트 응답 약 60초는 과거 실험·발표 수치이므로 현재 SLA 증거로 승격하지 않았다.
이번 변경은 Quant 워커의 직렬 큐 병목을 구조적으로 제거했지만, 개선 전후 latency를
측정했다고 주장하지 않는다.

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

- Python lockfile이 없어 재현 가능한 dependency resolution이 없다.
- SBOM과 CVE 스캔 결과가 없다.
- `fact_router.py`, `ceo_hermes_client.py`는 운영 정적 호출자는 0이지만 테스트 참조가
  남아 있어 유지했다. 상세 결과는 [SOURCE_GARBAGE_COLLECTION.md](SOURCE_GARBAGE_COLLECTION.md)에 있다.
- Quant 워커의 실제 처리량과 DB pool 포화 여부는 부하 실행 후 다시 측정해야 한다.

## 릴리스 판정·rollback 기준

현 시점은 stress/latency/E2E 증거가 없으므로 릴리스 승인하지 않는다. 다음 증거가 모두
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
