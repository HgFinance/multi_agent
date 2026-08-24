# Supabase → AWS control 도메인 데이터 이전

상태: **구현 완료 · 실행 전(dry-run 미완주)** · 실측일 2026-08-24

## 1. 이 작업이 실제로 무엇인가

착수 전 가정은 "Hosted Supabase 도메인 데이터를 AWS `control` 로 대량 이전"이었다.
실측 결과는 반대였다.

| | Hosted Supabase (source) | AWS control (target) |
|---|---|---|
| 도메인 행 합계 | **245** | **135,833** |
| execution.orders / fills | 0 / 0 | 0 / 0 |
| execution.user_order_requests | 0 | 37 |
| accounting.journals / lines | 0 / 0 | 25 / 98 |
| reference.instruments | 0 | 4,303 |
| quant.universe_members | 0 | 2,694 |

`control` 은 이미 운영 SoT 이고, Supabase 에만 남은 데이터는 **8개 표 91행**뿐이다.
따라서 이것은 대량 cutover 가 아니라 **소규모 정합화**다.

> 금융 원장(주문·체결·분개)은 **양쪽 모두 Supabase 에 없다.** 이전 대상이 아니다.

## 2. 경계

- Supabase Auth 는 그대로 identity SoT. `auth.*` 는 읽지도 쓰지도 않는다.
- `governance.user_profiles.user_id` 는 검증된 JWT `sub` 를 그대로 보존한다. 로컬 `auth.users` 를 만들지 않는다.
- `market`(TimescaleDB) 는 이번 범위 밖.
- 제외 스키마: `auth storage realtime extensions vault graphql graphql_public supabase_functions supabase_migrations net pgbouncer pgsodium pgtle cron`.
  `public` / `api` 는 base table 이 0개임을 **검증**하고 넘어간다(조용히 건너뛰지 않는다).

## 3. 스키마는 이전하지 않는다

`supabase/migrations/` 가 canonical chain 이고 `scripts/aws_database_bootstrap.py` 가 이를 `control` 에 replay 한다.
이 도구는 **행만 복사**하며, 그 전에 두 DB 가 구조적으로 동일함을 증명하지 못하면 한 행도 쓰지 않는다.

## 4. 선행 결함(발견·수정)

