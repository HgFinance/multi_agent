# 리서치본부 (Research)

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.
직원 런타임은 독립 LangGraph Worker와 Ollama `qwen3:1.7b`이며 Hermes Profile은 `research-department`(본부장 모델 `claude-opus-5`)다. `Modelfile`은 로컬 보조 실행용이고, Build·Eval·권한 기준은 [Ollama Department Modelfile Guide](../../docs/02-engineering/OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md)를 따른다.

실제 실행 상태와 재일님 2주 계획·Daily Scrum은 [실행 현황과 통합 계획 v2.2](../../docs/PROJECT_IMPLEMENTATION_STATUS.md#41-재일님-리서치본부와-퀀트백테스트본부)을 기준으로 한다.
Research와 Quant를 연결하는 목표 Graph, 계약과 논문 기반 도입 순서는
[Research-Quant Evidence-to-Strategy Framework](../../docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md)를 따른다.
리서치 산출물의 V3 계약, 소비 본부별 View, 품질 Gate와 단계별 구현 기준은
[Research Output Advancement Strategy](../../docs/02-engineering/RESEARCH_OUTPUT_ADVANCEMENT_STRATEGY.md)를 따른다.

## Mission

**가설 공급 조직이다.** 웹(논문·투자자 서한·실무자 글·커뮤니티·타 분야)에서 방법론을 수집해
반증 가능한 **실험 기획안**으로 만들어 퀀트본부에 넘긴다. 종목 방향·확률 예측은 하지 않는다 —
방향 판단은 실험을 통과해 승격된 전략의 몫이다.

수집·PIT·인용 검증 인프라는 그대로 쓴다. 바뀐 것은 **무엇을 만드느냐**다.

> 2026-08-10 재편. 이전 Mission("종목별 Research Packet 생성")은 프레임워크 자체가
> 투자판단을 내리는 구조를 전제했다. 종목 애널리스트 편제는 운영에서 내렸고, 코드는
> 감사 계보로만 남는다. 근거는
> [Research-Quant Strategy Factory Framework](../../docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md) 1절.

## Owner

재일님 — [TEAM_JAEIL_RESEARCH_QUANT_GUIDE](../../docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md)

## 입력·출력 계약

- 입력: 웹 방법론 소스(논문·프리프린트·투자자 서한·실무 블로그·커뮤니티·영상·타 분야 문헌),
  퀀트의 `ExperimentOutcomeV1` 기각 이력, 내부 시장 데이터(실행 가능성 판정용)
- 출력: **`ExperimentProposalV1`** (경제적 근거, 반대편 주체, 경쟁 설명, 통제 어휘 사상,
  데이터 요구, 반증 검사, 기각 이력 대응) → 퀀트본부 Gate 0
- 부가 출력: `MethodologyLeadV1`(스카우트 리드), Holding Brief(사용자 질의 응답 —
  **공장 입력이 아니고 주문 경로에도 닿지 않는다**)
- 시장 시계열 저장·조회 경계는 `repository/market_repository.py`의 `MarketDataRepository`다.
  다른 본부는 이 Repository가 아니라 실행 중인 `market-api`를 호출한다

## 직원 편제 (LLM Worker 8인)

| Worker | 역할 | 실행 |
|---|---|---|
| `methodology-scout-academic` (RES-11) | 논문·프리프린트 렌즈 | 소집 (`scout_cycle`) |
| `methodology-scout-practitioner` (RES-12) | 투자자 서한·실무자 글 렌즈 | 소집 |
| `methodology-scout-community` (RES-13) | 커뮤니티·영상·오픈소스 렌즈 | 소집 |
| `methodology-scout-crossdomain` (RES-14) | 타 분야 방법 전용(轉用) 렌즈 | 소집 |
| `competing-explanation-worker` (RES-15) | 경쟁 설명·반증 — **기획자와 입력 격리** | 소집 (`proposal_draft`) |
| `experiment-planner-worker` (RES-16) | 통제 어휘 사상, 데이터 요구, 반증 검사 | 소집 (`adopted_lead`) |
| `market-context-worker` (RES-17) | 유니버스·히스토리·DQ — 실행 가능성 재료 | 상시 |
| `holdings-analyst-worker` (RES-18) | 보유 종목 질의 응답 — **서비스 자리, 공장 미연결** | 소집 (`holding_question`) |

## 상주 운영과 처리량

**"24시간 상주"는 프로세스가 살아 있다는 뜻이지 쉬지 않고 검색한다는 뜻이 아니다.**
소스의 갱신 주기가 검색 주기의 상한이다 — arXiv는 하루 한 번 발행하는데 15분마다
뒤지면 같은 것을 96번 본다. 렌즈마다 주기가 다른 이유다.

숫자는 **병목에서 거꾸로** 잡았다. 공장의 처리량은 검색이 아니라 실험이 정한다.

| 단계 | 하루 물량 | 통과율 | 담당 계층 |
|---|---:|---:|---|
| 스카우트 소집(렌즈 4) | 히트 ~238 | — | 로컬 |
| 스니펫 스크리닝 | → 36 열람 | 15% | 로컬 |
| 본문 훑기 | → 7 큐 적재 | 20% | **로컬**(하루 ~700k 토큰 — 여기가 로컬의 값어치) |
| 편집장 정독·리드 작성 | 5건 | — | **본부장** |
| 기획안 채택 | 주 3건 | 10% | **본부장** |
| 실험 | 주 15회 | — | 결정론 파이프라인 |

주 15회는 `신규 family 3 × trial_budget 5`다. **`trial_budget` 5는 낭비 상한이자
PBO 성립 하한**이기도 하다(`pbo_cscv.MIN_VARIANTS = 4`) — 예산을 다 써야 변형 5개가
생겨 과적합 확률을 계산할 수 있다.

기획안 목표를 주 2건에서 **3건으로 올렸다.** 상주 가동인데 주 2건이면 입구를 열어둔
의미가 없다.

### 상주 운영의 두 가지 기본 실패 모드와 방어

1. **읽히지 않을 리드를 계속 만든다** → 편집장 큐 20건(약 3일치) 초과 시 스카우트 정지,
   퀀트 대기 10건 초과 시 기획안 발행 정지. 입구만 넓히면 재고만 쌓인다.
2. **고갈된 광맥을 24시간 판다** → 연속 3회 소집에서 신규 리드 0이면 그 주제를 24시간
   쉬게 하고 다른 데를 판다. 같은 질의 반복은 질의 대장(쿨다운 72시간)이 막는다
   — `lead_id` 해시는 같은 *문서*를 접을 뿐 같은 *질의*를 또 도는 것은 못 막는다.

파라미터 실체는 `hermes/config.yaml`의 `scout_operations` · `throughput_controls`다.

> 계측 먼저. 상주 가동 전에 ①하루 히트 ②스크리닝 통과율 ③실제 정독 수 ④기획안 전환율
> ⑤퀀트 대기 큐 다섯 수치를 볼 수 있게 해둔다. 없으면 어디가 막혔는지 모른 채 24시간 돈다.

## 직원 편제 보충

스카우트 4인은 같은 도구를 쓰고 **렌즈만 다르다**. 서로의 결과를 보지 않고 병렬로 뒤지는 것이
이 편제의 전부다 — 독립성은 프롬프트가 아니라 입력 격리로만 만들어진다.

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
파생 연속성 검증, 영속 Microstructure Feature, X Watchlist Collector, `research-web-mcp`와 Social/Web Evidence 교차 검증.
진행 상황은 팀 가이드 9절에 항목별로 적어둔다.

## 현재 Graph와 목표 Graph

현재 `scripts.py`는 여섯 분석가를 순차 실행하고, 마지막에 Hermes Profile의 Supervisor Persona를
읽은 일반 LLM 호출이 Packet을 합성한다. 즉 Profile과 Tool Gateway는 존재하지만 실제 Hermes
Runtime이 Case Queue, Checkpoint, Retry와 합성을 지휘하는 단계는 아직 아니다. `as_known_at`도
실행 시각으로 기록되지만 모든 Tool의 실제 Query Cutoff로 강제되지는 않는다.

목표 흐름은 다음과 같다.

```text
Research Hermes Case
  -> Point-in-Time Cutoff Lock
  -> 역할별 Retrieval Plan과 Context Timeline
  -> 여섯 분석가 독립 Branch
  -> Claim/Evidence·Citation·Numeric·Time Validator
  -> Macro/Micro 독립 Outlook
  -> Synthesis와 Skeptic
  -> ResearchPacketV2 발행
```

Branch는 현재 단일 GPU에서 `max_concurrency=1`로 실행한다. 병렬 표시는 하드웨어 동시 실행을
꾸미기 위한 것이 아니라, 역할 간 독립성과 부분 실패·Checkpoint 복구 경계를 정확히 나타내기
위한 것이다.

도입 우선순위:

1. `ResearchCaseV2`, `AnalystFindingV1`, `ResearchPacketV2`와 V1 Adapter
2. 전 Tool의 `as_known_at` Capability와 Fail-closed Replay
3. 역할별 Retrieval, Context Timeline과 Claim/Evidence Graph
4. LangGraph Fan-out/Fan-in, Branch Timeout과 Checkpoint
5. Macro/Micro Outlook, Skeptic과 제한된 Evidence Gap 재검색
6. 실제 Research Hermes Case/Queue/Retry Adapter

## Web Search MCP 배정

P0에서는 새 상시 Agent를 만들지 않는다. 기존 `RES-08 RAG Librarian/Evidence Curator`를
`RAG Librarian, Evidence Curator and Web Researcher`로 확장하고 `web-evidence-research` Skill과
`research.web.search/open/verify` 권한을 전담시킨다.

```text
Fundamental · News · Sector/Macro · Geopolitical Analyst
  -> WebSearchRequest
  -> RES-08 내부 RAG 재검색
  -> Self-hosted SearXNG 기반 research-web-mcp
  -> 상위 URL만 ArticleReader/Read-only Playwright MCP
  -> SEARCH_HIT
  -> Citation·Time·Numeric 검증
  -> VERIFIED_EVIDENCE
```

- Research Supervisor는 검색 Case의 우선순위·예산·SLA를 관리하지만 직접 검색하지 않는다.
- Fundamental, News/Sentiment, Sector/Macro와 Geopolitical은 `research.web.request`만 사용한다.
- Universe, Data Steward, Technical과 Microstructure는 Web MCP 없이 Market/Data API만 사용한다.
- 실시간 Web MCP는 Live Research Case에서만 허용하고 Historical Replay·Backtest에서는 차단한다.
- 검색 결과 Snippet은 Evidence가 아니며 Validator 통과 전 Fact Claim에 사용할 수 없다.
- Playwright MCP는 JavaScript·버튼·탭이 필요한 검증된 상위 URL에만 사용하고 로그인 Profile,
  Broker·DB Secret, 내부망, 다운로드와 파일 실행을 차단한다.

Web Search Queue의 반복 SLO 위반, RES-08의 Citation·Index 업무 지연 또는 전문 외국어·정책
Coverage 공백이 두 평가 주기 이상 확인될 때만 조건부 `RES-10 Web Intelligence Researcher`를
채용한다. 신설 시 RES-10은 URL 발견만, RES-08은 `VERIFIED_EVIDENCE` 승격만 담당한다.

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
