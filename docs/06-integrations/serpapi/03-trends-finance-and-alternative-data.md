# SerpApi 트렌드·금융·대체데이터 가이드

## 1. 역할 분리

| 데이터 | 기준 Source | SerpApi 역할 |
|---|---|---|
| 국내 실시간 가격·체결·호가 | LS증권 Open API | 사용하지 않음 |
| 거래소 일별 통계·Reference | KRX Open API | 사용하지 않음 |
| 검색 관심도 | Google Trends 계열 | Attention Feature 후보 |
| 해외 종목·지수·통화·선물 탐색 | 계약 Market Data Vendor 필요 | Google Finance로 후보·표시 정보 교차 확인 |
| 소비·가격·광고·채용·지역 수요 | 계약과 가설 검증 후 결정 | E-commerce·Ads·Jobs·Maps 계열 P2 |

## 2. Google Trends API

[공식 Google Trends API 문서](https://serpapi.com/google-trends-api)의 Endpoint:

```http
GET /search.json?engine=google_trends&q=반도체&geo=KR&hl=ko&data_type=TIMESERIES&date=today+3-m HTTP/1.1
Host: serpapi.com
```

### 2.1 파라미터

| 파라미터 | 규칙 |
|---|---|
| `q` | Query 또는 Topic ID. 최대 5개 비교는 일부 `data_type`에서만 가능, 각 Query 최대 100자 |
| `geo` | 지역. 미지정 시 Worldwide |
| `hl` | 언어 |
| `region` | `COUNTRY`, `REGION`, `DMA`, `CITY`; Region Chart에만 적용 |
| `data_type` | `TIMESERIES`, `GEO_MAP`, `GEO_MAP_0`, `RELATED_TOPICS`, `RELATED_QUERIES` |
| `tz` | 분 단위 UTC Offset. 서울은 실행 시 공식 규칙과 DST 여부를 계산해 설정 |
| `cat` | Google Trends Category |
| `gprop` | Web 기본, `images`, `news`, `froogle`, `youtube` |
| `date` | `now 1-H`, `now 4-H`, `now 1-d`, `now 7-d`, `today 1-m`, `today 3-m`, `today 12-m`, `today 5-y`, `all`, Custom Range |
| `csv` | CSV 형태 결과 배열 요청 |
| `include_low_search_volume` | 지역 Chart의 낮은 검색량 지역 포함 |

`TIMESERIES`와 비교 지역은 최대 5개 Query를 받을 수 있지만, Related Query·Topic과 단일 지역 관심도는 한 Query만 사용한다. Topic ID는 [Google Trends Autocomplete API](https://serpapi.com/google-trends-autocomplete)로 찾을 수 있다.

### 2.2 응답

대표 결과:

- `interest_over_time.timeline_data`
- 각 시점의 `timestamp`, `date`, `values[].query`, `values[].extracted_value`
- `interest_by_region`
- `compared_breakdown_by_region`
- `related_queries.rising`, `related_queries.top`
- `related_topics`

검색 관심도 값은 절대 검색량이나 가격이 아니다. 같은 `q`, `geo`, `date`, `gprop`, `cat`과 Query 묶음을 유지해야 비교 가능한 Feature가 된다.

### 2.3 Feature 규칙

| Feature | 계산 | 사용 |
|---|---|---|
| `attention_level` | `extracted_value` | 현재 관심 수준 |
| `attention_delta` | 최근 값 - 기준 Window 평균 | 관심 변화 |
| `attention_zscore` | Rolling Window 표준화 | 비정상 관심 감지 |
| `related_query_rise` | Rising Query 증가율 | Narrative 확장 후보 |
| `regional_dispersion` | 지역별 관심 분산 | 지역 집중도 |

투자 Signal에 사용하려면 다음을 검증한다.

- Query Alias 변경에 따른 History 단절
- 저검색량 Query의 결측과 Sampling Noise
- 뉴스 발생 뒤 검색량이 따라오는 후행성
- 국가·언어·Device와 Category 편향
- 동일 사건에 대한 여러 유사 Query의 중복
- Point-in-Time Snapshot 재현 가능성

## 3. Trending Now API

[공식 Trending Now API](https://serpapi.com/google-trends-trending-now):

```http
GET /search.json?engine=google_trends_trending_now&geo=KR&hours=24&only_active=true&hl=ko HTTP/1.1
Host: serpapi.com
```

| 파라미터 | 값 |
|---|---|
| `geo` | 공식 문서상 필수이며 빈 값은 US 기본 |
| `hours` | `4`, `24`, `48`, `168` |
| `category_id` | Trending Now 전용 Category |
| `only_active` | 현재 활성 Trend만 필터 |
| `hl` | 언어 |

`trending_searches` 대표 필드:

- `query`
- `start_timestamp`, `end_timestamp`
- `active`
- `search_volume`
- `increase_percentage`
- `categories`
- `trend_breakdown`
- `news_page_token`

`news_page_token`은 관련 뉴스 탐색 후보로 사용한다. Trend가 종목 Alias와 일치해도 즉시 주문하지 않고 Entity Link와 공시·뉴스·시장 Event 교차 검증을 통과시킨다.

## 4. Google Finance API

[공식 Google Finance API](https://serpapi.com/google-finance-api):

```http
GET /search.json?engine=google_finance&q=GOOGL:NASDAQ&window=1D&hl=en HTTP/1.1
Host: serpapi.com
```

| 파라미터 | 규칙 |
|---|---|
| `q` | 필수. 주식, 지수, 펀드, 통화 또는 선물 |
| `hl` | 언어 |
| `window` | `1D`, `5D`, `1M`, `6M`, `YTD`, `1Y`, `5Y`, `MAX` |

응답은 `graph`, `summary`, `knowledge_graph`, `news_results`, `financials`, `futures_chain`, `markets`, `discover_more` 등을 포함할 수 있다.

### 채택 범위

허용:

- 해외 Ticker·Exchange 표현 탐색
- Research UI의 후보 종목 설명 보강
- 해외 Market Data Vendor 도입 전 Schema 탐색
- 독립 Market Data와 값 차이 모니터링 실험

금지:

- 국내 실시간 가격 Source
- 주문 가격과 Risk Mark
- PnL·NAV 평가 가격
- Backtest History
- Option Chain·Greeks·Margin 계산
- LS 또는 계약 Market Data 장애 시 자동 Failover

Google Finance의 페이지 구조와 데이터 Coverage는 변경될 수 있고, 공식 Google Finance Markets API 문서도 최근 업데이트로 결과가 제한됐다고 안내한다.

## 5. Google Finance Markets API

[공식 Markets API](https://serpapi.com/google-finance-markets)는 현재 `engine=google_finance_markets`와 `trend=indexes`만 지원한다고 안내한다.

따라서 HgFinance에서는 보류한다.

- Market Breadth 기준 Source로 사용하지 않는다.
- 시장 목록의 변화 탐색에만 제한적으로 평가한다.
- LS/KRX/계약 Vendor와 Coverage가 겹치면 도입하지 않는다.

## 6. 조건부 대체데이터 엔진

SerpApi 전체 카탈로그에는 검색 관심도 외에도 여러 대체데이터 후보가 있다. API가 존재한다는 이유만으로 전략 Feature로 채택하지 않는다.

| 계열 | 가설 예시 | 선행 조건 |
|---|---|---|
| Google Shopping, Amazon, Walmart, eBay | 제품 가격·품절·리뷰 변화가 매출을 선행 | 상품-상장사 Mapping, 지역·판매자 편향, 장기 History와 상업 이용권 |
| Google Ads Transparency | 광고 집행 변화가 마케팅 강도를 반영 | 광고주 Entity 정확도, 캠페인 중복, 광고 국가·기간 |
| Google Jobs | 채용 공고 변화가 사업 확장·축소를 반영 | 공고 중복·재게시 제거, 직무 분류, 회사 Entity |
| Google Maps·Reviews, Yelp, Tripadvisor | 점포 수·평점·리뷰 변화가 오프라인 수요를 반영 | 점포 ID Version, 리뷰 조작·Selection Bias, 위치 Coverage |
| App Store·Google Play | 순위·리뷰 변화가 디지털 제품 수요를 반영 | App-상장사 Mapping, 국가별 Store, 순위 Snapshot |
| Flights·Hotels | 여행 수요와 가격 변화 | 날짜·노선·재고 조건 고정, 계절성과 환율 |
| YouTube·Short Videos | 제품·브랜드 Narrative 확산 | 채널 신뢰도, 조회수 조작, 게시시각과 Entity |

도입 Gate:

1. 경제적 가설과 Failure Condition을 먼저 작성한다.
2. 최소 6~12개월 Point-in-Time History를 확보할 수 있는지 확인한다.
3. 호출 비용과 Instrument Coverage를 산출한다.
4. 저장·가공·모델 입력·사용자 표시 권한을 확인한다.
5. 단순 가격·Momentum Factor 대비 Out-of-Sample 개선을 검증한다.
6. 개선이 사라지면 Collector와 Feature를 제거할 수 있어야 한다.

## 7. 저장 모델

### 7.1 Search Interest Metadata

Supabase:

```text
research.search_topics
  topic_id
  provider
  external_topic_id
  query_text
  language
  entity_ids
  valid_from
  valid_to
  query_template_version
```

### 7.2 Interest Time Series

TimescaleDB 후보:

```text
research.search_interest_observations
  observed_bucket
  topic_id
  engine
  geo
  data_type
  gprop
  category_id
  window_start
  window_end
  value
  search_volume
  increase_percentage
  source_observed_at
  request_hash
  raw_object_uri
```

Unique Key 후보:

```text
topic_id
+ engine
+ geo
+ data_type
+ gprop
+ category_id
+ window_start
+ window_end
+ observed_bucket
```

Window가 다른 요청의 `value`를 같은 시계열로 이어 붙이지 않는다. Query Template 변경은 `topic_id` 또는 Version을 새로 만든다.

## 8. 수집 주기

| 데이터 | 초기 주기 | 비용 통제 |
|---|---|---|
| Trending Now | 장중 15~30분 | 승인 국가·Category만, Active 우선 |
| 핵심 Company/Theme Trends | 일 1~4회 | Universe 전체가 아니라 승인 Query Set |
| Related Queries/Topics | 일 1회 또는 Event | Query당 1회, 결과 변화 시만 정규화 |
| Scholar·Patent 연계 Trend | 주 1회 | Research Watchlist만 |
| Google Finance | On-demand | UI·Research 탐색용 제한 예산 |
| E-commerce 대체데이터 | 연구 실험 주기 | ADR과 Strategy Experiment가 있을 때만 |

## 9. Agent 사용 계약

Agent에게 허용할 Tool:

```text
get_attention_series(topic_id, as_of, window, geo)
get_trending_topics(as_of, geo, category, limit)
compare_attention_topics(topic_ids, as_of, fixed_window)
```

Agent에게 금지할 Tool:

```text
get_google_finance_price_for_order(...)
run_arbitrary_serpapi_engine(...)
set_no_cache_and_poll_forever(...)
```

Agent 답변에는 Query Version, 지역, Window, 관측시각과 Source를 표시한다. 관심도는 “검색 관심 증가”라고 표현하고 “매출 증가”나 “가격 상승 확정”으로 바꾸지 않는다.
