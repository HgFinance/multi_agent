# OpenDART 정기보고서 재무정보 전체 참조

> [OpenDART 공식 개발가이드](https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS003)를 2026-07-30 12:02:21 +09:00 에 구조화한 개발용 참조입니다. 실제 연동 전 최신 계약과 예시는 공식 문서를 다시 확인합니다.

## API 목록

API 7개

| 번호 | API | API ID | 기능 |
|---:|---|---|---|
| 1 | [단일회사 주요계정](#api-2019016) | `2019016` | 상장법인(유가증권, 코스닥) 및 주요 비상장법인(사업보고서 제출대상 & IFRS 적용)이 제출한 정기보고서 내에 XBRL재무제표의 주요계정과목(재무상태표, 손익계산서)을 제공합니다. |
| 2 | [다중회사 주요계정](#api-2019017) | `2019017` | 상장법인(유가증권, 코스닥) 및 주요 비상장법인(사업보고서 제출대상 & IFRS 적용)이 제출한 정기보고서 내에 XBRL재무제표의 주요계정과목(재무상태표, 손익계산서)을 제공합니다. (대상법인 복수조회 복수조회 가능) |
| 3 | [재무제표 원본파일(XBRL)](#api-2019019) | `2019019` | 상장법인(유가증권, 코스닥) 및 주요 비상장법인(사업보고서 제출대상 & IFRS 적용)이 제출한 정기보고서 내에 XBRL재무제표의 원본파일(XBRL)을 제공합니다. |
| 4 | [다중회사 주요 재무지표](#api-2022002) | `2022002` | 상장법인(유가증권, 코스닥) 및 주요 비상장법인(사업보고서 제출대상 & IFRS 적용)이 제출한 정기보고서 내에 XBRL재무제표의 주요 재무지표를 제공합니다.(대상법인 복수조회 가능) |
| 5 | [XBRL택사노미재무제표양식](#api-2020001) | `2020001` | 금융감독원 회계포탈에서 제공하는 IFRS 기반 XBRL 재무제표 공시용 표준계정과목체계(계정과목) 을 제공합니다. |
| 6 | [단일회사 주요 재무지표](#api-2022001) | `2022001` | 상장법인(유가증권, 코스닥) 및 주요 비상장법인(사업보고서 제출대상 & IFRS 적용)이 제출한 정기보고서 내에 XBRL재무제표의 주요 재무지표를 제공합니다. |
| 7 | [단일회사 전체 재무제표](#api-2019020) | `2019020` | 상장법인(유가증권, 코스닥) 및 주요 비상장법인(사업보고서 제출대상 & IFRS 적용)이 제출한 정기보고서 내에 XBRL재무제표의 모든계정과목을 제공합니다. |

---

<a id="api-2019016"></a>

## 1. 단일회사 주요계정

- API ID: `2019016`
- 분류 코드: `DS003`
- 기능: 상장법인(유가증권, 코스닥) 및 주요 비상장법인(사업보고서 제출대상 & IFRS 적용)이 제출한 정기보고서 내에 XBRL재무제표의 주요계정과목(재무상태표, 손익계산서)을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019016)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/fnlttSinglAcnt.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/fnlttSinglAcnt.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(4) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| bsns_year | 사업 연도 | 2019 |
| stock_code | 종목 코드 | 상장회사의 종목코드(6자리) |
| reprt_code | 보고서 코드 | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |
| account_nm | 계정명 | ex) 자본총계 |
| fs_div | 개별/연결구분 | OFS:재무제표, CFS:연결재무제표 |
| fs_nm | 개별/연결명 | ex) 연결재무제표 또는 재무제표 출력 |
| sj_div | 재무제표구분 | BS:재무상태표, IS:손익계산서 |
| sj_nm | 재무제표명 | ex) 재무상태표 또는 손익계산서 출력 |
| thstrm_nm | 당기명 | ex) 제 13 기 3분기말 |
| thstrm_dt | 당기일자 | ex) 2018.09.30 현재 |
| thstrm_amount | 당기금액 | 9,999,999,999 |
| thstrm_add_amount | 당기누적금액 | 9,999,999,999 |
| frmtrm_nm | 전기명 | ex) 제 12 기말 |
| frmtrm_dt | 전기일자 | ex) 2017.01.01 ~ 2017.12.31 |
| frmtrm_amount | 전기금액 | 9,999,999,999 |
| frmtrm_add_amount | 전기누적금액 | 9,999,999,999 |
| bfefrmtrm_nm | 전전기명 | ex) 제 11 기말(※ 사업보고서의 경우에만 출력) |
| bfefrmtrm_dt | 전전기일자 | ex) 2016.12.31 현재(※ 사업보고서의 경우에만 출력) |
| bfefrmtrm_amount | 전전기금액 | 9,999,999,999(※ 사업보고서의 경우에만 출력) |
| ord | 계정과목 정렬순서 | 계정과목 정렬순서 |
| currency | 통화 단위 | 통화 단위 |

---

