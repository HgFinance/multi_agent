# MCP 조회형 정보 계층 — 결정 문서

> 상태: **채택 v2** (2026-08-18) · 근거: 문헌 12편 + MCP 생태계 실사 + 수집기 전수 대조
> 결정 취지: **"데이터를 들고 있지 않으면서 정보의 질을 살린다"** — 시세 외 외부 정보는 호출로 얻는다. 단, 용도에 따라 경계가 갈린다.

---

## 1. 결론 먼저 — 용도별 3분할

| 용도 | 방식 | 이유 (근거) |
|---|---|---|
| **① 사용자 질의 응대** (CEO ask) | **MCP 직조회** + 검증 게이트 + 비영속 인용 해시 | 신선도 질의는 query-time이 압도적: fast-changing 정확도 12%→77% ([FreshLLMs](https://arxiv.org/abs/2310.03214)). 질의당 지식이 작아 인덱스 불필요 ([CAG](https://arxiv.org/abs/2412.15605), [Amazon AAAI 2026](https://arxiv.org/abs/2602.23368): 벡터 DB 없이 RAG 성능 90%+) |
| **② 가설·수식 발굴** | **MCP 조사 → 경제적 메커니즘·AST 후보** | 조사 결과는 후보 생성에 쓰되 수치 백테스트 입력으로 직접 재사용하지 않는다. 원출처·조회 시각·인용을 후보 계보에 남긴다 |
| **③ 백테스트·감사·사후 채점** | **보유 시장 데이터만 사용** — MCP 직답 재사용 금지 | 라이브 조회는 "오늘의 웹"이지 "그날의 웹"이 아니다. 사후에 수치 입력으로 쓰면 look-ahead bias가 생긴다 |

핵심 원칙 (합의됨): 외부 응답은 적재하지 않는다. 그 응답에서 파생한 경제적 가설·AST
계보·실험 결과·실패 기억만 공장 원장에 남긴다.

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
| 증권사 (LS) | **자작 MCP 래핑** — `account_snapshot`/`ls_account_stream`의 브로커 직행 패턴을 에이전트 도구로 일반화 | 키는 래퍼에 격리(통합계획 6.2 유지), TTL 수십 초 휘발성 캐시로 한도 방어 |

공급망 리스크: 주력 2종이 **개인 유지보수 npm 패키지** — API 키가 서드파티 코드를 통과한다. **버전 고정 + 포크 보관 + 의존성 감사**가 도입 조건.

## 4. 수집기 처분표

> **⚠ 최종 결정이 조사 권고를 덮었다 (재일, 2026-08-13 저녁).**
> *"MCP 쓰면 그동안 수집해서 DB 적재하려고 만들었던 수집기는 정리해야지. 미시구조·호가·체결·가격데이터 수집하는 수집기 빼고 정리해야 MCP 도입 의의가 살지."*
> 조사(§4 원안)는 PIT·정정 revision·사후 채점 근거로 폐기 0 을 권고했으나, 소유자가 트레이드오프를 인지한 상태에서 **시세·가격축만 적재 유지**로 확정했다. 그 결과:
>
> | 처분 | 대상 |
> |---|---|
> | ✂ **삭제/비활성 (08-18 집행)** | 뉴스·공시·재무·기업정보·거시·지정학·소셜·Corporate Action·capability·Research Source steward 수집기와 **news-watcher · ls-news** 서비스 |
> | 유지 (시세·가격축) | ls-realtime · breadth · derivatives · chart-daily-universe · vkospi · style-index · market-archive |
> | 유지 (시장 운영축) | universe-restrictions · label-snapshot · calendar-observed · market-data-steward |
> | 수동 복구 도구 | retention · replay/restore drill은 Runtime 이미지와 상주 Scheduler에서 제외 |
>
> 같이 수용된 결과: 정성 정보의 신규 DB 유입은 중단한다. 기존 데이터의 삭제는 별도
> 보존 결정이며 이번 변경에 포함하지 않는다. Runtime 이미지는 시장 수집 파일만 명시적으로
> 복사해 삭제된 수집기가 예전 Image Layer에서 다시 실행되지 않게 한다.

**(원안 기록 — 2026-08-13 폐기 0 권고의 근거, 현재 Runtime 정책 아님)** 당시 검토는 다음 이유로 적재 유지를 권고했다:

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

## 5. 과거 제안: 최소 저장 4층 (현재 Runtime 정책 아님)

1. `research.qualitative_scores` 신설 — 원값+분모+백분위+**인용 좌표**(rcept_no+span+quote_hash), `unique(instrument_id, factor_key, as_known_at, peer_group)`, 행당 수백 바이트
2. 횡단면 동시 저장 — 백분위 계산의 구조적 전제 (질의식 즉석 채점 불가의 이유)
3. **forward-only** — 소급 생성·덮어쓰기 금지, 재채점은 새 행. LLM 채점기는 모델/프롬프트 버전 동봉 (LLM 자체의 lookahead 실증: 2019 어닝콜 채점에 Covid 언급 25%+, Sarkar & Vafa 2024)
4. 유지 수집분 의존 — financial_facts(QF-F/D), disclosure CORRECTED(QF-R "MCP 호출 0"), issuers.industry_code(peer_group), daily_labels(채점)

용량: peer_group 3벌을 감안해도 **수 MB/년** — 버리는 것(문서 15만 건 + 임베딩 586MB) 대비 3~4자리 작다.

## 6. 과거 도입안 (08-18 정책으로 대체됨)

**1단계 (이번 주, 장 마감 후 · 전부 독립 배포 가능):**
1. ~~`mcp_server.geopolitical_state`에 staleness 차단~~ → 08-18 도구 자체를 제거했다.
2. packet-outcome 주소 하드코딩 수리 (`MARKET_API_URL` env)
3. evidence_chunks **read-through 인덱싱** 스케줄 신설 (조회한 문서만)
4. MCP 게이트웨이에 **cache-on-cite 훅** — 응답에 실제 사용된 근거만 append-only 스냅샷(URL+본문 해시+조회시각+질의)
5. korean-dart-mcp·naver-search-mcp **버전 고정 + 포크**, 네이버 신규 키는 API HUB로

**2단계 (2~4주):** CEO 질의 입구에 Self-Route 라우팅 + CRAG 평가기·폴백 정식 개통. FinToolBench식 자체 평가셋(50~100문)으로 DART/뉴스 MCP 적시성·정합성 실측. 질의 빈도·토큰 비용 계측(Milvus 역전점 판단 근거).

**3단계 (1~2개월):** `qualitative_scores` 신설 + 배치 채점 가동 + ECOS 자작 MCP. 2단계 실측 통과 시 macro·geopolitical 수집기 파일 최종 제거. ls-news vs NAVER 병행 실측으로 주 소스 확정.

## 7. 최대 위험 2개

| 위험 | 완화 |
|---|---|
| **PIT 오염** — query-time 결과가 팩터·채점에 흘러들면 look-ahead가 조용히 생긴다 | 용도 경계를 **코드로** 강제: 굳은 값을 서빙하던 `geopolitical_state`를 제거했고, MCP 직답은 질의응대와 후보 아이디어 생성에만 쓴다. 백테스트 수치 입력은 보유 시장 데이터로 제한한다 |
| **외부 단일점** — DART·NAVER 등 공급자 장애·호출 한도 | 소스별 호출 예산·rate limiter와 독립 실패 상태를 둔다. 한도 소진 시 캐시로 위장하지 않고 fail-closed 하며, 구현된 요청형 폴백만 사용한다 |
