# SerpApi 학술·특허·영상 리서치 가이드

## 1. 사용 목적

학술·특허·영상 API는 단기 가격 Feed가 아니라 전략 연구와 산업 변화 탐지에 사용한다.

- 새로운 Factor, Portfolio 구성과 Risk 방법론 조사
- 기술·산업 Topic의 연구 강도와 특허 활동 탐색
- 기업 설명회·전문가 인터뷰·정책 영상 후보 발견
- 논문·특허·영상의 원문과 인용 관계 연결
- Hermes가 과거 Research Case와 새로운 자료를 함께 검색할 수 있는 Evidence 후보 생성

검색 결과 Metadata와 Snippet만으로 논문의 결론, 특허의 법적 유효성 또는 영상 발언을 확정하지 않는다.

## 2. Google Scholar API

[공식 Google Scholar API](https://serpapi.com/google-scholar-api):

```http
GET /search.json?engine=google_scholar&q=market+microstructure&as_ylo=2024&hl=en&num=20 HTTP/1.1
Host: serpapi.com
```

### 2.1 요청 파라미터

| 파라미터 | 규칙 |
|---|---|
| `q` | 기본 필수. `author:`, `source:` Helper 사용 가능 |
| `cites` | 특정 논문을 인용한 문서 검색. 사용 시 `q`는 선택 |
| `cluster` | 같은 논문의 모든 Version. `q`, `cites`와 동시 사용 금지 |
| `as_ylo`, `as_yhi` | 연도 범위 |
| `scisbd` | 최근 1년 Date Sort. `1` Abstract만, `2` 전체, `0` 관련도 |
| `hl` | UI 언어 |
| `lr` | `lang_en|lang_ko` 같은 결과 언어 제한 |
| `start` | 0, 10, 20 형태 Offset |
| `num` | 1~20, 기본 10 |
| `as_sdt` | 특허 포함 또는 미국 Case Law Search |
| `filter` | Similar/Omitted Result Filter |
| `as_vis` | Citation 결과 제외 여부 |
| `as_rr` | Review Article만 표시 |

### 2.2 주요 응답

`organic_results` 대표 필드:

- `result_id`
- `title`
- `link`
- `snippet`
- `publication_info.summary`
- `publication_info.authors`
- `inline_links.cited_by.cites_id`
- `inline_links.versions`
- `inline_links.serpapi_cite_link`

논문의 안정 식별자는 DOI를 우선하고, DOI가 없으면 `result_id`, 정규화 제목, 저자, 연도와 Source URL을 조합한다. Scholar `result_id`만 전사 Canonical Work ID로 사용하지 않는다.

### 2.3 Author API

[Google Scholar Author API](https://serpapi.com/google-scholar-author-api)는 `engine=google_scholar_author`와 `author_id`를 사용한다. 저자의 Article, Citation, Cited By와 Co-author 정보를 조사할 수 있다.

Watchlist는 이름 검색으로 자동 확정하지 않는다.

- 동명이인 분리
- 소속과 연구 분야 확인
- Author ID 승인
- 이름·소속 변경 History
- 회사·산업·연구 Topic Relation

## 3. Google Patents API

[공식 Google Patents API](https://serpapi.com/google-patents-api):

```http
GET /search.json?engine=google_patents&q=(semiconductor)+OR+(chip)&after=filing:20240101&sort=new&num=100 HTTP/1.1
Host: serpapi.com
```

### 3.1 요청 파라미터

| 파라미터 | 규칙 |
|---|---|
| `q` | 선택. 복수 검색식은 세미콜론으로 구분 가능 |
| `page` | 1부터 시작 |
| `num` | 10~100 |
| `sort` | 관련도 기본, `new`, `old` |
| `clustered` | `true`면 Classification Group |
| `dups` | Family 기본, `language`는 Publication 기준 |
| `patents` | Patent 결과 포함, 기본 `true` |
| `scholar` | Scholar 결과 포함, 기본 `false` |
| `before`, `after` | `priority:YYYYMMDD`, `filing:YYYYMMDD`, `publication:YYYYMMDD` |

### 3.2 주요 응답

`organic_results` 대표 필드:

- `patent_id`
- `publication_number`
- `title`
- `snippet`
- `priority_date`
- `filing_date`
- `publication_date`
- `grant_date`
- `inventor`
- `assignee`
- `language`
- `patent_link`
- `pdf`
- `cpc`, `cpc_description`

[Google Patents Details API](https://serpapi.com/google-patents-details-api)는 `engine=google_patents_details`와 `patent_id`로 발명자, 권리자, Citation, Family, 유사 문서와 Legal Event 등을 추가 조회할 수 있다.

### 3.3 HgFinance 활용

| Use Case | 결과 |
|---|---|
| 산업 기술 Radar | Topic별 출원·공개 변화와 주요 Assignee |
| 회사 혁신 Activity | Issuer와 Assignee Mapping 뒤 Filing Count·CPC 변화 |
| 경쟁사 Landscape | CPC·Citation·Patent Family Network |
| Event Research | 특허 양도·공개·Grant 후보를 원출처와 교차 검증 |

특허 수가 많다고 기술 우위나 기업가치가 자동으로 높아지는 것은 아니다. Family 중복, 국가별 공개, 법적 상태, Assignee 변경과 출원-공개 지연을 반영한다.

## 4. YouTube Search API

[공식 YouTube Search API](https://serpapi.com/youtube-search-api):

```http
GET /search.json?engine=youtube&search_query=company+earnings+call&gl=kr&hl=ko HTTP/1.1
Host: serpapi.com
```

| 파라미터 | 규칙 |
|---|---|
| `search_query` | 필수 |
| `gl` | 국가 |
| `hl` | 언어 |
| `sp` | 연속 Pagination Token 또는 Upload Date·해상도 등 Filter |

`video_results` 대표 필드:

- `video_id`
- `title`
- `link`
- `channel.name`
- `published_date`
- `views`
- `length`
- `description`
- `thumbnail`
- `serpapi_link`

`published_date`가 상대 시간일 수 있으므로 Video Details에서 절대 게시시각을 다시 확인한다.

## 5. YouTube Video와 Transcript

### 5.1 Video API

[YouTube Video API](https://serpapi.com/youtube-video-api)는 `engine=youtube_video`, `v={video_id}`를 사용한다. 제목, 채널, 조회수, 게시일, 설명, Chapter, 관련 영상, 댓글과 Reply Token을 반환할 수 있다.

댓글은 신뢰할 수 있는 기업 사실이 아니라 Audience Reaction 데이터다. 조작·봇·Selection Bias가 크므로 P2 연구 외에는 사용하지 않는다.

### 5.2 Transcript API

[YouTube Video Transcript API](https://serpapi.com/youtube-video-transcript):

```http
GET /search.json?engine=youtube_video_transcript&v={video_id}&language_code=ko HTTP/1.1
Host: serpapi.com
```

| 파라미터 | 규칙 |
|---|---|
| `v` | 필수 Video ID |
| `language_code` | 선택. `ko`, `en`, `en-US` 등. 요청 언어가 없으면 다른 언어가 반환될 수 있음 |

응답은 Transcript Snippet, 시작·종료 시간, Chapter와 언어 정보를 포함할 수 있다. 요청 언어와 실제 반환 언어를 별도 필드로 저장한다.

## 6. Canonical Research Mapping

### 6.1 Academic Work

```json
{
  "work_id": "doi:10.xxxx/example",
  "source": "google_scholar_via_serpapi",
  "source_result_id": "...",
  "title": "...",
  "authors": ["..."],
  "publication_year": 2026,
  "venue": "...",
  "doi": "10.xxxx/example",
  "source_url": "https://publisher.example/paper",
  "citation_count_observed": 123,
  "citation_count_observed_at": "2026-07-31T09:00:00Z",
  "evidence_status": "DISCOVERED"
}
```

Citation Count는 시간에 따라 변하는 관측값이므로 Work Metadata를 덮어쓰지 않고 관측 시계열로 저장한다.

### 6.2 Patent

```json
{
  "patent_id": "patent/US1234567B1/en",
  "publication_number": "US1234567B1",
  "title": "...",
  "assignees": ["..."],
  "inventors": ["..."],
  "priority_date": "2024-01-01",
  "filing_date": "2024-06-01",
  "publication_date": "2026-01-01",
  "cpc_codes": ["G06Q40/04"],
  "issuer_ids": [],
  "observed_at": "2026-07-31T09:00:00Z"
}
```

### 6.3 Video Transcript Document

```json
{
  "document_type": "VIDEO_TRANSCRIPT",
  "external_id": "youtube:{video_id}:{language_code}",
  "title": "...",
  "channel_id": "...",
  "source_url": "https://www.youtube.com/watch?v=...",
  "published_at": "...",
  "observed_at": "...",
  "requested_language": "ko",
  "returned_language": "en",
  "transcript_storage_allowed": false,
  "evidence_status": "DISCOVERED"
}
```

## 7. RAG 적용 원칙

| 자료 | 검색 결과 사용 | 원문 사용 |
|---|---|---|
| Scholar | 제목·저자·연도·Citation 후보 | Publisher, 저자 공개본 또는 계약 DB의 이용권 확인 |
| Patent | 공개 번호·날짜·Assignee·CPC 후보 | 공식 Patent 원문과 법적 상태 확인 |
| YouTube | 제목·채널·Video ID 후보 | Transcript·설명·댓글의 저장·Embedding·인용 권한 확인 |

Agentic RAG 규칙:

- Search Result Snippet은 `DISCOVERY_ONLY`.
- 논문 Claim은 원문 Page·Section 또는 허용된 Abstract 위치를 인용한다.
- 특허 Claim은 공개 번호, 국가, 날짜와 Claim/Description 위치를 구분한다.
- 영상 인용은 Video ID, Timestamp, 언어와 Transcript 유형을 표시한다.
- 자동 생성 자막은 `transcript_quality=AUTOGENERATED`로 표시하고 숫자·고유명사를 QA한다.
- 투자자·전문가 영상은 Persona의 권위가 아니라 검증 가능한 주장 단위로 처리한다.

## 8. 수집 주기

| 대상 | 권장 주기 | 범위 |
|---|---|---|
| 핵심 Research Query | 주 1회 | 승인된 Strategy Research Topic |
| Scholar Author Watchlist | 월 1회 | 승인된 연구자 |
| Patent Assignee·CPC Watchlist | 주 1회 | Universe와 핵심 산업 |
| 기업 공식 YouTube Channel | 15~60분 또는 Event | 실적·IR·제품 발표 |
| Transcript | 신규 승인 영상 발견 시 1회 | Video ID·언어별 |
| Citation Count | 월 1회 | 연구 평가 대상 Work |

## 9. 품질과 법적 Gate

- Scholar Result가 논문 전문 사용권을 부여한다고 해석하지 않는다.
- DOI, Publisher URL과 공개 License를 확인한다.
- Patent의 법적 상태는 투자 결론 전에 해당 관할 원문으로 확인한다.
- YouTube Transcript, 설명과 댓글은 Platform·저작권자의 권리를 검토한다.
- 전문 저장이 금지되면 Metadata, Source URL과 허용된 짧은 Snippet만 저장한다.
- 삭제·비공개·수정된 영상은 RAG Index와 Evidence 상태에 반영한다.
- 학술·특허·영상 지표의 전략 기여도는 Point-in-Time Backtest와 비용 포함 OOS 검증을 통과해야 한다.
