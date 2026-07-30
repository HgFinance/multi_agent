# OpenDART Open API 전체 참조

> [금융감독원 OpenDART 공식 개발가이드](https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS001)를 2026-07-30 12:02:21 +09:00 에 수집한 HgFinance 개발용 참조입니다. 최신 API 계약은 항상 공식 문서를 우선합니다.

## 수집 결과

| 항목 | 결과 |
|---|---:|
| 개발가이드 분류 | 6개 |
| API | 85개 |
| 요청 인자 필드 | 337개 |
| 응답 필드 | 2383개 |

## 전체 API 지도

| 코드 | 분류 | API 수 | 상세 문서 |
|---|---|---:|---|
| `DS001` | 공시정보 | 4 | [전체 요청·응답 계약](01-disclosure-information.md) |
| `DS002` | 정기보고서 주요정보 | 30 | [전체 요청·응답 계약](02-periodic-report-key-information.md) |
| `DS003` | 정기보고서 재무정보 | 7 | [전체 요청·응답 계약](03-periodic-report-financial-information.md) |
| `DS004` | 지분공시 종합정보 | 2 | [전체 요청·응답 계약](04-ownership-disclosure.md) |
| `DS005` | 주요사항보고서 주요정보 | 36 | [전체 요청·응답 계약](05-material-events.md) |
| `DS006` | 증권신고서 주요정보 | 6 | [전체 요청·응답 계약](06-securities-registration.md) |

## 공통 호출 계약

- 기본 도메인: `https://opendart.fss.or.kr`
- 인증: 쿼리 파라미터 `crtfc_key`에 발급받은 40자리 인증키를 전달한다.
- 조회 API: 대부분 `GET`이며 JSON과 XML을 제공한다.
- 파일 API: 공시 원문, 고유번호, XBRL은 ZIP 바이너리 또는 XML 파일을 반환할 수 있다.
- 회사 식별자: OpenDART `corp_code`는 8자리이며 KRX `stock_code`와 별도로 관리한다.
- 정기보고서 코드: `11011` 사업보고서, `11012` 반기보고서, `11013` 1분기보고서, `11014` 3분기보고서다.
- 공시 접수번호 `rcept_no`는 공시 원문과 정정 이력을 연결하는 핵심 식별자다.

## 메시지 코드

| 코드 | 의미 | 처리 원칙 |
|---|---|---|
| `000` | 정상 | 응답 저장 후 정규화한다. |
| `010` | 등록되지 않은 키 | 비밀값 설정을 점검하고 재시도하지 않는다. |
| `011` | 사용할 수 없는 키 | 키 상태를 확인하고 운영 알림을 발생시킨다. |
| `012` | 접근할 수 없는 IP | 허용 IP와 배포 환경을 점검한다. |
| `013` | 조회 데이터 없음 | 정상적인 빈 결과로 기록하되 요청 범위를 감사 가능하게 남긴다. |
| `014` | 파일 없음 | 접수번호와 파일 생성 상태를 확인한다. |
| `020` | 요청 제한 초과 | 지수 백오프하고 수집 일정을 늦춘다. |
| `021` | 조회 회사 수 초과 | 회사를 최대 100개 이하로 분할한다. |
| `100` | 필드 값 부적절 | 요청 검증 실패로 분류하고 자동 재시도하지 않는다. |
| `101` | 부적절한 접근 | URL과 호출 방식을 점검한다. |
| `800` | 시스템 점검 | 점검 종료 뒤 재시도한다. |
| `900` | 정의되지 않은 오류 | 제한된 횟수만 재시도하고 장애 기록을 남긴다. |
| `901` | 개인정보 보유기간 만료 키 | 계정과 키를 갱신하고 운영 알림을 발생시킨다. |

## HgFinance 적용 원칙

OpenDART는 실시간 가격 Feed가 아니라 기업 공시와 재무·지분·자본 이벤트를 제공하는 리서치 데이터 소스다. 각 에이전트가 OpenDART를 직접 반복 호출하지 않고 리서치본부 수집기가 한 번 수집해 전사 데이터 서비스로 배포한다.

```text
OpenDART API
  -> Research Collector
  -> Raw 원문/Object Storage
  -> 검증·정규화·중복제거
  -> Supabase research schema
  -> Chunk/Embedding/pgvector
  -> Research API와 Agentic RAG
```

### 수집 주기

