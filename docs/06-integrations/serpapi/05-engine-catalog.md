# SerpApi 전체 엔진 카탈로그

> 기준: [Search Engine APIs 공식 카탈로그](https://serpapi.com/search-engine-apis), 2026-07-31. 이 문서는 공식 페이지의 상위 Search API와 Extra API를 제공사별로 빠짐없이 찾을 수 있게 정리한 지도다. Organic Results, Filters, Pagination 같은 응답 Section 문서는 독립 엔진 목록에서 제외했다.

## 분류 기준

- **P1**: HgFinance가 가까운 단계에서 평가할 API
- **P2**: Offline Eval·비용·계약 Gate 뒤 검토할 API
- **보류**: 현재 서비스 범위에는 직접 필요하지 않음
- **금지 용도**: API 자체가 금지라는 뜻이 아니라 거래·위험 기준 Source로 사용하지 않는다는 뜻

## Google 계열

### P1

- [Google Search API](https://serpapi.com/search-api): 공식 웹 문서와 원출처 Discovery
- [Google News API](https://serpapi.com/google-news-api): 국내외 뉴스 Discovery
- [Google Trends API](https://serpapi.com/google-trends-api): Query·Topic 관심도
- [Google Trends Autocomplete API](https://serpapi.com/google-trends-autocomplete): Trends Topic ID 탐색
- [Google Trends Trending Now API](https://serpapi.com/google-trends-trending-now): 급상승 Topic
- [Google Scholar API](https://serpapi.com/google-scholar-api): 논문 Discovery
- [Google Scholar Author API](https://serpapi.com/google-scholar-author-api): 승인 연구자 Watchlist
- [Google Patents API](https://serpapi.com/google-patents-api): 특허 Discovery
- [Google Patents Details API](https://serpapi.com/google-patents-details-api): 특허 상세와 Citation·Legal Event

### P2

- Google Light Search API
- [Google AI Mode API](https://serpapi.com/google-ai-mode-api)
- [Google AI Overview API](https://serpapi.com/google-ai-overview-api)
- Google Ads API
- Google Ads Transparency API
- Google Autocomplete API
- Google Events API
- [Google Finance API](https://serpapi.com/google-finance-api)
- [Google Finance Markets API](https://serpapi.com/google-finance-markets)
- Google Forums API
- Google Images API
- Google Images Light API
- Google Jobs API
- Google News Light API
- Google Related Questions API
- Google Reverse Image API
- Google Scholar Case Law API
- Google Shopping API
- Google Shopping Light API
- Google Short Videos API
- Google Videos API
- Google Videos Light API

### 현재 보류

- Google Flights API
- Google Flights Autocomplete API
- Google Flights Deals API
- Google Hotels API
- Google Hotels Autocomplete API
- Google Hotels Photos API
- Google Hotels Reviews API
- Google Immersive Product API
- Google Lens API
- Google Local API
- Google Local Services API
- Google Maps API
- Google Maps Photos API
- Google Maps Autocomplete API
- Google Maps Directions API
- Google Maps Posts API
- Google Maps Reviews API
- Google Maps Contributor Reviews API
- Google Play Store API
- Google Play Games API
- Google Play Movies API
- Google Play Books API
- Google Play Product API
- Google Sports API
- Google Travel Explore API

응답 Section의 후속 API로 Google Ads Transparency Center Ad Details, Google Images Related Content, Google Jobs Listing, Google Maps Photo Meta, Google Scholar Cite, Google Shopping Filters와 Google Trends News 등이 있다. Parent API를 도입할 때만 해당 Token·ID 계약을 추가한다.

## Amazon

- Amazon Search API: P2 소비·상품 대체데이터 후보
- Amazon Product API: P2 상품 상세·가격·리뷰 후보

## Apple

- Apple App Store API: P2 App 수요 후보
- Apple App Store Reviews API: P2 사용자 반응 후보
- Apple App Store Product API: P2 App Metadata 후보
- Apple Maps API: 보류
- Apple Maps Places API: 보류

## Baidu

- Baidu Search API: P2 중국 웹 Coverage 후보
- Baidu News API: P2 중국 뉴스 Coverage 후보

## Bing

- [Bing Search API](https://serpapi.com/bing-search-api): P2 웹 Coverage 비교
- [Bing News API](https://serpapi.com/bing-news-api): P2 뉴스 Coverage 비교
- Bing Shopping API: 보류
- Bing Product API: 보류
- Bing Maps API: 보류
- Bing Reverse Image API: 보류
- Bing Videos API: P2 영상 Coverage 후보
- Bing Copilot API: 보류, 생성 답변은 Evidence 금지
- Bing Images API: 보류

## Brave

- Brave AI Mode API: 보류, 생성 답변은 Discovery만 허용

## DuckDuckGo

- [DuckDuckGo Search API](https://serpapi.com/duckduckgo-search-api): P2 웹 Coverage 비교
- [DuckDuckGo News API](https://serpapi.com/duckduckgo-news-api): P2 뉴스 Coverage 비교
- DuckDuckGo Maps API: 보류
- DuckDuckGo Light API: 보류

## eBay

- eBay Search API: P2 중고·상품 수요 후보
- eBay Product API: P2 상품 상세 후보

## Social Profile

- Facebook Profile API: 보류
- Instagram Profile API: 보류

이 두 API는 X Filtered Stream을 대체하지 않는다. Social Profile 수집은 개인정보, Platform 정책, 삭제·비공개 전파와 투자 근거 통제를 별도 검토한다.

## Naver

- [Naver Search API](https://serpapi.com/naver-search-api): P1 한국어 뉴스·웹·영상 Coverage
- Naver AI Overview API: 보류, 생성 답변은 Discovery만 허용

## OpenTable

- OpenTable Reviews API: 보류, 외식 업종 대체데이터 가설이 승인될 때 평가

## SerpApi Search Index

- [Search Index API](https://serpapi.com/search-index-api): P2 LLM-first Web Index의 Recall·Latency 평가

기존 Google/Naver 검색 대비 고유 원출처 발견률과 비용 개선이 없으면 도입하지 않는다.

## The Home Depot

- The Home Depot Search API: 보류
- The Home Depot Product API: 보류
- The Home Depot Reviews API: 보류

건설·주택 관련 대체데이터 전략 가설이 승인될 때만 P2로 전환한다.

## Tripadvisor

- Tripadvisor Search API: 보류
- Tripadvisor Place API: 보류
- Tripadvisor Reviews API: 보류

## Walmart

- Walmart Search API: P2 미국 소비·가격 후보
- Walmart Product API: P2 상품 상세 후보
- Walmart Reviews API: P2 소비자 반응 후보

## Yahoo

- Yahoo! Search API: P2 웹 Coverage 비교
- Yahoo! Videos API: 보류
- Yahoo! Images API: 보류

## Yandex

- Yandex Search API: P2 지역 Coverage 필요 시
- Yandex Images API: 보류
- Yandex Videos API: 보류

## Yelp

- Yelp Search API: 보류
- Yelp Place API: 보류
- Yelp Reviews API: P2 오프라인 수요 가설 승인 시

## YouTube

- [YouTube Search API](https://serpapi.com/youtube-search-api): P1 기업·정책·전문가 영상 Discovery
- [YouTube Video API](https://serpapi.com/youtube-video-api): P1 승인 영상 Metadata
- [YouTube Video Transcript API](https://serpapi.com/youtube-video-transcript): P1 승인 영상 Transcript 후보

## Extra APIs

- [Account API](https://serpapi.com/account-api): P0 사용량·Quota·시간당 처리 한도
- [Locations API](https://serpapi.com/locations-api): P0 지역 Canonicalization
- Pixel Position API: 보류. SEO 화면 위치 분석은 현재 투자 서비스 핵심이 아님
- [Search Archive API](https://serpapi.com/search-archive-api): P0 Async 결과 회수
- [Status and Error Codes](https://serpapi.com/api-status-and-error-codes): P0 오류·상태 계약

## 현재 채택 Set

```text
P0 operations
  account
  locations
  search archive
  status/error

P1 discovery
  google
  google_news
  naver
  google_trends
  google_trends_trending_now
  google_scholar
  google_scholar_author
  google_patents
  google_patents_details
  youtube
  youtube_video
  youtube_video_transcript

P2 evaluation
  bing
  bing_news
  duckduckgo
  duckduckgo_news
  google_ai_mode
  google_ai_overview
  search_index
  google_finance
```

실제 `engine` 문자열은 해당 공식 상세 문서의 Endpoint를 기준으로 Registry에 입력한다. 이름만 보고 문자열을 추정하지 않는다.

## 카탈로그 갱신 규칙

1. 분기 1회 공식 카탈로그와 이 목록을 비교한다.
2. 새 API가 생겨도 기본 상태는 `DISABLED`.
3. Source Owner가 Use Case, 비용, 저장권, LLM 사용권과 데이터 품질을 작성한다.
4. Offline Eval과 QA 승인을 통과하면 `EVALUATION`.
5. Production 사용권과 운영 SLO가 확인된 API만 `ACTIVE`.
6. Provider가 필드·Token·Coverage를 바꾸면 Golden Fixture Diff와 영향 전략을 점검한다.