<a id="api-2019017"></a>

## 2. 다중회사 주요계정

- API ID: `2019017`
- 분류 코드: `DS003`
- 기능: 상장법인(유가증권, 코스닥) 및 주요 비상장법인(사업보고서 제출대상 & IFRS 적용)이 제출한 정기보고서 내에 XBRL재무제표의 주요계정과목(재무상태표, 손익계산서)을 제공합니다. (대상법인 복수조회 복수조회 가능)
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019017)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/fnlttMultiAcnt.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/fnlttMultiAcnt.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(4) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| bsns_year | 사업 연도 | 사업연도(4자리) |
| stock_code | 종목 코드 | 상장회사의 종목코드(6자리) |
| reprt_code | 보고서 코드 | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |
| account_nm | 계정명 | ex) 자본총계 |
| fs_div | 개별/연결구분 | OFS:재무제표, CFS:연결재무제표 |
| fs_nm | 개별/연결명 | ex) 연결재무제표 또는 재무제표 출력 |
| sj_div | 재무제표구분 | BS:재무상태표, IS:손익계산서 |
| sj_nm | 재무제표명 | ex) 재무상태표 또는 손익계산서 출력 |
| thstrm_nm | 당기명 | ex) 제 13 기 3분기말 |
| thstrm_dt | 당기일자 | ex) 2018.09.30 현재 |
| thstrm_amount | 당기금액 | 9,999,999,999 |
| thstrm_add_amount | 당기누적금액 | 9,999,999,999 |
| frmtrm_nm | 전기명 | ex) 제 12 기말 |
| frmtrm_dt | 전기일자 | ex) 2017.01.01 ~ 2017.12.31 |
| frmtrm_amount | 전기금액 | 9,999,999,999 |
| frmtrm_add_amount | 전기누적금액 | 9,999,999,999 |
| bfefrmtrm_nm | 전전기명 | ex) 제 11 기말(※ 사업보고서의 경우에만 출력) |
| bfefrmtrm_dt | 전전기일자 | ex) 2016.12.31 현재(※ 사업보고서의 경우에만 출력) |
| bfefrmtrm_amount | 전전기금액 | 9,999,999,999(※ 사업보고서의 경우에만 출력) |
| ord | 계정과목 정렬순서 | 계정과목 정렬순서 |
| currency | 통화 단위 | 통화 단위 |

---

<a id="api-2019019"></a>

## 3. 재무제표 원본파일(XBRL)

- API ID: `2019019`
- 분류 코드: `DS003`
- 기능: 상장법인(유가증권, 코스닥) 및 주요 비상장법인(사업보고서 제출대상 & IFRS 적용)이 제출한 정기보고서 내에 XBRL재무제표의 원본파일(XBRL)을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019019)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/fnlttXbrl.xml | UTF-8 | Zip FILE (binary) |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| rcept_no | 접수번호 | STRING(8) | Y | 접수번호 ※ 조회방법 : 공시검색API 호출 > 응답요청 값 rcept_no 추출 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |

---

<a id="api-2022002"></a>

## 4. 다중회사 주요 재무지표

- API ID: `2022002`
- 분류 코드: `DS003`
- 기능: 상장법인(유가증권, 코스닥) 및 주요 비상장법인(사업보고서 제출대상 & IFRS 적용)이 제출한 정기보고서 내에 XBRL재무제표의 주요 재무지표를 제공합니다.(대상법인 복수조회 가능)
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2022002)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/fnlttCmpnyIndx.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/fnlttCmpnyIndx.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(4) | Y | 사업연도(4자리) ※ 2023년 3분기 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |
| idx_cl_code | 지표분류코드 | STRING(7) | Y | 수익성지표 : M210000 안정성지표 : M220000 성장성지표 : M230000 활동성지표 : M240000 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| reprt_code | 보고서 코드 | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |
| bsns_year | 사업 연도 | 2023 |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| stock_code | 종목 코드 | 상장회사의 종목코드(6자리) |
| stlm_dt | 결산기준일 | YYYY-MM-DD |
| idx_cl_code | 지표분류코드 | 수익성지표 : M210000 안정성지표 : M220000 성장성지표 : M230000 활동성지표 : M240000 |
| idx_cl_nm | 지표분류명 | 수익성지표,안정성지표,성장성지표,활동성지표 |
| idx_code | 지표코드 | ex) M211000 |
| idx_nm | 지표명 | ex) 영업이익률 |
| idx_val | 지표값 | ex) 0.256 |

---

<a id="api-2020001"></a>

## 5. XBRL택사노미재무제표양식

