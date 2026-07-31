# SerpApi 공통 API와 운영 가이드

## 1. 공통 호출 계약

대부분의 Search API는 다음 엔드포인트와 `engine` 파라미터를 공유한다.

```http
GET /search.json?engine={engine}&... HTTP/1.1
Host: serpapi.com
```

| 파라미터 | 필수 | 의미 | HgFinance 규칙 |
|---|---|---|---|
| `engine` | 엔진별 상이 | 사용할 검색 엔진 | Source Registry의 Allowlist 값만 허용 |
| `api_key` | 필수 | SerpApi 비밀키 | Secret으로만 주입하고 URL·Log·Trace에서 제거 |
| `output` | 선택 | `json` 기본, 일부 엔진은 `html` 지원 | Collector는 JSON만 정상 경로로 사용 |
| `no_cache` | 선택 | `true`면 SerpApi Cache를 우회 | Freshness가 필요한 승인 Job에만 사용 |
| `async` | 선택 | 요청을 제출하고 Search Archive에서 나중에 회수 | 대량 Backfill에만 사용 |
| `zero_trace` | 선택 | Enterprise 전용 비저장 Mode | 계약 확인 후 민감 Query에만 검토 |
| `json_restrictor` | 선택 | 반환 필드 제한 | 엔진 Fixture와 SDK 지원을 확인한 뒤 Payload 최적화 |

공통 파라미터가 존재해도 검색어 이름, 지역, 날짜, 페이지 방식은 엔진마다 다르다. 예를 들어 Google은 `q`, Naver는 `query`, YouTube는 `search_query`를 사용한다. 하나의 자유 형식 Dictionary를 전사에 노출하지 말고 엔진별 Pydantic Model로 검증한다.

## 2. 인증과 Secret

환경 변수 이름은 `SERPAPI_KEY`로 통일한다.

```python
import os
import serpapi

client = serpapi.Client(
    api_key=os.environ["SERPAPI_KEY"],
    timeout=15,
)

result = client.search({
    "engine": "google_news",
    "q": "한국 반도체",
    "hl": "ko",
    "gl": "kr",
})
```

