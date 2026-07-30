# OpenDART 지분공시 종합정보 전체 참조

> [OpenDART 공식 개발가이드](https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS004)를 2026-07-30 12:02:21 +09:00 에 구조화한 개발용 참조입니다. 실제 연동 전 최신 계약과 예시는 공식 문서를 다시 확인합니다.

## API 목록

API 2개

| 번호 | API | API ID | 기능 |
|---:|---|---|---|
| 1 | [임원ㆍ주요주주 소유보고](#api-2019022) | `2019022` | 임원ㆍ주요주주특정증권등 소유상황보고서 내에 임원ㆍ주요주주 소유보고 정보를 제공합니다. |
| 2 | [대량보유 상황보고](#api-2019021) | `2019021` | 주식등의 대량보유상황보고서 내에 대량보유 상황보고 정보를 제공합니다. |

---

<a id="api-2019022"></a>

## 1. 임원ㆍ주요주주 소유보고

- API ID: `2019022`
- 분류 코드: `DS004`
- 기능: 임원ㆍ주요주주특정증권등 소유상황보고서 내에 임원ㆍ주요주주 소유보고 정보를 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS004&apiId=2019022)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/elestock.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/elestock.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| rcept_dt | 접수일자 | 공시 접수일자(YYYY-MM-DD) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 회사명 |
| repror | 보고자 | 보고자명 |
| isu_exctv_rgist_at | 발행 회사 관계 임원(등기여부) | 등기임원, 비등기임원 등 |
| isu_exctv_ofcps | 발행 회사 관계 임원 직위 | 대표이사, 이사, 전무 등 |
| isu_main_shrholdr | 발행 회사 관계 주요 주주 | 10%이상주주 등 |
| sp_stock_lmp_cnt | 특정 증권 등 소유 수 | 9,999,999,999 |
| sp_stock_lmp_irds_cnt | 특정 증권 등 소유 증감 수 | 9,999,999,999 |
| sp_stock_lmp_rate | 특정 증권 등 소유 비율 | 0.00 |
| sp_stock_lmp_irds_rate | 특정 증권 등 소유 증감 비율 | 0.00 |

---

<a id="api-2019021"></a>

## 2. 대량보유 상황보고

- API ID: `2019021`
- 분류 코드: `DS004`
- 기능: 주식등의 대량보유상황보고서 내에 대량보유 상황보고 정보를 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS004&apiId=2019021)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/majorstock.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/majorstock.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| rcept_dt | 접수일자 | 공시 접수일자(YYYYMMDD) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사의 종목명(상장사) 또는 법인명(기타법인) |
| report_tp | 보고구분 | 주식등의 대량보유상황 보고구분 |
| repror | 대표보고자 | 대표보고자 |
| stkqy | 보유주식등의 수 | 보유주식등의 수 |
| stkqy_irds | 보유주식등의 증감 | 보유주식등의 증감 |
| stkrt | 보유비율 | 보유비율 |
| stkrt_irds | 보유비율 증감 | 보유비율 증감 |
| ctr_stkqy | 주요체결 주식등의 수 | 주요체결 주식등의 수 |
| ctr_stkrt | 주요체결 보유비율 | 주요체결 보유비율 |
| report_resn | 보고사유 | 보고사유 |

---