| 데이터 | 권장 시작 주기 | 수집 방식 |
|---|---|---|
| 공시검색 | 장중 1~5분, 장외 10~30분 | 접수일과 최근 `rcept_no` 커서 기반 증분 수집 |
| 고유번호 | 일 1회 | ZIP 전체 수신 후 변경된 회사만 upsert |
| 기업개황 | 일 1회 또는 회사 변경 감지 시 | `corp_code`별 캐시 갱신 |
| 정기보고서·재무정보 | 신규 정기공시 감지 직후 | 보고연도와 보고서 코드 단위 수집 |
| 지분·주요사항·증권신고서 | 관련 공시 감지 직후 | 이벤트 API 호출 후 원문과 함께 저장 |

주기는 초기 운영값이다. OpenDART의 실제 제한과 수집 지연을 관측해 조정하며, 공식 문서의 일반적인 일일 요청 제한 수치를 보장값으로 하드코딩하지 않는다.

### 저장 모델

| 테이블 또는 저장소 | 역할 | 대표 키 |
|---|---|---|
| `research.dart_corporations` | 회사 식별자와 상장 종목 연결 | `corp_code` |
| `research.dart_filings` | 공시 메타데이터와 정정 상태 | `rcept_no` |
| `research.dart_api_snapshots` | API별 정규화 전후 응답 이력 | `endpoint + request_hash + collected_at` |
| `research.dart_financial_facts` | 재무 계정과 기간별 값 | `corp_code + bsns_year + reprt_code + fs_div + account_id` |
| `research.dart_ownership_events` | 임원·주요주주·대량보유 변화 | `corp_code + rcept_no + event_key` |
| `research.dart_material_events` | 증자·합병·소송 등 자본 이벤트 | `corp_code + rcept_no + event_type` |
| Object Storage | 공시 ZIP, XBRL, XML, 원본 JSON | `source/opendart/date/rcept_no` |
| `research.documents`와 pgvector | RAG용 청크와 임베딩 | `document_id + chunk_index + embedding_model` |

OpenDART의 운영 원장은 Supabase PostgreSQL과 Object Storage에 둔다. TimescaleDB는 LS 가격·체결·호가와 파생 시계열을 위한 저장소이므로 OpenDART 원문 저장의 기본 위치로 사용하지 않는다.

### 중복·정정·시점 관리

1. 공시 목록은 `rcept_no`로 멱등 upsert한다.
2. API 응답은 요청 파라미터를 정렬한 `request_hash`와 원문 `content_hash`를 함께 저장한다.
3. 정정공시는 이전 행을 덮어쓰지 않고 새 접수번호와 정정 관계를 보존한다.
4. `rcept_dt`와 `collected_at`을 분리해 공시 시점과 시스템 관측 시점을 모두 남긴다.
5. `013` 빈 결과도 수집 실행 기록에 남겨 누락과 정상 공백을 구분한다.
6. 파싱 실패 시 원문은 보존하고 정규화 상태만 실패로 표시해 재처리한다.

### Agentic RAG 계약

- 청크 메타데이터에는 `corp_code`, `stock_code`, `rcept_no`, 보고서 유형, 접수일, 정정 여부와 원문 위치를 넣는다.
- 검색 결과는 원문 접수번호와 수집 시점을 인용해야 하며, 정정 전 문서는 기본 검색에서 제외한다.
- 재무 숫자는 벡터 검색 결과만으로 계산하지 않고 정규화 테이블을 구조화 조회한다.
- 리서치 Agent는 Research API를 통해 조회하고 API 키와 외부 호출 권한은 Collector에만 둔다.
- 투자 판단에는 LS 시장 데이터의 기준 시각과 OpenDART 공시 관측 시각을 함께 기록한다.

## 운영 체크리스트

- `OPENDART_API_KEY`는 Secret Manager 또는 배포 플랫폼의 Secret으로 주입하고 로그에 남기지 않는다.
- 호출 타임아웃, 재시도 횟수, 지수 백오프와 일일 요청 예산을 설정한다.
- 스케줄러 중복 실행을 막는 분산 Lock과 멱등 키를 적용한다.
- 수집 지연, 오류 코드, 마지막 성공 커서, 파싱 실패율과 정정공시 처리 지연을 모니터링한다.
- API 스키마 변경은 이 수집기를 다시 실행한 뒤 Git diff로 검토한다.

## 재수집

저장소 루트에서 다음 명령을 실행한다.

```powershell
.\scripts\collect_opendart_docs.ps1
```
