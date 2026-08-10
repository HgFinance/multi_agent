# AWS Elastic Beanstalk 배포 — 도현 파트

담당: 도현 (트레이딩본부 + 회계·포트폴리오본부)
결정: 2026-08-10 — **범위는 도현 파트만**, EB 단일 환경 + docker-compose.

이 디렉터리는 트레이딩·회계 거래 생명주기를 EB에 올리는 산출물이다. 다른 6개 부서는
포함하지 않으며, 그 이유와 올리려면 무엇이 필요한지는 5절 인계 체크리스트에 있다.

## 1. 무엇이 올라가는가

| 서비스 | 역할 | 외부 노출 |
|---|---|---|
| `portfolio-bff` | `apps/api/main.py`. 화면이 붙는 유일한 입구 | **포트 80** |
| `portfolio-worker` | 자문 실행 큐 소비자 | 없음 |
| `trading-api` | OrderIntent 심사, Broker Order 상태 머신 | 없음 |
| `trading-outbox-relay` | `execution.outbox` → Redis 발행 → `SENT` | 없음 |
| `accounting-api` | 원장·평가·대사·일일보고 | 없음 |
| `accounting-ledger-consumer` | `SENT` 체결 봉투 → 분개 → Projection | 없음 |
| `redis` | Outbox 발행 대상 | 없음 |

**relay와 consumer가 이 배포의 핵심이다.** 둘 중 하나라도 안 뜨면 체결이 원장에
도달하지 않는다 — 틀린 숫자가 나오는 게 아니라 장부가 비어 있게 된다.

Hermes(부서 에이전트 질의)는 **의도적으로 뺐다.** 구독 한도를 쓰려면 호스트에서
`scripts/claude_code_proxy.py`가 돌아야 하는데 EC2에는 없다. `ENABLE_AGENT_ASK`를
켜지 않으므로 `/{부서}/agent/ask`는 503으로 남는다 — 조용히 종량 과금으로 새는 것보다 낫다.

## 2. 번들 만들기

```powershell
pwsh scripts/package_eb_bundle.ps1     # -> dist/eb-bundle.zip
```

EB Docker 플랫폼은 **번들 루트의 `docker-compose.yml`** 을 실행한다. 저장소 루트의
그 이름은 재일님 소유 로컬 개발 스택이 쓰고 있어서, 스크립트가 `deploy/eb/docker-compose.yml`을
번들 루트 자리로 옮겨 담는다. 스크립트는 만든 zip을 다시 열어 조립이 맞는지 확인한다
(EB용 compose가 맞는지, Windows 전용 볼륨·호스트 프록시 의존이 섞이지 않았는지).

이미지는 ECR에 올리지 않고 인스턴스에서 빌드한다. Paper 단계에서는 레지스트리·인증·
태깅이 늘어나는 것보다 움직이는 부품이 적은 쪽이 낫다. 대신 첫 배포가 느리다.

## 3. 배포

```bash
eb init --platform "Docker running on 64bit Amazon Linux 2023" --region ap-northeast-2
eb create hedgefund-paper --instance-types t3.large --single

# 환경변수. DATABASE_URL 이 빠지면 trading-api/accounting-api 는 503으로 fail closed 되고
# accounting-ledger-consumer 는 아예 뜨지 않는다 - 조용히 인메모리로 후퇴하지 않는다.
eb setenv \
  DATABASE_URL='postgresql://...supabase...' \
  PAPER_DB=true \
  ACCOUNTING_MODE=PAPER_DB

eb deploy --staged
```

**인스턴스 크기**: 컨테이너 7개 + 인스턴스 빌드라 `t3.small`로는 빌드에서 죽는다.
`t3.large` 이상을 쓴다.

**`--single`(단일 인스턴스)을 쓰는 이유**: `trading-api`와 `accounting-api`는 복제하면
안 된다. trading은 `PAPER_DB=false`일 때 주문 상태가 프로세스 메모리에 있고, accounting은
같은 장부에 동시에 분개하면 평균원가가 낡은 상태를 본다. `trading-outbox-relay`는
`for update skip locked` 덕에 복제해도 안전하지만 지금 처리량에 필요가 없다.
LB 환경으로 갈 거면 **먼저 복제 안전성부터 정리해야 한다.**

