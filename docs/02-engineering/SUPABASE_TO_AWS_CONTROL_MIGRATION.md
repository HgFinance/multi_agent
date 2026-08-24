# Supabase → AWS control 도메인 데이터 이전 (1회성 Cutover)

상태: 구현 완료, 실제 운영 DB 대상 실행 전
기준일: 2026-08-24
대상 저장소: `HgFinance/multi_agent`
관련: [`docs/database/README.md`](../database/README.md) · [`deploy/aws/README.md`](../../deploy/aws/README.md) · [`docs/03-data/AWS_DATA_MIGRATION_PLAN.md`](../03-data/AWS_DATA_MIGRATION_PLAN.md)(TimescaleDB/Parquet 이전 전용, 이 문서와 범위가 다름)

## 1. 이 문서가 다루는 것

**Schema migration이 아니다.** `supabase/migrations/`가 canonical schema chain이고
`scripts/aws_database_bootstrap.py`가 이미 그 chain을 AWS `control` DB에 replay한다.
이 문서와 `scripts/migrate_supabase_to_aws_control.py`는 그 위에서, Hosted Supabase에
쌓여 있는 **실제 row 데이터**를 AWS `control`로 1회성 복사하는 것만 다룬다.

```text
scripts/aws_database_bootstrap.py   ->  AWS control schema 준비 (변경 없음)
scripts/migrate_supabase_to_aws_control.py
  --dry-run                         ->  preflight + 충돌 검사, 쓰기 없음
  --execute                         ->  실제 복사 (명시적 opt-in 필요)
  --validate-only                   ->  복사 후 재검증
```

Hosted Supabase Auth는 이전 대상이 아니고 계속 identity source of truth로 남는다.
`governance.user_profiles.user_id`는 local `auth.users`의 FK가 아니라 검증된 Supabase
JWT `sub`이며, 이 관계는 이전 전후로 UUID 값이 그대로 보존되어야 유지된다 — 이
도구는 그 값을 그대로 복사할 뿐 재발급하지 않는다.

## 2. 이전 대상 / 제외 대상

**포함**: `supabase/migrations/`가 소유한 domain schema의 base table 전부. 스키마
이름과 개수는 하드코딩하지 않고 매 실행마다 `information_schema`/`pg_catalog`로
다시 검사한다 (현재 governance/workforce/reference/research/quant/strategy/
execution/risk/accounting/audit/experience 11개, 96개 이상의 migration 파일 기준
약 211개 table — `experience`는 2026-08-24 `20260824000100_memo_harness_experience_bank.sql`로
추가된 신규 schema).

**제외** (Supabase가 관리하는 schema, 코드 어디에도 데이터로 취급하지 않음):
`auth, storage, realtime, extensions, vault, graphql, graphql_public,
supabase_functions, supabase_migrations, net, pgbouncer, pgsodium,
pgsodium_masks, pgtle, cron`. `public`과 `api`는 base table이 0개여야 한다는 것을
매 실행마다 assert하며, 위반 시(즉 예상 못한 곳에 실제 data table이 생겼을 때)
바로 중단한다 — 조용히 건너뛰지 않는다.

`market`(TimescaleDB) DB는 이 도구가 아예 연결하지 않는다.

## 3. 왜 새 schema migration을 또 만들지 않았나

`scripts/aws_database_bootstrap.py`가 이미 `supabase/migrations/`를 자체 checksum
추적 테이블(`supabase_migrations.schema_migrations`, sentinel checksum 방식)로
replay하고 있다. 이 도구는 그 replay를 다시 구현하지 않고, 대신 **재사용**한다:

- `bootstrap.discover_migrations` / `bootstrap.validate_applied_prefix` /
  `bootstrap._checksum_from_statements`로 target(`control`)의 migration history가
  repository 기준과 정확히 일치하는지(버전 *및* checksum) 확인한다. 어긋나면
  `scripts/aws_database_bootstrap.py`를 먼저 돌리라는 메시지와 함께 중단한다.
- `bootstrap.DEFAULT_USER_ID` / `DEFAULT_FUND_ID` / `DEFAULT_BOOK_ID` /
  `ACCOUNT_CHART`를 그대로 가져와 bootstrap이 이미 심어둔 PAPER 테스트 principal을
  "알려진 seed 데이터"로 인식한다(§5의 Case D).
- `bootstrap._connect` / `bootstrap._required_environment` /
  `bootstrap.database_name_from_dsn` / `bootstrap._set_transaction_timeouts`를 그대로
  사용해 DSN을 절대 출력하지 않는 안전장치를 그대로 물려받는다.

## 4. Preflight — 무엇을 확인하고 언제 중단하는가

`source`(Hosted Supabase)와 `target`(AWS `control`) 모두에 대해:

