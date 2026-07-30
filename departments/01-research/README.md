# 리서치본부 (Research)

## Mission

데이터 수집, RAG Evidence와 Research Packet 생성을 담당한다. Universe/Technical/Microstructure/News
Analyst를 소집해 종목별 근거, 촉매, 무효화 조건을 갖춘 Research Packet을 만든다.

## Owner

재일님 — [TEAM_JAEIL_RESEARCH_QUANT_GUIDE](../../docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md)

## 입력·출력 계약

- 입력: LS Open API 체결·10단계 호가(수집기 미구현), Open DART 공시·재무,
  `collectors/news.py`(Tavily)로 조회한 뉴스
- 출력: Research Packet (근거, 촉매, 무효화 조건) → `workflow` step 2 트레이딩본부로 전달
- 시장 시계열 저장·조회 경계는 `repository/market_repository.py`의 `MarketDataRepository`다.
  다른 본부는 이 Repository가 아니라 `market-api`(미구현)를 호출한다

## 구성

| 경로 | 내용 | 상태 |
|---|---|---|
| `hermes/` | Git 기준 Hermes Profile 사본 (`config.yaml`, `SOUL.md`) | 사용 중 |
| `contracts/market_events.py` | 정규 Market Event 계약 — `instrument_id`, 시각 규칙, `MarketTick`/`MarketQuote`, 멱등 `source_event_id`, Quarantine, Event Envelope | Sprint J0 완료 |
| `collectors/source_registry.py` | 수집 Source 카탈로그와 API Key 확보 상태 판정, 라이선스 Scope 강제 | Sprint J1 기반 완료 |
| `collectors/subscription_plan.py` | 종목별 실시간 구독 계획. 18개 TR 매트릭스(주식·선물·옵션 / 국내·해외), 범위 Gate, Universe 정의 | Sprint J1 기반 완료 |
| `collectors/news.py` | Tavily 뉴스 조회 Baseline. 탐색 전용이며 본문을 Storage·pgvector에 적재하지 않는다 | Baseline |
| `repository/market_repository.py` | `MarketDataRepository` 인터페이스 + `InMemory`/`Timescale` 두 구현 | Sprint J0 완료 |

미구현: LS WebSocket 수집기, Instrument Mapping, Redis Snapshot, `market-api`, Parquet Archive.
진행 상황은 팀 가이드 9절에 항목별로 적어둔다.

## 실행법

```bash
research-department chat -q 'Build a Research Packet for AAPL'

# 로컬 시장 시계열 DB (compose 프로젝트 hedgefund, 호스트 포트 5434)
docker compose up -d
docker compose exec -T timescaledb psql -U postgres -d market -v ON_ERROR_STOP=1 \
  < timescaledb/local-dev/001_dev_roles.sql
docker compose exec -T timescaledb psql -U postgres -d market -v ON_ERROR_STOP=1 \
  < timescaledb/migrations/001_initial_market_data.sql

# 뉴스 조회 (TAVILY_API_KEY 필요)
python departments/01-research/collectors/news.py '삼성전자 주가'
```

마이그레이션은 멱등하지 않다 — `create table`에 `if not exists`가 없어 비어 있지 않은 DB에
재적용하면 실패한다. 재적용이 필요하면 `docker compose down -v`로 볼륨을 비우고 다시 올린다.

## 테스트

```bash
python departments/01-research/contracts/market_events.py       # 계약 6개 영역
python departments/01-research/collectors/source_registry.py    # Registry 6개 영역 + 현황 리포트
python departments/01-research/collectors/subscription_plan.py   # 구독 계획 6개 영역 + Universe 가용성
python departments/01-research/repository/market_repository.py   # Repository 계약 (DB 없이)
python departments/01-research/repository/market_repository.py --integration  # 실제 TimescaleDB
```

`--integration`은 `.env`의 `TIMESCALE_DATABASE_URL`과 살아 있는 컨테이너가 필요하다.
Raw Table은 append-only(마이그레이션의 `market.reject_raw_mutation()` 트리거)라 이전
데이터를 지울 수 없다. 그래서 실행마다 `instrument_id`와 `provider_symbol`을 새로 만들어
격리하며, 그 덕에 반복 실행이 가능하다. 불변식이 살아 있는지도 점검 항목에 포함된다.

`collectors/news.py`는 외부 API를 호출하므로 자체 점검 스크립트가 없다.

## Handoff

- 수집 Source의 API Key 확보 상태는 `source_registry.py`의 리포트가 기준이다. 키가 없는
  Source를 호출하면 예외가 나며, 빈 결과를 정상으로 취급하지 않는다
- 2026-07-30 기준 P0 Blocked Domain은 `NEWS` 하나다(BIGKinds·NAVER 키 미확보).
  KRX·ECOS·KOSIS·FRED 키가 들어와 `CALENDAR`와 `MACRO`는 풀렸다. 뉴스 값을 추정으로
  채우지 않는다
- **해외주식·파생 수집은 ADR 승인 대기 중이다.** TR은 확인됐고 코드 구조도 준비됐지만
  [HEDGE_FUND_CORE_PLAN.md](../../docs/01-product/HEDGE_FUND_CORE_PLAN.md)가 "단일 주식시장"을
  전제하므로 `build_plan(approved_scopes=...)`에 명시하지 않으면 계획 생성이 거부된다
- **주식 지수 구성종목을 주는 LS TR이 없다.** 국내(KOSPI200/KOSDAQ150)는 KRX 키로
  해결됐고, 미국(NASDAQ100/S&P500/DJIA)은 지수 사업자 라이선스가 없어 불가다. 파생은
  `o3101`/`o3121` 마스터로 전체 상품을 받을 수 있어 이 제약이 없다
- `references/` 이전 여부는 미결정 — [REPOSITORY_DEPARTMENT_STRUCTURE.md](../../docs/02-engineering/REPOSITORY_DEPARTMENT_STRUCTURE.md) 7절 참고
