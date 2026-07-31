# SerpApi 뉴스·웹·AI 검색 가이드

## 1. 사용 목적

SerpApi의 뉴스와 웹 검색은 투자 사실의 원장이 아니라 다음 단계의 입력이다.

1. 새로운 사건과 원출처 URL을 발견한다.
2. 여러 검색 엔진에서 Coverage를 교차 확인한다.
3. 기업·정책기관·거래소·언론사 Allowlist의 문서를 찾는다.
4. 검색 결과를 Entity와 Instrument에 연결한다.
5. 원출처 수집과 License·Evidence QA를 통과시킨다.

검색 Snippet을 기사 전문처럼 취급하거나 AI 검색 답변을 사실로 바로 인용하지 않는다.

## 2. 엔진 선택표

| API | Endpoint의 `engine` | 핵심 Query | 우선순위 | HgFinance 역할 |
|---|---|---|---|---|
| [Google News](https://serpapi.com/google-news-api) | `google_news` | `q` | P1 | 국내외 뉴스 Discovery와 Story Coverage |
| [Naver Search](https://serpapi.com/naver-search-api) | `naver` | `query`, `where=news` | P1 | 한국어 뉴스와 국내 웹 Coverage |
| [Google Search](https://serpapi.com/search-api) | `google` | `q` | P1 | 기업 IR·기관 발표·원출처 발견 |
| [Bing News](https://serpapi.com/bing-news-api) | `bing_news` | `q` | P2 | 영문권 뉴스 Coverage 교차 확인 |
| [DuckDuckGo News](https://serpapi.com/duckduckgo-news-api) | `duckduckgo_news` | `q` | P2 | 다른 Ranking 기반의 누락 탐지 |
| [Search Index](https://serpapi.com/search-index-api) | 공식 문서 확인 | 엔진별 문서 확인 | P2 | LLM-first Web Index의 Recall 평가 |
| [Google AI Mode](https://serpapi.com/google-ai-mode-api) | `google_ai_mode` | `q` | P2 | 질문 확장과 참조 후보 발견 |
| [Google AI Overview](https://serpapi.com/google-ai-overview-api) | `google_ai_overview` | `page_token` | 보류 | Google Search가 반환한 Overview Token의 후속 조회 |

## 3. Google News API

### 3.1 요청

```http
GET /search.json?engine=google_news&q=반도체+수출&hl=ko&gl=kr HTTP/1.1
Host: serpapi.com
```

| 파라미터 | 규칙 |
|---|---|
| `q` | 선택. 일반 Google News 검색어와 `site:`, `when:` 등을 사용할 수 있음 |
| `gl` | 국가 코드. 한국 Query는 `kr`를 명시 |
| `hl` | 언어 코드. 한국어는 `ko` |
| `topic_token` | Business, Technology 같은 Topic 탐색 |
| `kgmid` | 특정 Knowledge Graph Entity. 단독 사용 |
| `publication_token` | 특정 언론사 결과 |
| `section_token` | Topic 또는 Publication의 하위 Section |
| `story_token` | 한 Story의 Full Coverage |
| `so` | `0` 관련도, `1` 날짜순 |

`q`는 Advanced Token 파라미터와 함께 사용할 수 없다. Query 검색과 Topic·Publication·Story 탐색을 다른 Pydantic Model로 분리한다.

### 3.2 주요 응답

`news_results`의 대표 필드:

- `position`
- `title`
- `source.name`
- `source.authors`
- `link`
- `date`
- `iso_date`
- `thumbnail`
- `stories`
- `highlight`
- 후속 탐색용 Topic·Publication·Story Token 또는 SerpApi Link

`iso_date`가 있으면 UTC Timestamp로 파싱하고, 없거나 상대 시간만 있으면 `published_at_confidence`를 낮춘다. 검색 결과의 날짜가 실제 기사 수정시각인지 최초 게시시각인지 원출처에서 다시 확인한다.

### 3.3 수집 Pattern

```text
Query Template Search
  -> news_results
  -> Story Token 발견 시 Full Coverage 1회
  -> Canonical URL
  -> 원출처 Domain Allowlist 확인
  -> 원문 또는 계약 Vendor Document와 Match
  -> Story Cluster
```

Story Token을 반복적으로 무제한 확장하지 않는다. 하나의 사건에 대해 최대 Page·Token 수와 종료 조건을 Source Registry에 둔다.

## 4. Naver Search API

### 4.1 요청

```http
GET /search.json?engine=naver&query=삼성전자&where=news&sort_by=1&period=1h HTTP/1.1
Host: serpapi.com
```

| 파라미터 | 규칙 |
|---|---|
| `query` | 필수. Naver 검색어 |
| `where` | `nexearch`, `web`, `video`, `news`, `image` |
| `page` | 1부터 시작하는 페이지 번호 |
| `start` | 결과 Offset. `page`와 동시에 임의 계산하지 않음 |
| `num` | 공식 문서상 Image 검색에만 최대 100 적용 |
| `sort_by` | News: `0` 관련도, `1` 최신, `2` 오래된 순 |
| `period` | `all`, `1h`, `2h`~`6h`, `1d`, `1w`, `1m`, `3m`, `6m`, `1y`, Custom Range |
| `device` | `desktop`, `tablet`, `mobile` |

Naver는 Google 계열과 검색어 파라미터 이름이 다르고 Pagination 공식도 `where`에 따라 다르다. Collector는 `page`를 우선 사용하고, 응답의 다음 페이지 Link가 있으면 거기서 필요한 Token·Offset을 파싱한다.

### 4.2 활용

- 국내 상장사와 제품명 Alias의 뉴스 Coverage
- 한국 정책·규제·산업 키워드의 최신 결과
- Naver News와 일반 Web 결과의 Source URL 비교
- Google News에서 놓친 국내 언론·블로그·영상 후보 탐색

Naver 검색 결과가 Naver 내부 Link와 원언론사 Link를 함께 줄 수 있으므로 Redirect를 해제한 Canonical 원출처 URL을 별도 저장한다.

## 5. Google Search API

### 5.1 핵심 파라미터

| 범주 | 파라미터 | 규칙 |
|---|---|---|
| Query | `q` | `site:`, `inurl:`, `intitle:` 등 공식 검색 연산자 사용 가능 |
| 위치 | `location`, `uule`, `lat`, `lon`, `radius` | 상호 배타 조건을 엔진 Model로 검증 |
| 지역화 | `google_domain`, `gl`, `hl`, `cr`, `lr` | 국가·언어를 명시해 재현성 확보 |
| 유형 | `tbm` | 기본 Web, `nws` News, `vid` Video, `pts` Patents 등 |
| Page | `start` | 0, 10, 20 형태의 Offset |
| Device | `device` | `desktop`, `tablet`, `mobile` |

HgFinance Query Template 예시:

```text
site:company-domain.com investor relations earnings
site:go.kr 반도체 지원 정책
site:sec.gov issuer-name 8-k
site:exchange-domain.example trading halt issuer-name
```

검색 결과의 `organic_results`, `knowledge_graph`, `answer_box`, `top_stories` 등은 후보 생성에 사용한다. 실제 Evidence는 공식 Domain 문서를 다시 수집해 만든다.

## 6. Bing과 DuckDuckGo

### 6.1 Bing News

| 파라미터 | 규칙 |
|---|---|
| `q` | 필수 |
| `mkt` | `en-US` 같은 언어-국가 시장. `cc`와 동시 사용 금지 |
| `cc` | ISO 국가 코드 |
| `first` | 결과 Offset, 기본 1 |
| `count` | 결과 수 제안값 |
| `qft` | 시간 범위와 날짜순. 예: 최근 1시간·24시간·7일·30일 |
| `safeSearch` | `Off`, `Moderate`, `Strict` |

특정 Provider 장애 시 자동 대체하는 Failover가 아니라, 동일 Query Set에 대한 Recall·중복·지연 평가용이다.

### 6.2 DuckDuckGo News

| 파라미터 | 규칙 |
|---|---|
| `q` | 필수, 최대 500자 |
| `kl` | `us-en`, `kr-kr` 같은 Region 값은 공식 지원 목록 확인 |
| `df` | `d`, `w`, `m` |
| `start` | Offset |
| `m` | 1~100 결과 요청 |
| `safe` | `1` Strict, `-1` Moderate, `-2` Off |

공식 문서는 높은 Offset에서 중복과 결과 수 변동 가능성을 안내한다. 자체 URL·Title Hash와 Story Cluster를 반드시 적용한다.

## 7. AI Mode와 AI Overview

### 7.1 Google AI Mode

`engine=google_ai_mode`, `q`가 필수다. 응답에는 구조화된 `text_blocks`, 재구성 Markdown과 `references`가 포함될 수 있다.

허용:

- 사용자의 질문을 검색 Query 후보로 확장
- 놓친 Source Domain과 검색어 발견
- Reference URL을 원출처 수집 Queue에 추가
- 정형 답변과 일반 웹 검색의 Coverage 차이 평가

금지:

- 생성된 `reconstructed_markdown`을 공식 Research Document로 저장
- AI 답변 문장을 출처 확인 없이 Investment Case Evidence로 인용
- 답변의 수치·날짜를 Trading Signal에 직접 연결
- Prompt에 비공개 Portfolio, 계좌, 주문 또는 개인정보 전달

### 7.2 Google AI Overview

`engine=google_ai_overview`는 일반 Google Search 응답의 `ai_overview.page_token`을 사용한다. 공식 문서는 Token이 약 1분 안에 만료될 수 있다고 안내한다.

따라서 일반 Polling·Backfill Workflow에 적합하지 않다. 사용한다면 Google Search와 즉시 이어지는 단일 Job에서만 호출하며, 결과는 `DISCOVERY_ONLY`로 표시한다.

## 8. Canonical Result Mapping

```json
{
  "provider": "serpapi",
  "engine": "google_news",
  "search_id": "serp-search-id",
  "request_hash": "sha256:...",
  "result_type": "news",
  "rank": 1,
  "title": "...",
  "snippet": null,
  "source_name": "...",
  "source_url": "https://publisher.example/article",
  "canonical_url": "https://publisher.example/article",
  "published_at": "2026-07-31T01:10:00Z",
  "observed_at": "2026-07-31T01:11:20Z",
  "evidence_status": "DISCOVERED",
  "raw_result_hash": "sha256:..."
}
```

필수 구분:

- `search_id`: SerpApi 검색 실행
- `result_id`: 검색 결과 항목
- `document_id`: 원출처를 실제 수집해 생성한 Research Document
- `story_cluster_id`: 동일 사건 묶음
- `evidence_status`: Agent 인용 가능 여부

## 9. 중복 제거

1. Tracking Parameter를 제거하고 Canonical URL을 만든다.
2. URL Hash로 Exact Duplicate를 찾는다.
3. 정규화된 제목과 Source·게시시각으로 Syndication 후보를 만든다.
4. MinHash·Embedding은 후보 축소 뒤 사용한다.
5. 같은 기사가 여러 검색 엔진에 나타나도 `document_id`는 하나만 만든다.
6. 검색 순위와 최초 관측 엔진은 별도 `discovery_occurrence`로 보존한다.

## 10. Evidence 승격 규칙

```text
DISCOVERED
  -> PRIMARY_SOURCE_FETCHED
  -> LICENSE_CHECKED
  -> PARSED
  -> ENTITY_LINKED
  -> QA_PASSED
  -> CITABLE
```

다음은 `CITABLE`이 될 수 없다.

- 검색 Snippet만 있는 결과
- 원문이 삭제되거나 접근할 수 없는 결과
- 게시시각 또는 Source가 불명확한 결과
- AI Mode·AI Overview의 생성 문장
- 본문 사용권이 없는 기사 전문
- Entity Confidence가 종목 Trigger 기준보다 낮은 결과

## 11. 서비스 운영 지침

- 종목별 무차별 Polling 대신 Sector·Theme Query와 Event-driven 종목 Query를 결합한다.
- Query Template, Locale, Provider와 Page 수를 Version 관리한다.
- Provider별 결과 수, 고유 URL 수, 원출처 도달률, 중복률과 게시 지연을 비교한다.
- Google·Naver 결과가 같은 사실을 보여도 독립 출처 두 개로 잘못 계산하지 않는다.
- SerpApi는 X Filtered Stream을 대체하지 않는다. 검색 결과의 Social Link는 승인된 X API Watchlist와 별도로 검증한다.
- 기사 본문은 SerpApi가 아니라 해당 언론사·계약 Vendor의 저장·Embedding 권한을 기준으로 처리한다.