[공식 Python Integration](https://serpapi.com/integrations/python)은 `pip install serpapi`와 `serpapi.Client`를 권장한다. 과거 `google-search-results` Package와 이름이 비슷하므로 Lockfile에서 배포 Package를 명시적으로 확인한다.

운영 원칙:

1. Key를 Query Parameter에 넣어 호출하더라도 Application Log에는 Parameter 전체 URL을 남기지 않는다.
2. OpenTelemetry의 HTTP URL Attribute에서 `api_key`를 Redact한다.
3. Key는 Collector Runtime에만 주입한다.
4. Frontend, Hermes Profile, LangGraph State와 Agent Memory에는 Key를 넣지 않는다.
5. 유출이 의심되면 즉시 Rotation하고 Search 사용량을 Account API로 확인한다.

## 3. Cache와 Freshness

[Google Search API 공통 설명](https://serpapi.com/search-api)에 따르면 모든 파라미터가 같은 요청은 최대 1시간 Cache가 사용될 수 있다. Cache Hit 요청은 월 검색량에 포함되지 않는다.

| Job | 권장 Cache 정책 | 이유 |
|---|---|---|
| 긴급 뉴스·공시 보강 | `no_cache=true` 후보 | 1시간 이내 변화가 중요 |
| 5~15분 News Poll | Query별 최소 간격과 비용을 평가해 선택 | Freshness와 검색 비용의 균형 |
| 일별 Scholar·Patent Watchlist | Cache 허용 | 즉시성이 낮음 |
| Historical Backfill | Cache 허용 또는 `async=true` | 중복 비용 절감 |
| Google Trends 정기 Snapshot | 같은 Window 재호출은 Cache 허용 | 비교 가능한 Snapshot 유지 |

`no_cache`와 `async`는 함께 사용하지 않는다. Cache를 우회한다고 원출처가 즉시 갱신된다는 보장은 없으므로 `observed_at`, 검색 결과의 게시시각과 원출처 최초 관측시각을 따로 기록한다.

## 4. 비동기 검색과 Search Archive

`async=true` 요청은 HTTP 연결을 오래 유지하지 않고 검색을 제출한다. 응답의 `search_metadata.id`를 저장한 뒤 [Search Archive API](https://serpapi.com/search-archive-api)로 결과를 조회한다.

```text
SUBMITTED
  -> QUEUED
  -> PROCESSING
  -> SUCCESS | ERROR
```

```http
GET /searches/{search_id}.json?api_key={SERPAPI_KEY} HTTP/1.1
Host: serpapi.com
```

Search Archive의 공식 보관 조회 기간은 검색 완료 후 최대 31일이다. 이는 HgFinance의 장기 Archive가 아니다. 사용권이 허용된 응답은 수집 직후 자체 Object Storage에 저장하고 `content_hash`, `search_id`, `collected_at`을 연결한다.

비동기 Worker 규칙:

- `search_id`를 Job의 외부 식별자로 저장한다.
- `Queued`와 `Processing`은 지수 Backoff로 Poll한다.
- 최대 대기시간을 넘으면 `PARTIAL` 또는 `FAILED_TIMEOUT`으로 끝낸다.
- 재실행 전에 동일 `request_hash`의 진행 중 Job이 있는지 확인한다.
- Archive `410 Gone`은 재시도로 복구하지 않고 만료 Finding을 남긴다.

## 5. Account와 Locations Extra API

### Account API

[Account API](https://serpapi.com/account-api)는 무료이며 월간 사용량, 남은 검색 수, 갱신일과 시간당 처리 한도를 제공한다.

```http
GET /account.json?api_key={SERPAPI_KEY} HTTP/1.1
Host: serpapi.com
```

운영 Metric:

- `serpapi_searches_left`
- `serpapi_month_usage`
- `serpapi_this_hour_searches`
- `serpapi_hourly_limit`
- `serpapi_quota_utilization_ratio`
- `serpapi_days_to_renewal`

권장 차단기:

| 상태 | 처리 |
|---|---|
| 남은 월 Quota 30% 미만 | P2 Job 중지, 경고 |
| 남은 월 Quota 10% 미만 | P1 정기 Job 축소, 긴급 Query 예산 보존 |
| 시간당 사용량 80% 이상 | 신규 Backfill 일시 중지 |
| Account 비활성 | 모든 호출 차단, Secret과 결제 상태 점검 |

### Locations API

[Locations API](https://serpapi.com/locations-api)는 무료이며 `GET /locations.json?q={text}&limit={1..10}`으로 지원 지역을 찾는다. 응답의 `canonical_name` 또는 Location ID를 지원 엔진의 `location`에 사용한다.

지역은 Query 의미의 일부다. `Seoul,South Korea`, 언어, 국가와 Device가 달라지면 별도 `request_hash`로 관리한다.

## 6. 상태와 오류 처리

[공식 상태·오류 문서](https://serpapi.com/api-status-and-error-codes)는 일반 HTTP 상태와 `search_metadata.status`를 함께 사용한다. `Success`라도 결과가 비어 있을 수 있다.

| HTTP | 의미 | 자동 재시도 | 처리 |
|---:|---|---|---|
| `200` | 요청 처리 | 조건부 | `search_metadata.status`, `error`, 결과 상태를 추가 확인 |
| `400` | 누락·잘못된 파라미터 | 아니오 | Schema 또는 Query Template 수정 |
| `401` | 잘못된 Key | 아니오 | Secret Rotation과 Incident |
| `403` | 계정 권한 없음 | 아니오 | 계정 상태·계약 확인 |
| `404` | Resource 없음 | 보통 아니오 | Token·ID와 엔진 계약 확인 |
| `410` | Archive 만료 | 아니오 | 장기 저장 누락 Finding |
| `429` | 시간당 한도 또는 월 Quota 초과 | 조건부 | Account API로 원인 판별 후 Backoff 또는 차단 |
| `500`, `503` | SerpApi 서버 오류 | 예 | Jitter를 포함한 제한 재시도 |

검색 상태:

- `Queued`: 처리 대기
- `Processing`: 처리 중
- `Success`: 처리 성공. 빈 결과일 수 있음
- `Error`: 검색 처리 실패

`HTTP 200 + Success + empty result`를 장애로 재시도하지 않는다. `search_information.*_state`, 최상위 `error`, 결과 배열 크기를 함께 저장해 정상 공백과 Parser 누락을 구분한다.

## 7. Request Hash와 중복 방지

동일 요청의 정의:

```text
provider
+ engine
+ normalized engine parameters
+ localization
+ device
+ freshness policy
+ schema version
```

Hash 생성 순서:

1. `api_key`와 Runtime-only Trace 값을 제거한다.
2. 파라미터 이름을 정렬한다.
3. Boolean, 숫자와 날짜를 Canonical 문자열로 변환한다.
4. Query 문자열은 Unicode NFC로 정규화하되 대소문자·연산자·공백 의미를 임의 변경하지 않는다.
5. 지역, 언어, 국가, Device와 Page Token을 포함한다.
6. Canonical JSON에 SHA-256을 적용한다.

`request_hash`는 동일 실행 방지에 사용하고, 결과 항목 중복은 별도의 Canonical URL과 `content_hash`로 처리한다.

## 8. 비용과 호출량

[가격 페이지](https://serpapi.com/pricing)는 2026-07-31 기준 무료 250회/월·시간당 50회를 포함한 여러 Plan을 표시한다. 가격과 한도는 변동 정보이므로 코드에 고정하지 않고 Account API와 Source Registry 설정을 기준으로 운영한다.

공식 안내상 성공 검색이 월 사용량으로 계산되며 Cache, 오류와 실패 요청은 계산되지 않는다. 빈 결과라도 성공 검색이면 1회로 계산될 수 있다. 반환 결과 수가 많아도 검색 1회라는 점만 보고 무제한 페이지 수집을 설계하면 안 된다. Page별 요청은 별도 검색이 될 수 있다.

예산 식:

```text
daily_estimated_searches
  = query_templates
  * locales
  * engines
  * pages
  * polls_per_day
  * retry_adjustment
```

전 종목마다 동일 키워드를 Poll하지 않는다. 종목·회사 Alias를 Sector/Theme Query로 묶고, 시장 Event가 발생한 종목만 On-demand Query를 허용한다.

## 9. 권장 라이브러리

| 영역 | 선택 | 용도 |
|---|---|---|
| 공식 SDK | `serpapi` | 초기 통합, 공식 예제와 Contract Smoke Test |
| HTTP | `httpx` | Async Collector, Timeout, Connection Pool과 Telemetry가 더 필요할 때 |
| Schema | `pydantic` v2 | 엔진별 요청·응답 Model |
| Retry | `tenacity` | 상태별 Backoff |
| Rate | `aiolimiter` | Account/Engine/Job별 호출 예산 |
| JSON | 표준 `json` 또는 `orjson` | Raw Fixture와 Canonical Hash |
| Test | `pytest`, `pytest-asyncio`, `respx` | HTTP Fixture, 오류와 Async Poll Test |
| Metrics | OpenTelemetry, Prometheus Client | 지연·오류·Quota·결과량 |

공식 SDK와 직접 HTTP 호출을 Business Logic에서 섞지 않는다. 둘 다 `SerpSearchProvider` Adapter 뒤에 두어 교체 가능하게 한다.

## 10. 운영 체크리스트

- [ ] `SERPAPI_KEY`가 Secret Manager에 있고 Log에서 Redact되는가
- [ ] 엔진과 Query Template이 Source Registry Allowlist에 등록됐는가
- [ ] 호출 전 Account Quota Gate가 작동하는가
- [ ] `request_hash`로 Scheduler 중복 실행을 막는가
- [ ] `Success + empty`와 실제 Error를 구분하는가
- [ ] `async` Search가 31일 이전 자체 Archive에 저장되는가
- [ ] 원출처 URL·게시시각·관측시각을 모두 보존하는가
- [ ] API 응답 변경을 Golden Fixture Diff로 탐지하는가
- [ ] Agent가 Key, 자유 형식 `engine` 또는 외부 URL을 직접 전달할 수 없는가