1. `source`의 `supabase_migrations.schema_migrations` 버전 목록이
   `supabase/migrations/`와 정확히 같은지 (하나라도 다르면 — source가 repo보다
   앞서 있든 뒤처져 있든 — 중단).
2. `target`의 migration history가 §3의 checksum까지 정확히 일치하는지.
3. 두 DB의 실제 schema(`pg_attribute`/`pg_constraint` 기준: column type,
   nullability, PK, FK, unique, check, generated 여부, `pg_get_constraintdef`
   전체 정의 문자열)를 **서로** 직접 비교한다. target은 이미 (2)에서 drift 없음이
   증명됐으므로, source와 target이 서로 같다는 것이 곧 source도 repository 기준과
   같다는 뜻이 된다 — SQL 파일을 다시 파싱해 "기대 schema"를 하드코딩하지 않는다.
4. `public`/`api`에 base table이 있으면 중단. 예상 밖 schema가 있으면 중단.

하나라도 어긋나면 **어디에도 아무것도 쓰지 않고** 종료한다(dry-run이든 execute든
동일).

## 5. 충돌 정책 (Pass 1 — 항상, 쓰기 전에 먼저)

Table마다 PK 단위로 source/target을 대조한다:

| Case | 조건 | 처리 |
|---|---|---|
| A | target에 해당 PK 없음 | 복사 대상 |
| B | 같은 PK, 전체 column 내용 동일 (canonical hash 일치) | idempotent skip — 이미 이전된 것으로 간주 |
| C | 같은 PK, 내용 다름 | **전체 migration 즉시 중단**, 어떤 table도 아직 쓰지 않은 상태 |
| D | target에만 있는 PK | `bootstrap.DEFAULT_*`/`ACCOUNT_CHART`로 식별되는 PAPER seed 데이터만 허용. 그 외는 전부 중단 |

Case C/D가 하나라도 있으면 **어떤 table도 복사를 시작하기 전에** 전체 실행을
중단한다 — 200번째 table의 충돌이 1번째 table의 쓰기를 막는다.

## 6. Transaction 경계 / FK 순서

- FK 순서는 sketch가 아니라 실제 `pg_constraint`에서 매 실행마다 계산한다
  (Tarjan SCC로 순환 감지). 순환이 있고 관련 FK가 전부 `DEFERRABLE`이면 그 그룹만
  한 transaction으로 묶어 처리하고, 하나라도 `NOT DEFERRABLE`이면 constraint를
  임의로 건드리지 않고 중단한다.
- 실제 복사는 **table 하나당 transaction 하나**다. 211개 table 전체를 하나의
  거대한 transaction으로 묶지 않는다(lock 시간 비현실적). 대신 전체 실행의
  안전성은 §5의 사전 충돌 스캔(쓰기 전 전수 검사) + idempotency(재실행 시
  이미 커밋된 table은 Case B로 재분류되어 건너뜀)로 확보한다.

## 7. 검증 (복사 후, 항상 다시 query해서 확인 — 메모리 카운터를 믿지 않음)

- **Level 1** row count.
- **Level 2** PK set 동일성(known seed 제외).
- **Level 3** canonical content hash — PK 순서로 정렬 후 UUID/`numeric`/
  `timestamptz`(UTC로 통일)/`jsonb`(key 정렬)/array를 canonical 직렬화해 table당
  하나의 hash로 집계, source·target 비교. 제외되는 column은 없다 — 이번 조사에서
  "migration tool이 만든 metadata"로 볼 수 있는 domain column을 찾지 못했다.
- **Domain invariant** — `execution.orders/order_events/fills`,
  `accounting.journals/journal_lines/positions/cash_balances`,
  `risk.risk_requests/risk_decisions`에 대해 count·수량 합계·POSTED journal의
  debit/credit 합계가 source/target에서 일치하는지 별도로 재확인한다.

`--validate-only`로 복사 없이 이 검증만 다시 돌릴 수 있다(cutover 직전 재확인용).

## 8. 실행 방법

### 8.1 환경변수

새 이름을 만들지 않고 기존 convention을 재사용한다:

- `SUPABASE_DATABASE_URL` — source. `.env.example`에 이미 있던(현재 앱 코드는
  무시하는) legacy 변수를 이 도구의 유일한 source DSN으로 재지정했다.
- `CONTROL_DATABASE_URL` — target. `scripts/aws_database_bootstrap.py`가 쓰는
  것과 동일한 admin DSN.
- `HEDGEFUND_CONTROL_DB_NAME` — bootstrap과 동일 (기본값 `control`).

### 8.2 Dry run (쓰기 없음)

```bash
SUPABASE_DATABASE_URL=... CONTROL_DATABASE_URL=... \
  python scripts/migrate_supabase_to_aws_control.py --dry-run
```