## 4. 헬스체크 — `/health`와 `/health/ready`는 다르다

`.ebextensions/01_health.config`가 EB 헬스체크를 **`/health`(liveness)** 로 지정한다.

| 경로 | 저장소 정상 | 저장소 장애 |
|---|---|---|
| `GET /health` | 200 `status: ok` | **200** `status: degraded`, `store_available: false` |
| `GET /health/ready` | 200 | **503** |
| 도메인 엔드포인트 | 정상 | **503** |

`/health`가 저장소를 조회하지 않는 것이 요점이다. 여기서 503을 내면 Supabase 순단
몇 초에 EB가 멀쩡한 인스턴스를 교체하기 시작하고, 그 사이 relay와 consumer까지 같이
죽는다. **프로세스가 살아서 주문을 올바르게 거절하는 상태를 "죽었다"로 판정하면 안 된다.**
트래픽 차단이 필요하면 ALB Target Group이 `/health/ready`를 보게 한다.

## 5. 다른 부서를 올리려면 — 인계 체크리스트

리서치·리스크·QA·CEO·인사·퀀트는 이 번들에 없다. 각 담당자가 아래를 고쳐야 Linux
EC2에서 뜬다. **이 항목들은 각 부서 소유 파일이라 도현 파트에서 고치지 않았다.**

| # | 문제 | 어디 | 고칠 방향 |
|---|---|---|---|
| 1 | `${USERPROFILE}/.hermes-*` 볼륨 | 각 부서 `compose.yaml`의 `*-hermes` | Windows 전용 경로다. named volume 또는 EFS로 바꾼다 |
| 2 | `host.docker.internal:8787` | 각 부서 `*-hermes`의 `ANTHROPIC_BASE_URL` | EC2에 claude CLI 프록시가 없다. 프록시를 컨테이너로 올리거나 그 부서 Hermes를 빼야 한다 |
| 3 | `127.0.0.1:80xx` 포트 바인딩 | 각 부서 `compose.yaml`의 `ports:` | EB에서는 publish 자체를 없앤다(컨테이너 네트워크로만 통신). 외부 입구는 BFF 하나 |
| 4 | `extra_hosts: host-gateway` | 각 부서 `*-hermes` | 2번과 같이 정리 |
| 5 | 루트 `docker-compose.yml`의 `timescaledb` | 재일님 | 시계열 DB를 EB 인스턴스에 같이 올릴지, RDS/외부로 뺄지 결정 필요 |
| 6 | 메모리 총합 | 전체 | Hermes 4개 × 1GB. 전 부서를 올리면 `t3.large`(8GB)로 부족하다 |

전사 스택을 올리기로 하면 이 파일의 `deploy/eb/docker-compose.yml`에 각 부서 서비스를
추가하는 것이 아니라, **부서별 fragment를 EB용으로 하나씩 정리한 뒤 합치는 것**이 맞다 —
지금 구조(`include:`)를 그대로 쓰면 한 부서의 Windows 전용 설정이 전체 배포를 막는다.

## 6. 아직 안 되는 것

- **NAV가 나오지 않는다.** Mark 공급원(`market-api`)이 없어 보유 종목이 생기는 순간부터
  `value_portfolio`가 평가를 거부한다(D3). 분개와 Position은 정상이고 스냅샷만 없다.
  `accounting-ledger-consumer` 로그에 `NAV 보류`가 찍히는 것이 정상 동작이다.
- **`/ui/snapshot`이 아직 Scripted Loop다.** `api.*` 읽기 뷰로 교체하는 작업이 남아 있다.
- **DLQ 재처리 도구가 없다.** `RELAY_MAX_ATTEMPTS=12`로 ~66분을 버티지만, 그걸 넘겨
  DLQ로 떨어진 봉투는 `execution.outbox`에서 사람이 직접 꺼내야 한다. `last_error`에
  원인이 남는다.