1. **마이그레이션 버전 중복** `20260824000100` 이 두 파일에 있었다
   (`workforce_roster_full_reconcile`, `memo_harness_experience_bank` — PR #402 머지 충돌).
   → `memo_harness_experience_bank` 를 `20260824000150` 으로 이동. **workforce 쪽을 옮기지 않은 이유**: hosted
   Supabase 가 이미 `20260824000100 = workforce_roster_full_reconcile` 로 기록해 두었기 때문에 그쪽을 건드리면
   원격 이력과 영구히 어긋난다.
2. **control 이력 drift** — 기록된 이력은 `20260820000500` 에서 멈췄는데 `experience.workflow_experiences` 는
   실제로 존재한다(이력 밖 적용). **미해결** — 5절 참조.

이 둘이 workforce 격차(45 vs 23)의 단일 원인이다. 데이터 복사가 아니라 **마이그레이션 적용으로 해소**해야 한다.

## 5. 실행 전 반드시 해결할 것 (BLOCKER)

`control` 의 이력을 canonical chain 과 맞춰야 한다(`aws_database_bootstrap.py` 재실행).
**지금 그대로 실행하면 안 된다** — 다른 세션이 작업 중인 미커밋 마이그레이션까지 라이브 DB 에 적용된다.
해당 작업자와 조율한 뒤 실행할 것.

## 6. 충돌 정책 — 표마다 명시 선언

`control` 에 정당한 target-only 행이 135,810 개 있으므로 "전부 일치해야 한다"는 규칙은 영원히 중단만 한다.
그렇다고 어느 쪽이 이기는지 추측하지 않는다. **모든 표가 매니페스트에 선언되어야 하고, 미선언 표는 치명적 오류다.**

`deploy/aws/supabase_control_migration_scope.json` (210개 표):

| 정책 | 개수 | 의미 |
|---|---|---|
| `COPY` | 8 | Supabase 에만 있는 행을 control 로 넣는다 |
| `CONTROL_AUTHORITATIVE` | 202 | control 이 SoT. **절대 쓰지 않는다.** 차이는 보고만 |

`COPY` 대상 8개 표(총 91행): `audit.findings`(55), `audit.eval_runs`(14), `audit.eval_results`(14),
`audit.incident_events`(2), `audit.incidents`(1), `audit.eval_sets`(1), `audit.corrective_actions`(1),
`governance.mandates`(3).

### 사람이 결정해야 하는 10개 표

양쪽 모두 행이 있고 개수가 다르다. 현재 초안은 전부 `CONTROL_AUTHORITATIVE`(= 쓰지 않음)로 두었다.

| 표 | source | control | 비고 |
|---|---|---|---|
| `governance.user_profiles` | 3 | 2 | **Supabase 에만 있는 사용자 1명.** identity 문제 — 방치하면 그 사용자는 control 에 없다 |
| `governance.fund_memberships` | 3 | 4 | |
| `accounting.funds` | 3 | 2 | |
| `accounting.ledger_accounts` | 9 | 15 | |
| `quant.dataset_manifests` | 2 | 3 | |
| `workforce.*` (5개 표) | 45/45/28/8/5 | 23/23/23/2/3 | 4절 마이그레이션 적용으로 해소됨 |

## 7. Case A~D 처리

- **A. target 비어 있음** → 복사.
- **B. 동일 PK + 동일 내용** → 이미 이전된 것으로 보고 건너뛴다(멱등).
- **C. 동일 PK + 다른 내용** → **중단**. 리포트에 **차이 나는 컬럼 이름**을 적는다(값은 절대 안 적는다).
  서버 생성 메타데이터라면 `--accept-metadata-drift schema.table:column` 으로 **한 컬럼씩 명시 허용**한다. 자동 무시는 없다.
- **D. source 에 없는 target 행** → `COPY` 표에서는 부트스트랩 시드 지문(`created_by_service='aws-paper-bootstrap'`,
  고정 PAPER UUID)과 대조해 설명되지 않으면 **중단**. `CONTROL_AUTHORITATIVE` 표에서는 정상으로 본다.

## 8. 안전 장치

- source 연결은 **트랜잭션 단위 read-only**. (세션 수준 SET 금지 — Supabase pooler 로 전파되어 공유 워크로드를 멈춘 전례가 있다.)
- 복사 전체가 **단일 트랜잭션**(`set constraints all deferred`). 중간 실패 시 control 은 그대로다.
- FK 순서는 실제 카탈로그에서 Tarjan SCC 로 계산. 비-deferrable 순환 FK 를 만나면 제약을 지우지 않고 중단.
- 긴 스캔에 `statement_timeout`/`lock_timeout` 적용.
- `GENERATED ALWAYS AS IDENTITY` 는 `overriding system value` 로 원본 키를 보존(현재 해당 컬럼 0개, 미래 대비).
- 빈 표에서는 `setval` 을 건드리지 않는다(값 1 소모 방지). 행이 있으면 `max(id)` 로 맞춘다.
- 이 도구가 남기는 `audit.traces` 자기 기록은 target 읽기에서 제외된다. 제외하지 않으면 성공한 실행도 `PARTIAL`
  로 보고되고 재실행 때 "설명 불가 데이터"로 분류되어 영구히 막힌다.
- DSN·비밀번호·토큰은 로그·예외·리포트 어디에도 남기지 않는다.

## 9. 실행 순서

```bash
# 0) 선행: control 이력 정합화 (5절 조율 후)
python scripts/aws_database_bootstrap.py

# 1) 백업 — 검증 실패 시 되돌아갈 지점
docker exec hedgefund-timescaledb pg_dump -U postgres -Fc -d control \
  > ~/hgfinance-db-backups/control-$(date +%Y%m%dT%H%M%SZ).dump

# 2) Dry run — 쓰기 없음. 매니페스트 없이 돌리면 무엇을 선언해야 하는지 알려준다
export SUPABASE_DATABASE_URL=...   # 세션 한정, .env 에 남기지 말 것
export CONTROL_DATABASE_URL=...
python scripts/migrate_supabase_to_aws_control.py --dry-run \
  --scope-file deploy/aws/supabase_control_migration_scope.json

# 3) 실행 — 명시적 opt-in 4개가 모두 있어야 한다
python scripts/migrate_supabase_to_aws_control.py --execute \
  --confirm-source-supabase --confirm-target-control \
  --scope-file deploy/aws/supabase_control_migration_scope.json \
  --target-backup-reference control-20260824T....dump

# 4) 검증 — 복사 없이 체크섬만 다시 본다
python scripts/migrate_supabase_to_aws_control.py --validate-only
```

`migration_reports/` 에 사람이 읽는 JSON 리포트가 남는다(gitignore 됨 — PK 가 들어간다).

## 10. Cutover

검증이 통과한 뒤에만 애플리케이션을 옮긴다. AWS 오버레이는 이미 `control` 을 가리키고 있고
(`deploy/aws/docker-compose.paper-order.yml`), 계약 테스트가 control DSN 에 "SUPABASE" 문자열이 들어가는 것과
컨테이너에 `SUPABASE_SERVICE_ROLE_KEY` 가 전달되는 것을 막고 있다. 프런트엔드의 Supabase Auth 클라이언트는 **그대로 둔다.**

## 11. 롤백

- 검증 실패 → **cutover 하지 않는다.** 복사는 단일 트랜잭션이라 부분 적용 상태가 남지 않는다.
- cutover 이후 문제 발견 → 9절 1단계 덤프를 `pg_restore` 로 복원.
- 양방향 복제는 만들지 않는다.

## 12. 남은 위험

- **control 이력 drift 미해결**(5절). 이게 풀리기 전에는 dry-run 자체가 fail-closed 로 중단된다.
- 6절 10개 표는 사람 결정 대기. 특히 `governance.user_profiles` 의 사용자 1명.
- 이 호스트는 여러 세션이 동시에 쓴다. 실행 시점 조율 필요.