- API ID: `2020001`
- 분류 코드: `DS003`
- 기능: 금융감독원 회계포탈에서 제공하는 IFRS 기반 XBRL 재무제표 공시용 표준계정과목체계(계정과목) 을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2020001)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/xbrlTaxonomy.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/xbrlTaxonomy.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| sj_div | 재무제표구분 | STRING(5) | Y | (※재무제표구분 참조) |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| sj_div | 재무제표구분 | 재무제표구분 |
| account_id | 계정ID | 계정 고유명칭 |
| account_nm | 계정명 | 계정명 |
| bsns_de | 기준일 | 적용 기준일 |
| label_kor | 한글 출력명 | 한글 출력명 |
| label_eng | 영문 출력명 | 영문 출력명 |
| data_tp | 데이터 유형 | ※ 데이타 유형설명 - text block : 제목 - Text : Text - yyyy-mm-dd : Date - X : Monetary Value - (X): Monetary Value(Negative) - X.XX : Decimalized Value - Shares : Number of shares (주식 수) - For each : 공시된 항목이 전후로 반복적으로 공시될 경우 사용 - 공란 : 입력 필요 없음 |
| ifrs_ref | IFRS Reference | IFRS Reference ※ 출력예시 K-IFRS 1001 문단 54 (9),K-IFRS 1007 문단 45 |

---

<a id="api-2022001"></a>

## 6. 단일회사 주요 재무지표

- API ID: `2022001`
- 분류 코드: `DS003`
- 기능: 상장법인(유가증권, 코스닥) 및 주요 비상장법인(사업보고서 제출대상 & IFRS 적용)이 제출한 정기보고서 내에 XBRL재무제표의 주요 재무지표를 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2022001)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/fnlttSinglIndx.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/fnlttSinglIndx.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(4) | Y | 사업연도(4자리) ※ 2023년 3분기 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |
| idx_cl_code | 지표분류코드 | STRING(7) | Y | 수익성지표 : M210000 안정성지표 : M220000 성장성지표 : M230000 활동성지표 : M240000 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| reprt_code | 보고서 코드 | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |
| bsns_year | 사업 연도 | 2023 |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| stock_code | 종목 코드 | 상장회사의 종목코드(6자리) |
| stlm_dt | 결산기준일 | YYYY-MM-DD |
| idx_cl_code | 지표분류코드 | 수익성지표 : M210000 안정성지표 : M220000 성장성지표 : M230000 활동성지표 : M240000 |
| idx_cl_nm | 지표분류명 | 수익성지표,안정성지표,성장성지표,활동성지표 |
| idx_code | 지표코드 | ex) M211000 |
| idx_nm | 지표명 | ex) 영업이익률 |
| idx_val | 지표값 | ex) 0.256 |

---

<a id="api-2019020"></a>

## 7. 단일회사 전체 재무제표

- API ID: `2019020`
- 분류 코드: `DS003`
- 기능: 상장법인(유가증권, 코스닥) 및 주요 비상장법인(사업보고서 제출대상 & IFRS 적용)이 제출한 정기보고서 내에 XBRL재무제표의 모든계정과목을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS003&apiId=2019020)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/fnlttSinglAcntAll.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(4) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |
| fs_div | 개별/연결구분 | STRING(3) | Y | OFS:재무제표, CFS:연결재무제표 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| reprt_code | 보고서 코드 | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |
| bsns_year | 사업 연도 | 2018 |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| sj_div | 재무제표구분 | BS : 재무상태표 IS : 손익계산서 CIS : 포괄손익계산서 CF : 현금흐름표 SCE : 자본변동표 |
| sj_nm | 재무제표명 | ex) 재무상태표 또는 손익계산서 출력 |
| account_id | 계정ID | XBRL 표준계정ID ※ 표준계정ID가 아닐경우 ""-표준계정코드 미사용-"" 표시 |
| account_nm | 계정명 | 계정명칭 ex) 자본총계 |
| account_detail | 계정상세 | ※ 자본변동표에만 출력 ex) 계정 상세명칭 예시 - 자본 [member]\|지배기업 소유주지분 - 자본 [member]\|지배기업 소유주지분\|기타포괄손익누계액 [member] |
| thstrm_nm | 당기명 | ex) 제 13 기 |
| thstrm_amount | 당기금액 | 9,999,999,999 ※ 분/반기 보고서이면서 (포괄)손익계산서 일 경우 [3개월] 금액 |
| thstrm_add_amount | 당기누적금액 | 9,999,999,999 |
| frmtrm_nm | 전기명 | ex) 제 12 기말 |
| frmtrm_amount | 전기금액 | 9,999,999,999 |
| frmtrm_q_nm | 전기명(분/반기) | ex) 제 18 기 반기 |
| frmtrm_q_amount | 전기금액(분/반기) | 9,999,999,999 ※ 분/반기 보고서이면서 (포괄)손익계산서 일 경우 [3개월] 금액 |
| frmtrm_add_amount | 전기누적금액 | 9,999,999,999 |
| bfefrmtrm_nm | 전전기명 | ex) 제 11 기말(※ 사업보고서의 경우에만 출력) |
| bfefrmtrm_amount | 전전기금액 | 9,999,999,999(※ 사업보고서의 경우에만 출력) |
| ord | 계정과목 정렬순서 | 계정과목 정렬순서 |
| currency | 통화 단위 | 통화 단위 |

---
