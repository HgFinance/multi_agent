# 리서치본부 (Research)

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.
Hermes Profile은 `research-department`이고 직원은 독립 LangGraph Worker다. 활성 Worker·권한은 [Worker Role Boundaries](../../docs/02-engineering/WORKER_ROLE_BOUNDARIES.md), 운영 모델은 [Worker Model Matrix](../../docs/02-engineering/WORKER_MODEL_MATRIX.md)가 소유한다. Research 생성 LLM은 공용 Qwen AWQ+Hybrid Gateway만 사용하며 로컬 `Modelfile` alias는 없다.

실제 실행 상태와 재일님 2주 계획·Daily Scrum은 [실행 현황과 통합 계획 v2.2](../../docs/PROJECT_IMPLEMENTATION_STATUS.md#41-재일님-리서치본부와-퀀트백테스트본부)을 기준으로 한다.
Research와 Quant를 연결하는 목표 Graph, 계약과 논문 기반 도입 순서는
[Research-Quant Evidence-to-Strategy Framework](../../docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md)를 따른다.
리서치 산출물의 V3 계약, 소비 본부별 View, 품질 Gate와 단계별 구현 기준은
[Research Output Advancement Strategy](../../docs/02-engineering/RESEARCH_OUTPUT_ADVANCEMENT_STRATEGY.md)를 따른다.

## Mission

**리서치 본부는 방법론·근거·시장 데이터 제공 본부다.** 웹(논문·투자자 서한·실무자 글·
커뮤니티·타 분야)과 내부 증거를 수집·검증해 다른 실행면이 재현 가능하게 사용할 수 있는
근거를 제공한다. 전략 목표의 가설·코드 작성·백테스트·계보를 실제로 수행하는 주체는 이
본부가 아니라 별도 논리 소유자인 **Strategy Hermes**다.

## 소유권 경계: Strategy Hermes와 Research HQ

이 저장소의 물리 경로와 조직 소유권은 일치하지 않는 부분이 있다. `autonomous/`는
`01-research` 아래에 남아 있지만, 그 디렉터리의 논리 소유자와 실제 연구 실행자는
`strategy-hermes`다. 독립 프로필은 `strategy-hermes/`에 있고, 직접 실행되는 Hermes와
연구실 접수·저장·검증 코드는 `autonomous/`에 있다. 상세 계약은
[`autonomous/OWNERSHIP.md`](autonomous/OWNERSHIP.md)를 정본으로 한다.

| 구분 | Research HQ | Strategy Hermes |
|---|---|---|
| 주 책임 | 방법론·근거·시장 데이터 제공 | 전략 목표를 달성할 때까지 연구 |
| 전략 연구 실행 | 하지 않음 | 직접 실행되는 Hermes가 수행 |
| 연구실 쓰기 | 접수 매니페스트 외에는 하지 않음 | 가설·계획·코드·결과·실패 기억·계보 |
| 실행 경로 | `research-hermes`의 일반 리서치/증거 표면 | `strategy-hermes` 프로필 + `autonomous/` 런타임 |
| 금지된 위임 | 전략 연구를 `autonomous/runner.py`로 실행하지 않음 | `research-hermes`·`quant-hermes`·factory·주문 경로로 위임하지 않음 |

따라서 `01-research`라는 경로명은 Strategy Hermes의 조직 소유권을 의미하지 않는다.
이 경계를 정리하기 전까지는 물리적 디렉터리 이동이나 보존 코드·볼륨 삭제를 하지 않는다.

### 현행 Strategy Hermes 전략 연구 경로

논리 정본은 `strategy-hermes`다. 사용자의 자연어 목표는
`POST /ui/strategy-research/ask`로 `intake/`에 멱등 매니페스트로 등록되고, **직접 실행되는
Strategy Hermes**가 이를
`labs/<request_id>/`의 독립 연구실로 물질화합니다. 그 폴더 안에서 `objective.json`,
`hypotheses/`, `plans/`, `results/`, `agent-runs/`, `events.jsonl`과 사람이 읽는 요약 파일을
영속화하며, 재시작해도 같은 `request_id`로 이어집니다. PIT·비용·OOS·강건성·누수·실패 모드
게이트를 통과하지 못한 결과는 후보로 승격되지 않습니다. 기존 DB/팩토리 계약은 안전한 의존성
이관과 감사 증거를 위해 보존되어 있지만 이 경로에 연결되지 않습니다.

Research HQ의 수집·PIT·인용 검증 인프라는 공급자 경계로만 사용한다. 바뀐 것은 **누가
전략 연구를 소유하고 실행하느냐**다.

> 2026-08-10 재편. 이전 Mission("종목별 Research Packet 생성")은 프레임워크 자체가
> 투자판단을 내리는 구조를 전제했다. 종목 애널리스트 편제는 운영에서 내렸고, 코드는
> 감사 계보로만 남는다. 근거는
> [Research-Quant Strategy Factory Framework](../../docs/02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md) 1절.

## Owner

재일님 — [TEAM_JAEIL_RESEARCH_QUANT_GUIDE](../../docs/05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md)

## 입력·출력 계약

- 입력: 웹 방법론 소스(논문·프리프린트·투자자 서한·실무 블로그·커뮤니티·영상·타 분야 문헌),
  퀀트의 `ExperimentOutcomeV1` 기각 이력, 내부 시장 데이터(실행 가능성 판정용)
- 출력: **자율 연구 계획과 불변 결과 아티팩트** (경제적 근거, 경쟁 설명, 데이터 요구,
  반증 검사, 실패 이력, 계보) → 퀀트본부 검증 실험실
- 부가 출력: 근거 카드·방법론 메모, Holding Brief(사용자 질의 응답 —
  **자율 연구실 입력과 주문 경로에 닿지 않는다**)
- 시장 시계열 저장·조회 경계는 `repository/market_repository.py`의 `MarketDataRepository`다.
  다른 본부는 이 Repository가 아니라 실행 중인 `market-api`를 호출한다

## 직원 편제 (LLM Worker 4인)

| Worker | 역할 | 실행 |
|---|---|---|
| `methodology-scout-academic` (RES-11) | 논문·프리프린트 + **타 분야 전용(轉用)** 2렌즈 | 소집 (`scout_cycle`) |
| `methodology-scout-practitioner` (RES-12) | 투자자 서한·실무자 글 + **커뮤니티·영상** 2렌즈 | 소집 |
| `competing-explanation-worker` (RES-15) | 경쟁 설명·반증 — **기획자와 입력 격리** | 소집 (`proposal_draft`) |
| `holdings-analyst-worker` (RES-18) | 보유 종목 질의 응답 — **서비스 자리, 연구실 미연결** | 소집 (`holding_question`) |

> **2026-08-11 감축 (8 → 4).** 직원은 병렬성·맥락격리·독립성·권한격리 중 하나가
> 명확할 때만 둔다 — 본부장이 순차로 해도 되는 일은 본부장이 한다.
>
> - **스카우트 4렌즈 → 2명.** 렌즈별 병렬성은 실재하지만 주기가 달라(학술 1회/일,
>   커뮤니티 3회/일, 타분야 2회/주) 한 명이 두 렌즈를 시간 분리해 맡을 수 있다.
>   한 세션에 한 렌즈만 돌리고 어느 렌즈였는지 말한다 — 섞으면 전이 논증이 조용히
>   유추로 바뀐다.
> - **`experiment-planner` → 편집장 흡수.** 어휘 사상은 편집 판단이고, 우리 데이터로
>   답이 나오는지는 부서 맥락을 든 사람만 안다.
> - **`market-context` → 폐지.** `data_resolution.resolve()` 가 이미 커버리지·이력·
>   종목수를 실측한다. 에이전트가 서술로 반복하면 코드 판정과 경쟁한다.
> - `competing-explanation` 은 **독립성이 코드로 강제되는 유일한 워커**라 유지한다
>   (`proposal_intake.build()` 가 서명이 기획자와 같은 실행이면 발행을 거부한다).

## 상주 운영과 처리량

**"24시간 상주"는 프로세스가 살아 있다는 뜻이지 쉬지 않고 검색한다는 뜻이 아니다.**
소스의 갱신 주기가 검색 주기의 상한이다 — arXiv는 하루 한 번 발행하는데 15분마다
뒤지면 같은 것을 96번 본다. 렌즈마다 주기가 다른 이유다.

숫자는 **병목에서 거꾸로** 잡았다. 연구실의 처리량은 검색이 아니라 검증 가능한 실험이 정한다.

| 단계 | 하루 물량 | 통과율 | 담당 계층 |
|---|---:|---:|---|
| 스카우트 소집(2명 × 2렌즈) | 히트 ~238 | — | 로컬 |
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

스카우트 2인은 같은 도구를 쓰고 **렌즈만 다르다**(각자 2렌즈를 주기로 시간 분리). 서로의 결과를 보지 않고 병렬로 뒤지는 것이
이 편제의 전부다 — 독립성은 프롬프트가 아니라 입력 격리로만 만들어진다. 렌즈 4개를 2명이 맡되 **한 세션에 한 렌즈만** 돌리는 이유도 같다.

## 구성

| 경로 | 내용 | 상태 |
|---|---|---|
| `hermes/` | Research HQ의 방법론·증거 Profile 사본 (`config.yaml`, `SOUL.md`) | Research HQ 제공면 |
| **`strategy-hermes/`** | Strategy Hermes 전용 Profile (`config.yaml`, `SOUL.md`) | 현행 전략 연구 Profile |
| **`autonomous/`** | Strategy Hermes가 소유하는 접수·독립 연구실·직접 Hermes 실행·검증 인프라 | 논리 소유자: Strategy Hermes |
| **`contracts/factory_contracts.py`** | 기존 DB 계약의 감사·이관용 보존본. 새 Hermes 러너는 import하지 않는다 | 의존성 이관 전 삭제 금지 |
| **`factory/`** | 기존 제안/리드 경로의 보존본. 컨테이너·MCP에는 복사하지 않으며 새 경로와 연결하지 않는다 | 안전한 단계적 삭제 대기 |
| **`collectors/ls_unified_parser.py`** | LS 통합시세(US3/UH1) 파서. KRX+NXT 한 소켓, 수신 시각 3종 보존 | 이식 완료, **호출처 0건** |
| **`collectors/import_external_microstructure.py`** | 외부 프로젝트 호가·체결 이관. PIT 없는 구간을 `market.pit_provenance` 에 `NONE` 으로 못박는다 | 72거래일 이관 진행 |
| `contracts/market_events.py` | 정규 Market Event 계약 — `instrument_id`, 시각 규칙, `MarketTick`/`MarketQuote`, 멱등 `source_event_id`, Quarantine, Event Envelope | Sprint J0 완료 |
| `collectors/source_registry.py` | 시장 Source와 요청형 MCP Source 카탈로그, API Key 상태 판정과 라이선스 Scope 강제 | Sprint J1 기반 완료 |
| `collectors/subscription_plan.py` | 종목별 실시간 구독 계획. 18개 TR 매트릭스(주식·선물·옵션 / 국내·해외), 범위 Gate, Universe 정의 | Sprint J1 기반 완료 |
| `collectors/ls_client.py` | LS OAuth·REST 종목 Master와 영구 Instrument Mapping 주입 경계 | Sprint J1 완료 |
| `collectors/ls_realtime_adapter.py` | 국내 주식 체결·호가 Payload를 공통 Market Event로 정규화 | Sprint J1 완료 |
| `api/external_sources.py`, `api/external_macro.py` | 공시·재무·뉴스·웹·거시를 Hermes가 필요할 때만 MCP로 조회. 상주 수집·DB 적재 없음 | 운영 경계 |
| `collectors/{calendar_collector,market_breadth_collector}.py` | 거래일 관측 Calendar·시장 Breadth와 DQ | 시장 데이터 전용 |
| `repository/market_repository.py` | `MarketDataRepository` 인터페이스 + `InMemory`/`Timescale` 두 구현 | Sprint J0 완료 |
| `repository/reference_repository.py` | 레거시 Reference·Document·재무·거시 Repository 메서드. 비시장 writer는 Runtime Collector가 호출하지 않음 | 감사·마이그레이션 호환 |
| `collectors/ls_realtime_service.py` | 전 종목 LS WebSocket, 4 Socket 구독과 Timescale 적재 Runtime | Docker 실행 확인 |
| `collectors/collector_scheduler.py` | 시세·가격·거래가능성·시장 DQ·Archive만 실행하는 Batch Schedule | Docker 실행 확인 |
| `api/market_api.py`, `api/main.py` | Snapshot·Bar·Breadth·DQ·Regime·Microstructure와 Evidence 조회 | Docker 실행 확인 |
| `api/mcp_server.py`, `api/tool_gateway.py` | Hermes Tool 호출면, 허용 경로 강제와 Bearer 인증 | `research-mcp` Docker 실행 확인 |
| `agents/`, `scripts.py` | 종목분석가 편제와 Research Packet Pipeline v2 | **운영 은퇴.** 감사 계보로만 보존하며 Runtime 이미지에 넣지 않는다 |
| `evidence/` | 시장 가격 context와 결정론 Evidence helper. 문서·뉴스 DB 번들은 레거시 | 요청형 뉴스·공시는 `api/external_sources.py`가 담당 |
| `collectors/derivatives_collector.py` | KOSPI200 선물·옵션·Greeks Snapshot 수집 | 실제 적재 3,910건 확인 |
| `collectors/market_data_steward.py` | 시장 데이터 실행·신선도·DQ Gate | 시장 데이터 전용 |
| `collectors/market_archive_exporter.py`, `replay_restore_drill.py` | 검증된 Parquet Archive와 복구 Drill | 자체 점검·팀 가이드 증거 존재 |

남은 핵심 (2026-08-10 기준, 자율 연구실 관점에서 다시 매긴 순서):

1. **수집기 통합시세 교체** — `ls_realtime_adapter.py` 의 `TR_TO_KIND` 가 아직
   `S3_/K3_/H1_/HA_`(코스피·코스닥 분리)라 **NXT 를 못 받는다.** 이관한 72거래일
   이후로는 KRX 만 쌓이고, 그러면 거래소 간 가격발견 가설이 시간이 갈수록 무의미해진다.
   `ls_unified_parser.py`(US3/UH1)가 이미 있으나 타입이 달라 변환 계층이 필요하다.
2. **일봉 유니버스·이력 확장** — 350종목·634거래일로는 walk-forward 창이 0개였다(실측).
   3,925종목 × 2016~ 로 백필 중.
3. 반려된 기획안의 `REVISE` 경로 — 고쳐도 `proposal_id` 가 같아 재발행이 안 된다.
4. Redis Stream Producer, 상시 Feature/Priority Engine, 파생 연속성 검증,
   영속 Microstructure Feature, X Watchlist Collector.

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

## 스카우트의 웹 도달 수단

스카우트는 **Hermes 내장 `web` 툴셋(Web Search & Scraping)** 으로 바깥을 본다.
별도 검색 MCP를 세우지 않는다 — 2026-08-10 실측 결과 내장 툴셋이 이미 `enabled`
상태였고, 그것으로 실제 논문을 찾아왔다(NBER w14804, NY Fed Staff Report,
LSE TugOfWar, Oxford ORA, arXiv 1312.0514 — 링크 전부 HTTP 200 확인).

> **한때 MCP가 병목이라고 적혀 있었지만 사실이 아니었다.** 웹 도달 수단은 처음부터
> 있었고, 없던 것은 **스카우트 산출을 원장에 옮기는 코드**였다. 그래서 DB의 리드가
> 전부 손으로 넣은 데모였다(`model_version` 이 빈 문자열인 것이 그 증거였다).
> 방법론 근거 카드는 Research HQ의 증거면이 담당한다. 전략 목표의 연구실 생성과
> 실행은 Strategy Hermes가 담당하며, Research HQ의 프로필은 `autonomous/runner.py`를
> 전략 실행 명령으로 등록하거나 호출하지 않는다.

에이전트는 제안하고 **코드가 판정한다**:

| 검사 | 하는 일 |
|---|---|
| 링크 실측 | 실제로 접속해 본다. 다만 403·429는 봇 차단이지 부재의 증거가 아니다 — 죽은 링크(404·DNS 실패)만 버린다 |
| 메커니즘 | 없으면 성과 서술일 뿐이라 반려. 링크 확인보다 먼저 본다(버릴 것에 네트워크를 쓰지 않는다) |
| 계보 | `model_version`·`prompt_version` 이 비면 적재 거부 — 계보 없는 리드는 재현할 수 없다 |
| 중복 | 같은 소스를 다시 주우면 새 리드가 아니라 `independent_mentions` 가 오른다 |

논문 검색 MCP(`paper-search`)는 stdio 전용 서버라 Hermes stdio 클라이언트가 ~7초에
연결을 끊는 문제가 남아 `enabled: false` 다. 내장 `web` 으로 이미 논문을 찾고 있으므로
급하지 않다 — 붙인다면 이미 동작이 검증된 HTTP 통로(`research-mcp:8037`)에
`mcp-proxy` 로 얹는 쪽이 서버마다 디버깅하는 것보다 빠르다.

## 실행법

```bash
research-department chat -q '시장 미시구조로 검증 가능한 경제적 가설과 typed AST 후보를 제안해줘'

# 로컬 시장 시계열 DB (compose 프로젝트 hedgefund, 호스트 포트 5434)
docker compose up -d
docker compose ps
curl http://127.0.0.1:8035/health
curl http://127.0.0.1:8036/health
docker compose exec -T timescaledb psql -U postgres -d market -v ON_ERROR_STOP=1 \
  < timescaledb/local-dev/001_dev_roles.sql
docker compose exec -T timescaledb psql -U postgres -d market -v ON_ERROR_STOP=1 \
  < timescaledb/migrations/001_initial_market_data.sql

# 요청형 외부 정보 MCP 도구 자체 점검 (외부 호출·DB 적재 없음)
python departments/01-research/api/external_sources.py
python departments/01-research/api/external_macro.py
```

마이그레이션은 멱등하지 않다 — `create table`에 `if not exists`가 없어 비어 있지 않은 DB에
재적용하면 실패한다. 재적용이 필요하면 `docker compose down -v`로 볼륨을 비우고 다시 올린다.

## 테스트

```bash
python departments/01-research/contracts/market_events.py       # 계약 6개 영역
python departments/01-research/collectors/source_registry.py    # Registry 6개 영역 + 현황 리포트
python departments/01-research/collectors/subscription_plan.py   # 구독 계획 6개 영역 + Universe 가용성
python departments/01-research/collectors/ls_realtime_adapter.py # 체결·호가 정규화
python departments/01-research/collectors/collector_scheduler.py --check # 시장 데이터 전용 Job 경계
python departments/01-research/collectors/market_breadth_collector.py # Breadth·DQ
python departments/01-research/repository/market_repository.py   # Repository 계약 (DB 없이)
python departments/01-research/repository/market_repository.py --integration  # 실제 TimescaleDB
python departments/01-research/scripts.py                        # Research Pipeline 11개 영역
```

`--integration`은 `.env`의 `TIMESCALE_DATABASE_URL`과 살아 있는 컨테이너가 필요하다.
Raw Table은 append-only(마이그레이션의 `market.reject_raw_mutation()` 트리거)라 이전
데이터를 지울 수 없다. 그래서 실행마다 `instrument_id`와 `provider_symbol`을 새로 만들어
격리하며, 그 덕에 반복 실행이 가능하다. 불변식이 살아 있는지도 점검 항목에 포함된다.

뉴스·공시·재무·거시·소셜 정보는 상주 수집기가 아니라 Research MCP 요청 시점에만
조회한다. 조회 결과를 운영 DB의 장기 수집 테이블로 적재하지 않는다.

## Handoff

- 수집 Source의 API Key 확보 상태는 `source_registry.py`의 리포트가 기준이다. 키가 없는
  Source를 호출하면 예외가 나며, 빈 결과를 정상으로 취급하지 않는다
- NAVER·DART·ECOS·FRED·Tavily 등의 키는 Research MCP에만 주입한다. Collector Scheduler나
  `ls-realtime`에는 주입하지 않으며, 정보 조회 결과는 주문·전략 승격의 단독 근거가 될 수 없다
- 뉴스·소셜·공시·거시 정보는 MCP가 원출처와 조회 시각을 동봉해 요청형으로 반환한다.
  시장 데이터 수집 경로로 되돌리려면 별도 계약·저장용량·PIT 검토와 명시적 승인이 필요하다
- **해외주식·파생 수집은 ADR 승인 대기 중이다.** TR은 확인됐고 코드 구조도 준비됐지만
  [HEDGE_FUND_CORE_PLAN.md](../../docs/01-product/HEDGE_FUND_CORE_PLAN.md)가 "단일 주식시장"을
  전제하므로 `build_plan(approved_scopes=...)`에 명시하지 않으면 계획 생성이 거부된다
- **주식 지수 구성종목을 주는 LS TR이 없다.** 국내 KOSPI200/KOSDAQ150도 KRX 서비스 이용
  승인이 확인되기 전에는 자동 구성할 수 없어 명시적 Universe 입력이 필요하다. 미국
  NASDAQ100/S&P500/DJIA는 지수 사업자 라이선스가 없어 불가다. 파생은
  `o3101`/`o3121` 마스터로 전체 상품을 받을 수 있어 이 제약이 없다
- `references/` 이전 여부는 미결정 — [REPOSITORY_DEPARTMENT_STRUCTURE.md](../../docs/02-engineering/REPOSITORY_DEPARTMENT_STRUCTURE.md) 7절 참고
