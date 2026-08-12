# KRX Data Marketplace Open API 전체 참조

> [KRX 공식 서비스 목록](https://openapi.krx.co.kr/contents/OPP/INFO/service/OPPINFO004.cmd)과 31개 상세 명세를 2026-07-30 17:10:36 +09:00 에 수집한 HgFinance 개발용 참조입니다. 최신 API 계약·이용승인·약관은 KRX 원문을 우선합니다.

## 수집 결과

| 항목 | 결과 |
|---|---:|
| 서비스 분류 | 7개 |
| API | 31개 |
| 요청 인자 필드 | 31개 |
| 응답 필드 | 427개 |

## 전체 API 지도

| 경로 | 분류 | API 수 | 상세 문서 |
|---|---|---:|---|
| `idx` | 지수 | 5 | [전체 요청·응답 계약](01-index.md) |
| `sto` | 주식 | 8 | [전체 요청·응답 계약](02-stock.md) |
| `etp` | 증권상품 | 3 | [전체 요청·응답 계약](03-securities-products.md) |
| `bon` | 채권 | 3 | [전체 요청·응답 계약](04-bond.md) |
| `drv` | 파생상품 | 6 | [전체 요청·응답 계약](05-derivatives.md) |
| `gen` | 일반상품 | 3 | [전체 요청·응답 계약](06-general-commodities.md) |
| `esg` | ESG | 3 | [전체 요청·응답 계약](07-esg.md) |

## 공통 호출 계약

- 방식: `GET`
- 승인 API 기본 경로: `https://data-dbg.krx.co.kr/svc/apis/{category}/{api_id}`
- 샘플 API 기본 경로: `https://data-dbg.krx.co.kr/svc/sample/apis/{category}/{api_id}`
- 인증: HTTP 요청 헤더 `AUTH_KEY: {issued-key}`
- 응답: 접미사가 없으면 JSON, `.json`은 JSON, `.xml`은 XML
- Query: 각 상세 문서의 요청 인자를 URL Query String으로 전달한다.
- JSON 응답: 명세의 Output Block 이름을 최상위 키로 사용하고 행 배열을 반환한다.

```http
GET /svc/apis/sto/stk_bydd_trd?basDd=20260102 HTTP/1.1
Host: data-dbg.krx.co.kr
AUTH_KEY: {issued-key}
```

## 이용 절차

1. Data Marketplace 계정을 만들고 인증키를 신청한다.
2. 상세 화면의 샘플 기능과 개발 명세로 계약을 확인한다.
3. 필요한 API마다 이용 기간과 목적을 지정해 활용 신청한다.
4. 관리자 승인 후 발급 키로 승인 API 경로를 호출한다.

인증키 발급과 개별 API 활용 승인은 별도 단계다. 문서에 엔드포인트가 공개되어 있어도 승인 전 운영 호출 권한이 생기는 것은 아니다.

## 약관·출시 게이트

> 아래 내용은 [2025년 12월 26일 시행 국문 약관](https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO002.jsp)의 구현 영향 요약이다. 법률 자문이 아니며, 출시 전 최신 약관과 별도 데이터 계약을 확인한다.

| 약관 항목 | 현재 공개 조건 | HgFinance 처리 |
|---|---|---|
| 이용 목적 | 비상업적 목적만 허용 | 유료·상업 서비스에는 그대로 사용하지 않고 별도 상업 이용 계약을 확보한다. |
| 제3자 제공 | KRX 제공 정보를 제3자에게 제공할 수 없음 | 사용자 화면·API·다운로드로 원 데이터를 재배포하지 않는다. |
| 표시 의무 | 화면에 한국거래소 통계정보 사용 사실 표시 | KRX 파생 화면과 리포트에 Source Attribution을 넣는다. |
| 호출 제한 | 키당 일 10,000회 이하 | 일일 예산, 캐시, 중복제거와 차단기를 둔다. |
| 키 유효기간 | 발급일부터 1년, 연장 가능 | 만료 30일 전 운영 알림과 갱신 Runbook을 둔다. |
| 장기 미사용 | 12개월 미사용 키는 삭제될 수 있음 | 활성 키 상태와 마지막 성공 호출을 감시한다. |
| 계약 종료 | 종료 후 제공 정보 이용 불가 | 데이터 사용권 만료와 보존·삭제 정책을 계약에 맞춘다. |

따라서 이 공개 API는 연구·내부 검증용 Source로는 유용하지만, 개인 헤지펀드 서비스의 상업적 Production Source로 자동 승인된 것으로 간주하면 안 된다. 상업 이용, 내부 모델 학습, 결과 노출, 파생지표 제공과 재배포 범위를 KRX와 별도로 확인하는 것을 Production Launch Gate로 둔다.

## HgFinance 적용 원칙

KRX Open API는 LS증권 WebSocket 실시간 Feed를 대체하지 않는다. 거래소 공식 일별 통계, 종목 기본정보, 파생상품·채권·일반상품과 ESG Reference를 보강하는 EOD Data Source다.

```text
KRX Open API
  -> Research Collector
  -> Raw JSON/XML Archive
  -> Schema Validation / Idempotent Upsert
  -> Supabase Metadata + TimescaleDB EOD Series
  -> Research API / Quant Dataset
  -> Agentic RAG Evidence Metadata
```

### 사용 우선순위

| 우선순위 | 데이터 | 활용 |
|---|---|---|
| P0 | 유가증권·코스닥·코넥스 종목기본정보 | Instrument Master와 `stock_code` 검증 |
| P0 | 주식·ETF·ETN·ELW 일별매매정보 | LS 수집 데이터 EOD 대사와 백필 |
| P1 | 지수·선물·옵션 일별정보 | 벤치마크, 파생 Feature와 전략 검증 |
| P1 | 채권·금·석유·배출권 | Cross-asset Regime Feature |
| P2 | ESG 지수·증권상품·사회책임투자채권 | ESG Universe와 리서치 메타데이터 |

### 수집 주기

| 데이터 | 권장 시작 주기 | 방식 |
|---|---|---|
| 종목기본정보 | 거래일 장 마감 후 1회 | 기준일자 단위 전체 Snapshot과 변경분 upsert |
| 일별매매정보 | 거래일 장 마감 후 지연을 두고 1회 | 날짜별 증분 수집, 다음 날 누락 재확인 |
| 지수·파생·채권·일반상품 | 거래일 장 마감 후 1회 | 시장별 워터마크와 재시도 |
| ESG | 거래일 또는 주 1회 | 변경 빈도를 관측한 뒤 주기 조정 |

### 저장 모델

| 저장소 | 역할 | 대표 키 |
|---|---|---|
| Object Storage | 원본 JSON/XML과 수집 Manifest | `category/api_id/bas_dd/content_hash` |
| `research.krx_instruments` | 주식·ETF·ETN·ELW 종목 기준정보 | `market + isu_cd + valid_from` |
| TimescaleDB `market.krx_daily_stats` | OHLCV·거래대금·시가총액 일별 시계열 | `bas_dd + market + isu_cd` |
| TimescaleDB `market.krx_derivative_daily` | 선물·옵션 일별 시계열 | `bas_dd + isu_cd` |
| `research.krx_reference` | 지수·채권·상품·ESG Reference | `api_id + natural_key + valid_from` |
| `research.source_runs` | 호출 예산, 워터마크, 상태와 오류 | `source + api_id + run_id` |

### 품질·중복 관리

1. `api_id + 요청 인자`를 정렬해 `request_hash`를 만든다.
2. 원문 `content_hash`가 같으면 중복 정규화를 건너뛴다.
3. 날짜·시장·종목의 자연키로 멱등 upsert하되 이전 원문은 보존한다.
4. LS EOD 집계와 KRX 일별 통계를 대사하고 차이는 품질 Finding으로 남긴다.
5. 숫자 필드가 문자열로 제공될 수 있으므로 명세의 형식과 단위를 기준으로 Decimal 변환한다.
6. 공식 문서에는 통합 오류 코드 표가 없으므로 승인 환경의 HTTP 상태와 오류 Payload를 Fixture로 축적한다.

### Agentic RAG 계약

- Agent는 KRX를 직접 호출하지 않고 Research API와 Quant Dataset을 사용한다.
- 숫자 계산은 구조화 테이블에서 수행하고, RAG에는 Source·기준일·API ID·수집시각을 Evidence로 넣는다.
- 약관상 제3자 제공 금지를 고려해 원 응답을 사용자에게 그대로 노출하는 Tool을 만들지 않는다.
- 모델 학습·파인튜닝·임베딩 이용 가능 범위는 별도 계약 확인 전 허용하지 않는다.

## 재수집

저장소 루트에서 다음 명령을 실행한다.

```powershell
.\scripts\collect_krx_openapi_docs.ps1
```