`migration_readiness: PASS`가 아니면 그 다음 단계로 진행하지 않는다.

### 8.3 Backup

이 저장소에는 아직 `control` DB용 자동 backup/snapshot 도구가 없다
(`deploy/aws/README.md`가 언급하는 `--skip-database-backup`는 실제로는 구현돼
있지 않다 — §11 참고). 자동화를 새로 만드는 대신, `--execute`는 operator가
직접 backup을 확인했다는 attestation을 **강제로 요구**한다:

```bash
# 예시: control DB만 별도로 백업
pg_dump "$CONTROL_DATABASE_URL" -Fc -f control-pre-migration-$(date +%Y%m%dT%H%M%S).dump
```

### 8.4 Migration 실행

```bash
SUPABASE_DATABASE_URL=... CONTROL_DATABASE_URL=... \
  python scripts/migrate_supabase_to_aws_control.py \
  --execute \
  --confirm-source-supabase \
  --confirm-target-control \
  --target-backup-reference "control-pre-migration-20260824T090000.dump"
```

`--confirm-source-supabase`/`--confirm-target-control`/`--target-backup-reference`
중 하나라도 빠지면 `--execute`는 즉시 거부된다. 실행 결과는
`migration_reports/supabase_to_control_<timestamp>.json`에 남고, `audit.traces`에
`DATA_MIGRATION` 종류의 run record가 하나 생긴다(RUNNING → COMPLETED/PARTIAL).

### 8.5 Validation 재확인

```bash
SUPABASE_DATABASE_URL=... CONTROL_DATABASE_URL=... \
  python scripts/migrate_supabase_to_aws_control.py --validate-only
```

### 8.6 Cutover

`deploy/aws/docker-compose.paper-order.yml`과 AWS 경로의 application 코드는 이미
전부 `DATABASE_URL`/`CONTROL_DATABASE_URL`을 통해 `control`을 바라보고 있고
(`SUPABASE_SERVICE_ROLE_KEY`는 AWS container에 아예 주입되지 않는다는 것도
`tests/contracts/test_aws_paper_order_deployment.py`가 이미 강제한다), 이 점은
이번 조사에서 새로 확인됐을 뿐 이 작업이 만든 변경이 아니다. 따라서 코드 레벨의
"cutover 작업"은 없다 — validation이 PASS하면 AWS stack을 (재)배포하는 것 자체가
cutover다.

## 9. Rollback

복사 전용 도구라 source(Supabase)는 절대 수정·삭제되지 않는다.

- **Cutover 전** 문제 발견: 아무것도 하지 않는다 — app은 원래부터 `control`을
  본 적이 없으므로 되돌릴 것이 없다.
- **Cutover 후** 문제 발견: app의 `DATABASE_URL`/`CONTROL_DATABASE_URL`을 원래
  Supabase pooler DSN으로 되돌리고 재배포한다. Source가 손대지지 않았으므로
  데이터 손실 없이 즉시 되돌릴 수 있다.
- **`control` DB 자체가 손상**된 경우에만 §8.3에서 만든 `pg_dump` 백업으로
  복구한다.

양방향 replication은 만들지 않았다 — 필요하지도, 요청받지도 않았다.

## 10. 남아 있는 위험 / 확인하지 않은 것

- `execution/accounting/risk` domain invariant 점검(§7)에 쓰인 정확한 column
  이름은 `20260729000400_execution_risk_accounting.sql`을 직접 읽어 확정한
  것이다. 이후 관련 schema가 바뀌면 이 도구의 `DOMAIN_INVARIANT_QUERIES`도 같이
  갱신해야 한다.
- `docs/03-data/AWS_DATA_MIGRATION_PLAN.md`(2026-08-12, 재일 소유)는 `research.*`가
  "이미 클라우드라 옮길 것 없음"이라고 적어놓았는데, 이는 TimescaleDB/Parquet
  이전만 다루던 시점의 가정이고 지금 이 문서가 다루는 domain schema 이전과는
  별개다. 그 문서를 다시 쓰지 않고 이 문서로의 pointer만 추가했다.
- `governance.user_profiles`가 참조하는 `20260729000100_foundation_reference.sql`이
  과거 어떤 시점에 FK를 포함한 버전으로 이미 적용된 환경이 있었을 가능성이
  조사에서 발견됐다(현재 파일에는 FK가 없음). 그런 환경의 `control` DB를 대상으로
  이 도구를 돌리면 §4의 checksum drift 검사에서 정확히 잡혀 중단되도록
  설계했지만, 실제로 그런 환경이 존재하는지는 실행 전까지 알 수 없다.
- `SUPABASE_DATABASE_URL`/1536차원 embedding 등 조사 중 발견한 기존 문서-코드
  불일치는 이 작업의 부산물로 조용히 고치지 않았다 — 별도로 다뤄야 한다.
