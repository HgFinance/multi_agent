# Supabase Read-only Portfolio Adapter

사용자 적합성 추천의 실제 데이터 입력 경계다. 이 Adapter는 Canonical
`supabase/migrations/`의 다음 원천만 읽는다.

- `strategy.versions.target_portfolio_schema`: 포트폴리오 후보 메타데이터
- `research.documents`: Point-in-Time Research 근거
- `execution.market_snapshots`: `as_of` 이전 시장 Snapshot
- `accounting.portfolio_snapshots`: 선택된 `fund_id`의 회계 Snapshot

모든 조회는 `as_of`/`observed_at`/`published_at` 컷오프를 적용한다. 후보 JSON에
적합성에 필요한 필드가 없으면 후보를 생성하거나 기본값을 추론하지 않고 제외한다.

## 실행

DB DSN은 저장소에 기록하지 않는다. 기존 `DATABASE_URL`을 사용하거나, 별도 읽기
전용 DSN을 `SUPABASE_DATABASE_URL`로 지정한다. `SUPABASE_READONLY_DRIVER`는
`asyncpg` 또는 `psycopg2`로 지정할 수 있으며 기본값은 설치된 비동기 드라이버를
우선한다.

```bash
SUPABASE_READONLY_DRIVER=asyncpg \
python scripts/run_portfolio_supabase_readonly.py \
  --profile-json '{"user_id":"user-001","mindset":"BALANCED","experience":"INTERMEDIATE","investment_horizon_years":5,"max_drawdown_pct":"0.20","liquidity_need":"MEDIUM","as_of":"2026-08-04T00:00:00+00:00"}'
```

복사 과정에서 JSON 문자열 내부가 줄바꿈될 수 있으면 `--profile-file profile.json`
방식을 사용한다. JSON 필드 사이의 줄바꿈은 허용하지만 `user_id`나 `liquidity_need`
같은 따옴표 안 값의 줄바꿈은 허용하지 않는다.

`DATABASE_URL`/`SUPABASE_DATABASE_URL`이 없거나 DB 조회가 실패하면 결과는
`SUPABASE_UNAVAILABLE` 또는 `DEGRADED/HOLD`로 끝난다. 자동으로 TEST 후보를
대체하거나 외부 시스템에 쓰지 않는다.

연결과 PIT 데이터가 정상인 뒤에는 동일 `as_of`를 2~3회 반복해 추천·Risk/QA advisory 결과의 결정론적 Replay를 확인할 수 있다.

`--diagnose-only --as-of ...` 결과의 `data_diagnostics`는 후보가 없는 이유를 `versions_total`, `versions_as_of`, `versions_deployable`, `versions_with_schema`, `candidate_rows_before_contract`와 시장 Snapshot의 전체/시점/사용 가능 건수로 분리한다.

```bash
cp profile.example.json profile.json
python scripts/run_portfolio_supabase_readonly.py \
  --profile-file profile.json \
  --replay-count 2
```

이 명령은 OrderIntent를 만들거나 Paper Broker를 호출하지 않는다. 포트폴리오 적합성 추천과 주문 실행은 서로 다른 계약이므로 Paper 주문 검증은 별도 명시적 OrderIntent 테스트에서만 수행한다.

## 연결 복구 진단

프로필 없이 연결 경로만 점검할 수 있다. 출력에는 DSN, 호스트, 사용자, 비밀번호가 포함되지 않는다.

```bash
python scripts/run_portfolio_supabase_readonly.py --diagnose-only
# 연결 후 실제 PIT row 존재 여부까지 확인
python scripts/run_portfolio_supabase_readonly.py --diagnose-only \
  --as-of 2026-08-04T00:00:00+00:00
```

진단 순서는 DNS → read-only DB 연결 → canonical table visibility다. 대표적인 원인은 `DSN_NOT_CONFIGURED`, `INVALID_POSTGRES_DSN`, `DNS_RESOLUTION_FAILED`, `DATABASE_CONNECTION_FAILED`, `SCHEMA_PROBE_FAILED`다. 일시적인 DNS/연결 오류는 bounded retry 후에도 실패하면 `HOLD`로 종료한다.

현재 환경에서 `DNS_RESOLUTION_FAILED`가 나오면 Supabase 데이터가 없다는 뜻이 아니다. 실행 호스트의 DNS/방화벽/네트워크 경로가 Supabase Pooler에 도달하지 못한 상태이므로, 로컬 실행 환경의 DNS와 Pooler 주소·포트를 먼저 확인해야 한다.

## 권한 경계

Adapter에는 INSERT/UPDATE/DELETE 경로가 없다. DB 연결도 read-only transaction으로
설정하며, Broker 주문·Ledger Posting·Risk 승인·Production 승격은 이 경계에서
수행하지 않는다. Supabase의 실제 Row Level Security와 별도로, 운영 환경에서는
읽기 전용 DB role/Pooler 계정을 사용한다.
