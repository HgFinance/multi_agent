# MCP 조회형 정보 계층 — 결정 문서

> 상태: **초안 v1** (2026-08-13, 팀 검토 전) · 근거: 문헌 12편 + MCP 생태계 실사 + 수집기 26종 전수 대조 (5-agent 조사, 교차 검증 완료)
> 결정 취지: **"데이터를 들고 있지 않으면서 정보의 질을 살린다"** — 시세 외 외부 정보는 호출로 얻는다. 단, 용도에 따라 경계가 갈린다.

---

## 1. 결론 먼저 — 용도별 3분할

| 용도 | 방식 | 이유 (근거) |
|---|---|---|
| **① 사용자 질의 응대** (CEO ask) | **MCP 직조회** + 검증 게이트 + 인용분만 스냅샷 | 신선도 질의는 query-time이 압도적: fast-changing 정확도 12%→77% ([FreshLLMs](https://arxiv.org/abs/2310.03214)). 질의당 지식이 작아 인덱스 불필요 ([CAG](https://arxiv.org/abs/2412.15605), [Amazon AAAI 2026](https://arxiv.org/abs/2602.23368): 벡터 DB 없이 RAG 성능 90%+) |
| **② 정성 팩터** (QF-*) | **조회는 MCP, 저장은 점수** — 배치 채점 + 경량 적재 | 백분위는 같은 `as_known_at`의 **전 종목 횡단면**이 동시에 있어야 계산된다(QUALITATIVE_FACTOR_SPEC §6.4.2) — 즉석 채점이 구조적으로 불가. DART 20k/일로 전 종목×다도구 질의식 채점도 불가 |
| **③ 백테스트·감사·사후 채점** | **정식 적재 유지** — MCP 직답 재사용 금지 | 라이브 조회는 "오늘의 웹"이지 "그날의 웹"이 아니다 — 사후에 쓰는 순간 look-ahead bias. 웹 RAG는 재현 자체가 불가([Parallel.ai](https://parallel.ai/articles/how-to-build-a-rag-pipeline-with-web-search-instead-of-vector-databases)). 자본시장법 §60 기록 10년 의무 |

핵심 원칙 (합의됨): **"다시 조회해서 복원할 수 있는가?"** — 복원 가능한 외부 정보는 적재 금지, 복원 불가능한 것(우리의 판단·시점 기록)은 원장이다.

## 2. 문헌 평결 — 지지 4 + 한계 4

**방향을 지지하는 것:**

| 문헌 | 핵심 수치 |
|---|---|
| [FreshLLMs](https://arxiv.org/abs/2310.03214) (ACL 2024) | 쿼리 시점 검색 주입 시 fast-changing 질문 정확도 **12% → 77%** |
| [CAG](https://arxiv.org/abs/2412.15605) (WWW 2025) | 질의당 지식이 작으면 검색·인덱스 자체가 불필요 |
| [Amazon](https://arxiv.org/abs/2602.23368) (AAAI 2026) | 벡터 DB 없는 키워드 tool-use 에이전트가 RAG 성능 **90%+** — 갱신 잦은 KB에서 특히 유리 |
| Claude Code 선례 | 임베딩 파이프라인 제거 → grep 기반 tool-use로 전환, 프로덕션 검증 |

**반드시 대비해야 하는 한계:**

| 한계 | 수치 | 대비책 |
|---|---|---|
| 무검증 인용 정밀도 붕괴 | 일반 RAG 인용 정밀도 **2~5%** ([Self-RAG](https://arxiv.org/abs/2310.11511) 베이스라인) | Self-RAG식 인용 검증 게이트 |
| 저품질 검색 결과 삼킴 | 품질 평가기+폴백으로 **+36.6%** ([CRAG](https://arxiv.org/abs/2401.15884)) | CRAG식 평가기 + 네이버→Tavily 폴백 |
| 링크 부패 | 2013년 웹페이지 **38% 소멸** (Pew 2024) | 인용분만 append-only 스냅샷 (cache-on-cite) |
| 반복 질의 비용 역전 | 인덱싱 시 토큰 40%+ 절감 주장 (Milvus 반론) | [Self-Route](https://arxiv.org/abs/2407.16833) 라우팅(비용 39~65% 절감) + 질의 빈도 실측 후 재평가 |

금융 도메인 특유: tool-use는 아직 전문가 수준 미달([FinSearchComp](https://arxiv.org/abs/2509.13160)) — [FinToolBench](https://arxiv.org/abs/2603.08262) 방식의 자체 평가셋(도구 호출 필수 질의 50~100개)으로 실측 후 확대가 안전. 공시 탐색은 "어느 보고서 → 어느 조항" 2단계 패턴이 표준([FinAgentBench](https://arxiv.org/abs/2508.14052)).

## 3. MCP 채택 목록 (생태계 실사)

| 축 | 채택 | 실사 결과 |
|---|---|---|
| DART 공시·재무 | **[korean-dart-mcp](https://github.com/chrisryugj/korean-dart-mcp)** (chrisryugj) | ★91, v0.9.1, 도구 15종(search_disclosures·get_financials·get_xbrl·download_document…), 무료 20,000 req/일. 대안: SongHyojun0228 등 3종 |
| 한국 뉴스 | **[naver-search-mcp](https://github.com/isnow890/naver-search-mcp)** (isnow890) | ★81, v1.0.49, 앱당 25,000회/일 무료. ⚠ 제목·요약·링크만 — 본문은 fetch 단계 필요. 보조: Tavily(기존 키 사용 중) |
| 거시 (미국) | [fred-mcp-server](https://github.com/stefanoamorelli/fred-mcp-server) | 성숙, 무료 120 req/분. AGPL 주의 |
| 거시 (한국) | ✅ **ECOS 자작 완료** (2026-08-13, `external_macro.py` — search/items/series 3종, 기준금리 실측). **KOSIS 는 폐기** (재일 결정 2026-08-13: 키 재발급 부담 — 실제로 08-11 만료 실측. 소비자물가 등은 ECOS 로 조회 가능해 능력 손실 없음) | 해소 |
| 증권사 (LS) | **자작 MCP 래핑** — `fact_router`/`account_snapshot`의 브로커 직행 패턴을 에이전트 도구로 일반화 | 키는 래퍼에 격리(통합계획 6.2 유지), TTL 수십 초 휘발성 캐시로 한도 방어 |

공급망 리스크: 주력 2종이 **개인 유지보수 npm 패키지** — API 키가 서드파티 코드를 통과한다. **버전 고정 + 포크 보관 + 의존성 감사**가 도입 조건.

## 4. 수집기 26종 처분표 — 전수 대조 결과

**폐기 0 · MCP 전환 2(이미 집행) · 캐시온리드 1 · 유지 23.**

"더 내릴 수 있는 게 없다"가 조사의 결론이다. 남은 수집기는 전부 다음 중 하나가 적재 위에서만 성립하기 때문:

- **observed_at PIT**: DART `rcept_dt`는 날짜뿐(시각 없음) — "09:00 판단이 15:00 공시를 미리 봤는지"는 적재 시점 기록으로만 구분된다. 직조회는 소급 생성 불가
- **정정 revision PIT**: "그 시점에 알 수 있었던 최신 개정본" 재현 — 직조회는 항상 최신본만 본다
- **종목 연결**: 뉴스→6자리 매핑(`document_instruments`)은 우리 산출물 — MCP 응답에 없다
- **사후 채점**: packet_outcome·analyst_calibration — 그때 본 것의 저장이 전제
- **키 격리**: LS 자격을 수집기에 가두는 설계 — MCP화가 원칙 위반

| 처분 | 대상 |
|---|---|
| MCP 전환 (집행 완료) | macro(FRED·KOSIS→MCP, ECOS 자작 필요) · geopolitical(⚠ 후속 미완 — 아래 §6) |
| 캐시온리드 | document-archive: 원문은 MCP `download_document` 요청시 조회 + **조회한 문서만 청크 인덱싱**(read-through) — evidence_chunks 08-01 동결로 죽어가는 RAG 3소비자를 살리면서 용량 목적 유지 |
| 유지 (수리 3) | disclosure(중복 dedupe+페이지), ls-news(롤백), packet-outcome(주소) — 전부 수리 사안이지 폐기 근거 아님 |
| 유지 (그대로 20) | 뉴스 2·bluesky·재무 2·CA·기업정보·제한·라벨·VKOSPI·스타일·달력·시세축 5·감사 3·retention |

## 5. 최소 저장 4층 (팩터가 요구하는 것)

1. `research.qualitative_scores` 신설 — 원값+분모+백분위+**인용 좌표**(rcept_no+span+quote_hash), `unique(instrument_id, factor_key, as_known_at, peer_group)`, 행당 수백 바이트
2. 횡단면 동시 저장 — 백분위 계산의 구조적 전제 (질의식 즉석 채점 불가의 이유)
3. **forward-only** — 소급 생성·덮어쓰기 금지, 재채점은 새 행. LLM 채점기는 모델/프롬프트 버전 동봉 (LLM 자체의 lookahead 실증: 2019 어닝콜 채점에 Covid 언급 25%+, Sarkar & Vafa 2024)
4. 유지 수집분 의존 — financial_facts(QF-F/D), disclosure CORRECTED(QF-R "MCP 호출 0"), issuers.industry_code(peer_group), daily_labels(채점)

용량: peer_group 3벌을 감안해도 **수 MB/년** — 버리는 것(문서 15만 건 + 임베딩 586MB) 대비 3~4자리 작다.

## 6. 도입 순서

**1단계 (이번 주, 장 마감 후 · 전부 독립 배포 가능):**
1. `mcp_server.geopolitical_state`에 staleness 차단 — **굳은 국면을 '현재'로 서빙 중** (GEO 라벨 이틀 결측의 원인 축)
2. packet-outcome 주소 하드코딩 수리 (`MARKET_API_URL` env)
3. evidence_chunks **read-through 인덱싱** 스케줄 신설 (조회한 문서만)
4. MCP 게이트웨이에 **cache-on-cite 훅** — 응답에 실제 사용된 근거만 append-only 스냅샷(URL+본문 해시+조회시각+질의)
5. korean-dart-mcp·naver-search-mcp **버전 고정 + 포크**, 네이버 신규 키는 API HUB로

**2단계 (2~4주):** CEO 질의 입구에 Self-Route 라우팅 + CRAG 평가기·폴백 정식 개통. FinToolBench식 자체 평가셋(50~100문)으로 DART/뉴스 MCP 적시성·정합성 실측. 질의 빈도·토큰 비용 계측(Milvus 역전점 판단 근거).

**3단계 (1~2개월):** `qualitative_scores` 신설 + 배치 채점 가동 + ECOS 자작 MCP. 2단계 실측 통과 시 macro·geopolitical 수집기 파일 최종 제거. ls-news vs NAVER 병행 실측으로 주 소스 확정.

## 7. 최대 위험 2개

| 위험 | 완화 |
|---|---|
| **PIT 오염** — query-time 결과가 팩터·채점에 흘러들면 look-ahead가 조용히 생긴다. 실사례 이미 존재: geopolitical_state가 굳은 산출을 '현재'로 서빙 | 용도 경계를 **코드로** 강제: 팩터·채점 경로는 적재 평면만 읽게, MCP 직답은 질의응대 전용 격리. 사용분 자동 스냅샷(1-④). staleness 감시는 research-data-steward 몫 |
| **외부 단일점** — 개인 npm 2종 + DART 20k/일이 질의 트래픽과 경합 + 네이버 구 키 2027-06 종료 | 버전 고정+포크(1-⑤), 게이트웨이 소스별 호출 예산·rate limiter, 한도 접근 시 캐시 우선 강등, CRAG 폴백 계층, 신규 키 선제 이관 |
