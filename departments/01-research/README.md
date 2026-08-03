# 리서치본부 (Research)

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.
Local Ollama Alias는 [`Modelfile`](Modelfile)의 `qwen3:14b` 기반 `agent-research`이고 Hermes Profile은 `research-department`다. Build·Eval·권한 기준은 [Ollama Department Modelfile Guide](../../docs/02-engineering/OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md)를 따른다.

실제 실행 상태와 재일님 2주 계획·Daily Scrum은 [실행 현황과 통합 계획 v2.2](../../docs/PROJECT_IMPLEMENTATION_STATUS.md#41-재일님-리서치본부와-퀀트백테스트본부)을 기준으로 한다.

## Mission

데이터 수집, RAG Evidence와 Research Packet 생성을 담당한다. Universe/Technical/Microstructure/News
Analyst를 소집해 종목별 근거, 촉매, 무효화 조건을 갖춘 Research Packet을 만든다.

## Owner

재일님 — [TEAM_JAEIL_RESEARCH_QUANT_GUIDE](../../docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md)

## 입력·출력 계약

- 입력: LS Open API 종목 Master·체결·호가, OpenDART 공시·재무, 거시·Corporate Action,
  NAVER/Alpaca/Tavily 뉴스와 향후 승인된 X 유명 인사 Watchlist
- 출력: Research Packet (근거, 촉매, 무효화 조건) → `workflow` step 2 트레이딩본부로 전달
- 시장 시계열 저장·조회 경계는 `repository/market_repository.py`의 `MarketDataRepository`다.
  다른 본부는 이 Repository가 아니라 실행 중인 `market-api`를 호출한다

## 구성

| 경로 | 내용 | 상태 |
|---|---|---|
| `hermes/` | Git 기준 Hermes Profile 사본 (`config.yaml`, `SOUL.md`) | 사용 중 |
| `contracts/market_events.py` | 정규 Market Event 계약 — `instrument_id`, 시각 규칙, `MarketTick`/`MarketQuote`, 멱등 `source_event_id`, Quarantine, Event Envelope | Sprint J0 완료 |
| `collectors/source_registry.py` | 수집 Source 카탈로그와 API Key 확보 상태 판정, 라이선스 Scope 강제 | Sprint J1 기반 완료 |
| `collectors/subscription_plan.py` | 종목별 실시간 구독 계획. 18개 TR 매트릭스(주식·선물·옵션 / 국내·해외), 범위 Gate, Universe 정의 | Sprint J1 기반 완료 |
| `collectors/ls_client.py` | LS OAuth·REST 종목 Master와 영구 Instrument Mapping 주입 경계 | Sprint J1 완료 |
| `collectors/ls_realtime_adapter.py` | 국내 주식 체결·호가 Payload를 공통 Market Event로 정규화 | Sprint J1 완료 |
| `collectors/{opendart_collector,opendart_financial,corporate_action_collector}.py` | 공시·재무·Corporate Action 수집과 PIT 보존 | Sprint J2 Prototype |
| `collectors/{macro_collector,calendar_collector,market_breadth_collector}.py` | 거시·관측 Calendar·시장 Breadth와 DQ | Sprint J2 Prototype |
| `collectors/naver_news_collector.py`, `contracts/news_events.py` | 국내 뉴스 REST Polling을 공통 Push Stream 계약으로 제공 | P0 Prototype |
| `collectors/alpaca_news_collector.py` | 해외 뉴스와 일부 KRX 상장사 연결 | P1 보조 Source |
| X Filtered Stream Collector·승인 계정 Registry | 유명 투자자·정책 당국자·기업·산업 전문가의 공개 Post를 종목·주제에 연결 | P1 계획, 미구현 |
| `collectors/news.py` | Tavily 뉴스 조회 Baseline. 탐색 전용이며 본문을 Storage·pgvector에 적재하지 않는다 | Baseline |
| `repository/market_repository.py` | `MarketDataRepository` 인터페이스 + `InMemory`/`Timescale` 두 구현 | Sprint J0 완료 |
| `repository/reference_repository.py` | Supabase Instrument·Issuer·Document·재무·거시·CA Repository | Sprint J2 Prototype |
| `collectors/ls_realtime_service.py` | 전 종목 LS WebSocket, 4 Socket 구독과 Timescale 적재 Runtime | Docker 실행 확인 |
| `collectors/collector_scheduler.py` | 공시·거시·Reference·Archive Batch Schedule | Docker 실행 확인 |
| `api/market_api.py`, `api/main.py` | Snapshot·Bar·Breadth·DQ·Regime·Microstructure와 Evidence 조회 | Docker 실행 확인 |
| `api/mcp_server.py`, `api/tool_gateway.py` | Hermes Tool 호출면, 허용 경로 강제와 Bearer 인증 | `research-mcp` Docker 실행 확인 |
| `agents/`, `evidence/`, `scripts.py` | 분석가 6인, RAG 사서, Evidence Bundle과 Research Packet Pipeline v2 | 자체 점검 11개 통과 |
| `collectors/derivatives_collector.py` | KOSPI200 선물·옵션·Greeks Snapshot 수집 | 실제 적재 3,910건 확인 |
| `collectors/research_data_steward.py` | Research Source 전체의 실행·신선도·DQ Gate | `collector_runs` 367건, 실패 11건 분류 필요 |
| `collectors/market_archive_exporter.py`, `replay_restore_drill.py` | 검증된 Parquet Archive와 복구 Drill | 자체 점검·팀 가이드 증거 존재 |

남은 핵심: Redis Stream Producer, 상시 Feature/Priority Engine, Research Packet의 Canonical Artifact·Event,
파생 연속성 검증, 영속 Microstructure Feature, X Watchlist Collector와 Social Evidence 교차 검증.
진행 상황은 팀 가이드 9절에 항목별로 적어둔다.

## 실행법

```bash
research-department chat -q 'Build a Research Packet for AAPL'

# 로컬 시장 시계열 DB (compose 프로젝트 hedgefund, 호스트 포트 5434)
docker compose up -d
docker compose ps
curl http://127.0.0.1:8035/health
curl http://127.0.0.1:8036/health
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
python departments/01-research/collectors/ls_realtime_adapter.py # 체결·호가 정규화
python departments/01-research/contracts/news_events.py          # Polling/WebSocket 공통 Stream 계약
python departments/01-research/collectors/market_breadth_collector.py # Breadth·DQ
python departments/01-research/repository/market_repository.py   # Repository 계약 (DB 없이)
python departments/01-research/repository/market_repository.py --integration  # 실제 TimescaleDB
python departments/01-research/scripts.py                        # Research Pipeline 11개 영역
```

`--integration`은 `.env`의 `TIMESCALE_DATABASE_URL`과 살아 있는 컨테이너가 필요하다.
Raw Table은 append-only(마이그레이션의 `market.reject_raw_mutation()` 트리거)라 이전
데이터를 지울 수 없다. 그래서 실행마다 `instrument_id`와 `provider_symbol`을 새로 만들어
격리하며, 그 덕에 반복 실행이 가능하다. 불변식이 살아 있는지도 점검 항목에 포함된다.

`collectors/news.py`는 외부 API를 호출하므로 자체 점검 스크립트가 없다.

## Handoff

- 수집 Source의 API Key 확보 상태는 `source_registry.py`의 리포트가 기준이다. 키가 없는
  Source를 호출하면 예외가 나며, 빈 결과를 정상으로 취급하지 않는다
- 2026-07-31 기준 국내 뉴스 P0는 NAVER로 열려 있다. BIGKinds는 비용 대비 필요성이 확인될
  때까지 `DISABLED`이며 키가 생겨도 자동 활성화하지 않는다. P0 Blocked Domain은 미래 거래일을
  제공할 승인된 Source가 없는 `CALENDAR`다
- X의 "팔로우"는 자동 Follow가 아니라 내부 승인 Watchlist다. 공식 X API의 `from:` Filter Rule을
  사용하고, Post는 `UNVERIFIED_SOCIAL`로 시작해 공시·독립 뉴스·시장 데이터로 교차 검증되기 전에는
  Order Intent 또는 Strategy 승격의 단독 근거로 사용하지 않는다
- **해외주식·파생 수집은 ADR 승인 대기 중이다.** TR은 확인됐고 코드 구조도 준비됐지만
  [HEDGE_FUND_CORE_PLAN.md](../../docs/01-product/HEDGE_FUND_CORE_PLAN.md)가 "단일 주식시장"을
  전제하므로 `build_plan(approved_scopes=...)`에 명시하지 않으면 계획 생성이 거부된다
- **주식 지수 구성종목을 주는 LS TR이 없다.** 국내 KOSPI200/KOSDAQ150도 KRX 서비스 이용
  승인이 확인되기 전에는 자동 구성할 수 없어 명시적 Universe 입력이 필요하다. 미국
  NASDAQ100/S&P500/DJIA는 지수 사업자 라이선스가 없어 불가다. 파생은
  `o3101`/`o3121` 마스터로 전체 상품을 받을 수 있어 이 제약이 없다
- `references/` 이전 여부는 미결정 — [REPOSITORY_DEPARTMENT_STRUCTURE.md](../../docs/02-engineering/REPOSITORY_DEPARTMENT_STRUCTURE.md) 7절 참고
