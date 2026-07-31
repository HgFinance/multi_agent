# SerpApi Search Engine APIs 개발 참조

> [SerpApi 공식 Search Engine APIs 카탈로그](https://serpapi.com/search-engine-apis)와 연결된 공식 API 문서를 2026-07-31 기준으로 조사한 HgFinance 개발용 참조다. 엔진, 가격, 호출량, 응답 필드는 바뀔 수 있으므로 구현과 출시 시점에는 공식 문서와 계정 계약을 다시 확인한다.

## 문서 목적

SerpApi는 Google, Naver, Bing, DuckDuckGo, YouTube, Google Trends, Google Scholar와 Google Patents 등 여러 검색 서비스를 하나의 검색 API 계열로 제공한다. HgFinance는 이를 다음 용도로만 검토한다.

- 국내외 뉴스와 공식 웹 문서의 **발견**
- 기업·산업·정책 키워드의 검색 관심도 변화 관찰
- 논문·특허·영상 리서치 후보 발굴
- 서로 다른 검색 엔진 간 결과 교차 확인
- Search API의 사용량, 비동기 작업과 오류 상태 운영

SerpApi는 LS증권 실시간 가격 Feed, OpenDART 공시 원장, KRX 공식 통계 또는 계약형 뉴스 전문 Vendor를 대체하지 않는다.

## 핵심 결정

| 항목 | HgFinance 결정 |
|---|---|
| 실시간 가격 | 사용 금지. 국내 가격·체결·호가는 LS증권 Open API가 기준 Source다. |
| 뉴스 | Google News와 Naver Search를 Discovery Provider 후보로 평가한다. 검색 결과만으로 사실을 확정하지 않는다. |
| 웹 검색 | 기업 IR, 기관 발표와 원출처 URL을 찾는 용도로 사용한다. 검색 순위는 투자 근거가 아니다. |
| 검색 관심도 | Google Trends와 Trending Now를 Narrative·Attention Feature 후보로 평가한다. |
| 금융 검색 | Google Finance는 해외 자산 탐색과 교차 확인용 P2다. 시세 원장·백테스트·주문 판단에는 사용하지 않는다. |
| AI 검색 | Google AI Mode와 AI Overview는 질문 확장과 출처 후보 발견에만 쓴다. 생성 답변 자체는 Evidence가 아니다. |
| Agent 권한 | Hermes와 LangGraph Agent는 SerpApi Key나 임의 검색 권한을 받지 않는다. 승인된 `research-api` Tool만 사용한다. |
| 저장 | 검색 실행과 결과 Metadata는 Supabase, 관심도 시계열은 TimescaleDB 후보, 허용된 Raw JSON은 Object Storage에 둔다. |
| Production | 비용·호출량·원출처 이용권·본문 저장권·LLM 사용권을 확인한 뒤 Source Registry에서 활성화한다. |

## 문서 지도

| 문서 | 내용 |
|---|---|
| [공통 API와 운영](01-common-api-and-operations.md) | 인증, 공통 파라미터, Cache, Async, Search Archive, 오류, 비용과 보안 |
| [뉴스·웹·AI 검색](02-news-web-and-ai-search.md) | Google/Naver/Bing/DuckDuckGo 뉴스, 일반 웹 검색과 AI 검색 통제 |
| [트렌드·금융·대체데이터](03-trends-finance-and-alternative-data.md) | Google Trends, Trending Now, Google Finance와 조건부 대체데이터 |
| [학술·특허·영상](04-scholar-patents-and-video.md) | Google Scholar, Patents, YouTube와 RAG 적용 방식 |
| [전체 엔진 카탈로그](05-engine-catalog.md) | 공식 페이지의 상위 Search API와 Extra API를 제공사별로 분류한 전체 지도 |
| [HgFinance 통합 계약](06-hgfinance-integration-contract.md) | Adapter, 저장 Schema, Evidence 승격, Tool, 테스트와 단계별 구현 |

## 프로젝트 우선순위

| 우선순위 | 구현 범위 | 이유 |
|---|---|---|
| P0 | 공통 Adapter, Secret, Request Hash, Account API, 오류·Quota 계측, Fixture | 어떤 엔진을 선택해도 필요한 운영 기반 |
| P1 | Google News, Naver Search `where=news`, Google Search, Google Trends | 뉴스 Coverage, 원출처 발견과 관심도 Feature 평가 |
| P1 | Scholar, Patents, YouTube Search/Transcript의 제한된 Watchlist | 전략 연구와 산업 변화 조사 |
| P2 | Bing·DuckDuckGo 교차 검색, Search Index, Google AI Mode/Overview | Coverage 개선 효과를 Offline Eval로 입증한 뒤 도입 |
| 보류 | Google Finance를 가격 Source로 사용, 전 엔진 무차별 수집, Agent 직접 외부 호출 | 데이터 권위·비용·재현성과 통제 원칙에 어긋남 |

## 목표 흐름

```text
Approved Query Template / Watchlist
  -> SerpApi Collector Adapter
  -> Request Hash + Quota Gate
  -> Raw Response Policy Check
  -> Result Normalization
  -> URL Canonicalization + Exact/Near Dedup
  -> Primary Source Fetch or Licensed News Provider Match
  -> Entity Link + Story Cluster + Evidence QA
  -> research.documents / attention observations
  -> research-api
  -> Hermes / LangGraph Agentic RAG
```

검색 결과는 `DISCOVERED` 상태로 시작한다. 원출처를 실제로 수집하고 사용권·시점·내용을 검증한 뒤에만 Agent가 인용할 수 있는 Evidence로 승격한다.

## 공식 기준

- [Search Engine APIs 전체 카탈로그](https://serpapi.com/search-engine-apis)
- [Google Search API](https://serpapi.com/search-api)
- [Google News API](https://serpapi.com/google-news-api)
- [Naver Search API](https://serpapi.com/naver-search-api)
- [Google Trends API](https://serpapi.com/google-trends-api)
- [Google Trends Trending Now API](https://serpapi.com/google-trends-trending-now)
- [Google Scholar API](https://serpapi.com/google-scholar-api)
- [Google Patents API](https://serpapi.com/google-patents-api)
- [YouTube Search API](https://serpapi.com/youtube-search-api)
- [공식 Python Integration](https://serpapi.com/integrations/python)
- [가격과 호출량](https://serpapi.com/pricing)
- [약관](https://serpapi.com/legal)

이 문서는 공식 문서를 대체하지 않는다. 특히 검색 결과에 연결된 기사, 영상, 논문, 특허와 웹페이지의 저장·가공·임베딩·재배포 권한은 SerpApi 계약과 별도로 각 원출처의 권리를 확인해야 한다.
