# OpenDART 정기보고서 주요정보 전체 참조

> [OpenDART 공식 개발가이드](https://opendart.fss.or.kr/guide/main.do?apiGrpCd=DS002)를 2026-07-30 12:02:21 +09:00 에 구조화한 개발용 참조입니다. 실제 연동 전 최신 계약과 예시는 공식 문서를 다시 확인합니다.

## API 목록

API 30개

| 번호 | API | API ID | 기능 |
|---:|---|---|---|
| 1 | [주식의 총수 현황](#api-2020002) | `2020002` | 정기보고서(사업, 분기, 반기보고서) 내에 주식의총수현황을 제공합니다. |
| 2 | [자기주식 취득 및 처분 현황](#api-2019006) | `2019006` | 정기보고서(사업, 분기, 반기보고서) 내에 자기주식 취득 및 처분 현황을 제공합니다. |
| 3 | [배당에 관한 사항](#api-2019005) | `2019005` | 정기보고서(사업, 분기, 반기보고서) 내에 배당에 관한 사항을 제공합니다. |
| 4 | [증자(감자) 현황](#api-2019004) | `2019004` | 정기보고서(사업, 분기, 반기보고서) 내에 증자(감자) 현황을 제공합니다. |
| 5 | [채무증권 발행실적](#api-2020003) | `2020003` | 정기보고서(사업, 분기, 반기보고서) 내에 채무증권 발행실적을 제공합니다. |
| 6 | [기업어음증권 미상환 잔액](#api-2020004) | `2020004` | 정기보고서(사업, 분기, 반기보고서) 내에 기업어음증권 미상환 잔액을 제공합니다. |
| 7 | [단기사채 미상환 잔액](#api-2020005) | `2020005` | 정기보고서(사업, 분기, 반기보고서) 내에 단기사채 미상환 잔액을 제공합니다. |
| 8 | [회사채 미상환 잔액](#api-2020006) | `2020006` | 정기보고서(사업, 분기, 반기보고서) 내에 회사채 미상환 잔액을 제공합니다. |
| 9 | [신종자본증권 미상환 잔액](#api-2020007) | `2020007` | 정기보고서(사업, 분기, 반기보고서) 내에 신종자본증권 미상환 잔액을 제공합니다. |
| 10 | [조건부 자본증권 미상환 잔액](#api-2020008) | `2020008` | 정기보고서(사업, 분기, 반기보고서) 내에 조건부 자본증권 미상환 잔액을 제공합니다. |
| 11 | [공모자금의 사용내역](#api-2020016) | `2020016` | 정기보고서(사업, 분기, 반기보고서) 내에 공모자금의 사용내역을 제공합니다. |
| 12 | [사모자금의 사용내역](#api-2020017) | `2020017` | 정기보고서(사업, 분기, 반기보고서) 내에 사모자금의 사용내역을 제공합니다. |
| 13 | [회계감사인의 명칭 및 감사의견](#api-2020009) | `2020009` | 정기보고서(사업, 분기, 반기보고서) 내에 회계감사인의 명칭 및 감사의견을 제공합니다. |
| 14 | [감사용역체결현황](#api-2020010) | `2020010` | 정기보고서(사업, 분기, 반기보고서) 내에 감사용역체결현황을 제공합니다. |
| 15 | [회계감사인과의 비감사용역 계약체결 현황](#api-2020011) | `2020011` | 정기보고서(사업, 분기, 반기보고서) 내에 회계감사인과의 비감사용역 계약체결 현황을 제공합니다. |
| 16 | [독립(사외)이사 및 그 변동현황](#api-2020012) | `2020012` | 정기보고서(사업, 분기, 반기보고서) 내에 독립(사외)이사 및 그 변동현황을 제공합니다. |
| 17 | [최대주주 현황](#api-2019007) | `2019007` | 정기보고서(사업, 분기, 반기보고서) 내에 최대주주 현황을 제공합니다. |
| 18 | [최대주주 변동현황](#api-2019008) | `2019008` | 정기보고서(사업, 분기, 반기보고서) 내에 최대주주 변동현황을 제공합니다. |
| 19 | [소액주주 현황](#api-2019009) | `2019009` | 정기보고서(사업, 분기, 반기보고서) 내에 소액주주 현황을 제공합니다. |
| 20 | [임원 현황](#api-2019010) | `2019010` | 정기보고서(사업, 분기, 반기보고서) 내에 임원 현황을 제공합니다. |
| 21 | [직원 현황](#api-2019011) | `2019011` | 정기보고서(사업, 분기, 반기보고서) 내에 직원 현황을 제공합니다. |
| 22 | [미등기임원 보수현황](#api-2020013) | `2020013` | 정기보고서(사업, 분기, 반기보고서) 내에 미등기임원 보수현황을 제공합니다. |
| 23 | [이사·감사 전체의 보수현황(주주총회 승인금액)](#api-2020014) | `2020014` | 정기보고서(사업, 분기, 반기보고서) 내에 이사·감사 전체의 보수현황(주주총회 승인금액)을 제공합니다. |
| 24 | [이사·감사 전체의 보수현황(보수지급금액 - 이사·감사 전체)](#api-2019013) | `2019013` | 정기보고서(사업, 분기, 반기보고서) 내에 이사·감사 전체의 보수현황(보수지급금액 - 이사·감사 전체)을 제공합니다. |
| 25 | [이사·감사 전체의 보수현황(보수지급금액 - 유형별)](#api-2020015) | `2020015` | 정기보고서(사업, 분기, 반기보고서) 내에 이사·감사 전체의 보수현황(보수지급금액 - 유형별)을 제공합니다. |
| 26 | [이사·감사의 개인별 보수현황(5억원 이상)](#api-2019012) | `2019012` | 정기보고서(사업, 분기, 반기보고서) 내에 이사·감사의 개인별 보수현황(5억원 이상)을 제공합니다. ※ 2026년 4월까지 제출된 보고서해당 |
| 27 | [이사·감사의 개인별 보수현황(5억원 이상) (Ver 2.0)](#api-2026001) | `2026001` | 정기보고서(사업, 분기, 반기보고서) 내에 이사·감사의 개인별 보수현황(5억원 이상)을 제공합니다. ※ 2026년 5월 이후부터 제출된 보고서해당 |
| 28 | [개인별 보수지급 금액(5억이상 상위5인)](#api-2019014) | `2019014` | 정기보고서(사업, 분기, 반기보고서) 내에 개인별 보수지급 금액(5억이상 상위5인)을 제공합니다. ※ 2026년 4월까지 제출된 보고서해당 |
| 29 | [개인별 보수지급 금액(5억이상 상위5인) (Ver 2.0)](#api-2026002) | `2026002` | 정기보고서(사업, 분기, 반기보고서) 내에 이사·감사의 개인별 보수현황(5억원 이상)을 제공합니다. ※ 2026년 5월 이후부터 제출된 보고서해당 |
| 30 | [타법인 출자현황](#api-2019015) | `2019015` | 정기보고서(사업, 분기, 반기보고서) 내에 타법인 출자현황을 제공합니다. |

---

<a id="api-2020002"></a>

## 1. 주식의 총수 현황

- API ID: `2020002`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 주식의총수현황을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020002)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/stockTotqySttus.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/stockTotqySttus.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| se | 구분 | 구분(증권의종류, 합계, 비고) |
| isu_stock_totqy | 발행할 주식의 총수 | Ⅰ. 발행할 주식의 총수, 9,999,999,999 |
| now_to_isu_stock_totqy | 현재까지 발행한 주식의 총수 | Ⅱ. 현재까지 발행한 주식의 총수, 9,999,999,999 |
| now_to_dcrs_stock_totqy | 현재까지 감소한 주식의 총수 | Ⅲ. 현재까지 감소한 주식의 총수, 9,999,999,999 |
| redc | 감자 | Ⅲ. 현재까지 감소한 주식의 총수(1. 감자), 9,999,999,999 |
| profit_incnr | 이익소각 | Ⅲ. 현재까지 감소한 주식의 총수(2. 이익소각), 9,999,999,999 |
| rdmstk_repy | 상환주식의 상환 | Ⅲ. 현재까지 감소한 주식의 총수(3. 상환주식의 상환), 9,999,999,999 |
| etc | 기타 | Ⅲ. 현재까지 감소한 주식의 총수(4. 기타), 9,999,999,999 |
| istc_totqy | 발행주식의 총수 | Ⅳ. 발행주식의 총수 (Ⅱ-Ⅲ), 9,999,999,999 |
| tesstk_co | 자기주식수 | Ⅴ. 자기주식수, 9,999,999,999 |
| distb_stock_co | 유통주식수 | Ⅵ. 유통주식수 (Ⅳ-Ⅴ), 9,999,999,999 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2019006"></a>

## 2. 자기주식 취득 및 처분 현황

- API ID: `2019006`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 자기주식 취득 및 처분 현황을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2019006)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/tesstkAcqsDspsSttus.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/tesstkAcqsDspsSttus.xml | UTF-8 | XML |

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
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 법인명 | 법인명 |
| acqs_mth1 | 취득방법 대분류 | 배당가능이익범위 이내 취득, 기타취득, 총계 등 |
| acqs_mth2 | 취득방법 중분류 | 직접취득, 신탁계약에 의한취득, 기타취득, 총계 등 |
| acqs_mth3 | 취득방법 소분류 | 장내직접취득, 장외직접취득, 공개매수, 주식매수청구권행사, 수탁자보유물량, 현물보유량, 기타취득, 소계, 총계 등 |
| stock_knd | 주식 종류 | 보통주, 우선주 등 |
| bsis_qy | 기초 수량 | 9,999,999,999 |
| change_qy_acqs | 변동 수량 취득 | 9,999,999,999 |
| change_qy_dsps | 변동 수량 처분 | 9,999,999,999 |
| change_qy_incnr | 변동 수량 소각 | 9,999,999,999 |
| trmend_qy | 기말 수량 | 9,999,999,999 |
| rm | 비고 | 비고 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2019005"></a>

## 3. 배당에 관한 사항

- API ID: `2019005`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 배당에 관한 사항을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2019005)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/alotMatter.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/alotMatter.xml | UTF-8 | XML |

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
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 법인명 | 법인명 |
| se | 구분 | 유상증자(주주배정), 전환권행사 등 |
| stock_knd | 주식 종류 | 보통주 등 |
| thstrm | 당기 | 9,999,999,999 |
| frmtrm | 전기 | 9,999,999,999 |
| lwfr | 전전기 | 9,999,999,999 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2019004"></a>

## 4. 증자(감자) 현황

- API ID: `2019004`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 증자(감자) 현황을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2019004)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/irdsSttus.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/irdsSttus.xml | UTF-8 | XML |

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
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 법인명 | 법인명 |
| isu_dcrs_de | 주식발행 감소일자 | 주식발행 감소일자 |
| isu_dcrs_stle | 발행 감소 형태 | 발행 감소 형태 |
| isu_dcrs_stock_knd | 발행 감소 주식 종류 | 발행 감소 주식 종류 |
| isu_dcrs_qy | 발행 감소 수량 | 9,999,999,999 |
| isu_dcrs_mstvdv_fval_amount | 발행 감소 주당 액면 가액 | 9,999,999,999 |
| isu_dcrs_mstvdv_amount | 발행 감소 주당 가액 | 9,999,999,999 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2020003"></a>

## 5. 채무증권 발행실적

- API ID: `2020003`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 채무증권 발행실적을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020003)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/detScritsIsuAcmslt.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/detScritsIsuAcmslt.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| isu_cmpny | 발행회사 | 발행회사 |
| scrits_knd_nm | 증권종류 | 증권종류 |
| isu_mth_nm | 발행방법 | 발행방법 |
| isu_de | 발행일자 | 발행일자(YYYYMMDD) |
| facvalu_totamt | 권면(전자등록)총액 | 9,999,999,999 |
| intrt | 이자율 | 0.00 |
| evl_grad_instt | 평가등급(평가기관) | 평가등급(평가기관) |
| mtd | 만기일 | 만기일(YYYYMMDD) |
| repy_at | 상환여부 | 상환여부 |
| mngt_cmpny | 주관회사 | 주관회사 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2020004"></a>

## 6. 기업어음증권 미상환 잔액

- API ID: `2020004`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 기업어음증권 미상환 잔액을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020004)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/entrprsBilScritsNrdmpBlce.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/entrprsBilScritsNrdmpBlce.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| remndr_exprtn1 | 잔여만기 | 잔여만기 |
| remndr_exprtn2 | 잔여만기 | 잔여만기 |
| de10_below | 10일 이하 | 9,999,999,999 |
| de10_excess_de30_below | 10일초과 30일이하 | 9,999,999,999 |
| de30_excess_de90_below | 30일초과 90일이하 | 9,999,999,999 |
| de90_excess_de180_below | 90일초과 180일이하 | 9,999,999,999 |
| de180_excess_yy1_below | 180일초과 1년이하 | 9,999,999,999 |
| yy1_excess_yy2_below | 1년초과 2년이하 | 9,999,999,999 |
| yy2_excess_yy3_below | 2년초과 3년이하 | 9,999,999,999 |
| yy3_excess | 3년 초과 | 9,999,999,999 |
| sm | 합계 | 9,999,999,999 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2020005"></a>

## 7. 단기사채 미상환 잔액

- API ID: `2020005`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 단기사채 미상환 잔액을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020005)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/srtpdPsndbtNrdmpBlce.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/srtpdPsndbtNrdmpBlce.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| remndr_exprtn1 | 잔여만기 | 잔여만기 |
| remndr_exprtn2 | 잔여만기 | 잔여만기 |
| de10_below | 10일 이하 | 9,999,999,999 |
| de10_excess_de30_below | 10일초과 30일이하 | 9,999,999,999 |
| de30_excess_de90_below | 30일초과 90일이하 | 9,999,999,999 |
| de90_excess_de180_below | 90일초과 180일이하 | 9,999,999,999 |
| de180_excess_yy1_below | 180일초과 1년이하 | 9,999,999,999 |
| sm | 합계 | 9,999,999,999 |
| isu_lmt | 발행 한도 | 9,999,999,999 |
| remndr_lmt | 잔여 한도 | 9,999,999,999 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2020006"></a>

## 8. 회사채 미상환 잔액

- API ID: `2020006`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 회사채 미상환 잔액을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020006)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/cprndNrdmpBlce.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/cprndNrdmpBlce.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| remndr_exprtn1 | 잔여만기 | 잔여만기 |
| remndr_exprtn2 | 잔여만기 | 잔여만기 |
| yy1_below | 1년 이하 | 9,999,999,999 |
| yy1_excess_yy2_below | 1년초과 2년이하 | 9,999,999,999 |
| yy2_excess_yy3_below | 2년초과 3년이하 | 9,999,999,999 |
| yy3_excess_yy4_below | 3년초과 4년이하 | 9,999,999,999 |
| yy4_excess_yy5_below | 4년초과 5년이하 | 9,999,999,999 |
| yy5_excess_yy10_below | 5년초과 10년이하 | 9,999,999,999 |
| yy10_excess | 10년초과 | 9,999,999,999 |
| sm | 합계 | 9,999,999,999 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2020007"></a>

## 9. 신종자본증권 미상환 잔액

- API ID: `2020007`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 신종자본증권 미상환 잔액을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020007)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/newCaplScritsNrdmpBlce.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/newCaplScritsNrdmpBlce.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| remndr_exprtn1 | 잔여만기 | 잔여만기 |
| remndr_exprtn2 | 잔여만기 | 잔여만기 |
| yy1_below | 1년 이하 | 9,999,999,999 |
| yy1_excess_yy5_below | 1년초과 5년이하 | 9,999,999,999 |
| yy5_excess_yy10_below | 5년초과 10년이하 | 9,999,999,999 |
| yy10_excess_yy15_below | 10년초과 15년이하 | 9,999,999,999 |
| yy15_excess_yy20_below | 15년초과 20년이하 | 9,999,999,999 |
| yy20_excess_yy30_below | 20년초과 30년이하 | 9,999,999,999 |
| yy30_excess | 30년초과 | 9,999,999,999 |
| sm | 합계 | 9,999,999,999 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2020008"></a>

## 10. 조건부 자본증권 미상환 잔액

- API ID: `2020008`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 조건부 자본증권 미상환 잔액을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020008)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/cndlCaplScritsNrdmpBlce.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/cndlCaplScritsNrdmpBlce.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| remndr_exprtn1 | 잔여만기 | 잔여만기 |
| remndr_exprtn2 | 잔여만기 | 잔여만기 |
| yy1_below | 1년 이하 | 9,999,999,999 |
| yy1_excess_yy2_below | 1년초과 2년이하 | 9,999,999,999 |
| yy2_excess_yy3_below | 2년초과 3년이하 | 9,999,999,999 |
| yy3_excess_yy4_below | 3년초과 4년이하 | 9,999,999,999 |
| yy4_excess_yy5_below | 4년초과 5년이하 | 9,999,999,999 |
| yy5_excess_yy10_below | 5년초과 10년이하 | 9,999,999,999 |
| yy10_excess_yy20_below | 10년초과 20년이하 | 9,999,999,999 |
| yy20_excess_yy30_below | 20년초과 30년이하 | 9,999,999,999 |
| yy30_excess | 30년초과 | 9,999,999,999 |
| sm | 합계 | 9,999,999,999 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2020016"></a>

## 11. 공모자금의 사용내역

- API ID: `2020016`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 공모자금의 사용내역을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020016)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/pssrpCptalUseDtls.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/pssrpCptalUseDtls.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| se_nm | 구분 | 구분 |
| tm | 회차 | 회차 ③ 2019년 12월 9일부터 추가됨 |
| pay_de | 납입일 | 납입일 |
| pay_amount | 납입금액 | 9,999,999,999 ① 2018년 1월 18일까지 사용됨 |
| on_dclrt_cptal_use_plan | 신고서상 자금사용 계획 | 신고서상 자금사용 계획 ① 2018년 1월 18일까지 사용됨 |
| real_cptal_use_sttus | 실제 자금사용 현황 | 실제 자금사용 현황 ① 2018년 1월 18일까지 사용됨 |
| rs_cptal_use_plan_useprps | 증권신고서 등의 자금사용 계획(사용용도) | 증권신고서 등의 자금사용 계획(사용용도) ② 2018년 1월 19일부터 추가됨 |
| rs_cptal_use_plan_prcure_amount | 증권신고서 등의 자금사용 계획(조달금액) | 9,999,999,999 ② 2018년 1월 19일부터 추가됨 |
| real_cptal_use_dtls_cn | 실제 자금사용 내역(내용) | 실제 자금사용 내역(내용) ② 2018년 1월 19일부터 추가됨 |
| real_cptal_use_dtls_amount | 실제 자금사용 내역(금액) | 9,999,999,999 ② 2018년 1월 19일부터 추가됨 |
| dffrnc_occrrnc_resn | 차이발생 사유 등 | 차이발생 사유 등 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2020017"></a>

## 12. 사모자금의 사용내역

- API ID: `2020017`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 사모자금의 사용내역을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020017)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/prvsrpCptalUseDtls.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/prvsrpCptalUseDtls.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| se_nm | 구분 | 구분 |
| tm | 회차 | 회차 ③ 2019년 12월 9일부터 추가됨 |
| pay_de | 납입일 | 납입일 |
| pay_amount | 납입금액 | 9,999,999,999 ① 2018년 1월 18일까지 사용됨 |
| cptal_use_plan | 자금사용 계획 | 자금사용 계획 ① 2018년 1월 18일까지 사용됨 |
| real_cptal_use_sttus | 실제 자금사용 현황 | 실제 자금사용 현황 ① 2018년 1월 18일까지 사용됨 |
| mtrpt_cptal_use_plan_useprps | 주요사항보고서의 자금사용 계획(사용용도) | 주요사항보고서의 자금사용 계획(사용용도) ② 2018년 1월 19일부터 추가됨 |
| mtrpt_cptal_use_plan_prcure_amount | 주요사항보고서의 자금사용 계획(조달금액) | 9,999,999,999 ② 2018년 1월 19일부터 추가됨 |
| real_cptal_use_dtls_cn | 실제 자금사용 내역(내용) | 실제 자금사용 내역(내용) ② 2018년 1월 19일부터 추가됨 |
| real_cptal_use_dtls_amount | 실제 자금사용 내역(금액) | 9,999,999,999 ② 2018년 1월 19일부터 추가됨 |
| dffrnc_occrrnc_resn | 차이발생 사유 등 | 차이발생 사유 등 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2020009"></a>

## 13. 회계감사인의 명칭 및 감사의견

- API ID: `2020009`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 회계감사인의 명칭 및 감사의견을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020009)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/accnutAdtorNmNdAdtOpinion.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/accnutAdtorNmNdAdtOpinion.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| bsns_year | 사업연도 | 사업연도(당기, 전기, 전전기) |
| adtor | 감사인 | 감사인 |
| adt_opinion | 감사의견 | 감사의견 |
| adt_reprt_spcmnt_matter | 감사보고서 특기사항 | 감사보고서 특기사항 ① 2019년 12월 8일까지 사용됨 |
| emphs_matter | 강조사항 등 | 강조사항 등 ② 2019년 12월 9일부터 추가됨 |
| core_adt_matter | 핵심감사사항 | 핵심감사사항 ② 2019년 12월 9일부터 추가됨 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2020010"></a>

## 14. 감사용역체결현황

- API ID: `2020010`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 감사용역체결현황을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020010)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/adtServcCnclsSttus.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/adtServcCnclsSttus.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| bsns_year | 사업연도 | 사업연도(당기, 전기, 전전기) |
| adtor | 감사인 | 감사인 |
| cn | 내용 | 내용 |
| mendng | 보수 | 보수 ① 2020년 7월 5일까지 사용됨 |
| tot_reqre_time | 총소요시간 | 총소요시간 ① 2020년 7월 5일까지 사용됨 |
| adt_cntrct_dtls_mendng | 감사계약내역(보수) | 감사계약내역(보수) ② 2020년 7월 6일부터 추가됨 |
| adt_cntrct_dtls_time | 감사계약내역(시간) | 감사계약내역(시간) ② 2020년 7월 6일부터 추가됨 |
| real_exc_dtls_mendng | 실제수행내역(보수) | 실제수행내역(보수) ② 2020년 7월 6일부터 추가됨 |
| real_exc_dtls_time | 실제수행내역(시간) | 실제수행내역(시간) ② 2020년 7월 6일부터 추가됨 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2020011"></a>

## 15. 회계감사인과의 비감사용역 계약체결 현황

- API ID: `2020011`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 회계감사인과의 비감사용역 계약체결 현황을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020011)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/accnutAdtorNonAdtServcCnclsSttus.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/accnutAdtorNonAdtServcCnclsSttus.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| bsns_year | 사업연도 | 사업연도(당기, 전기, 전전기) |
| cntrct_cncls_de | 계약체결일 | 계약체결일 |
| servc_cn | 용역내용 | 용역내용 |
| servc_exc_pd | 용역수행기간 | 용역수행기간 |
| servc_mendng | 용역보수 | 용역보수 |
| rm | 비고 | 비고 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2020012"></a>

## 16. 독립(사외)이사 및 그 변동현황

- API ID: `2020012`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 독립(사외)이사 및 그 변동현황을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020012)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/outcmpnyDrctrNdChangeSttus.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/outcmpnyDrctrNdChangeSttus.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| drctr_co | 이사의 수 | 9,999,999,999 |
| otcmp_drctr_co | 독립(사외)이사 수 | 9,999,999,999 |
| apnt | 독립(사외)이사 변동현황(선임) | 9,999,999,999 |
| rlsofc | 독립(사외)이사 변동현황(해임) | 9,999,999,999 |
| mdstrm_resig | 독립(사외)이사 변동현황(중도퇴임) | 9,999,999,999 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2019007"></a>

## 17. 최대주주 현황

- API ID: `2019007`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 최대주주 현황을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2019007)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/hyslrSttus.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/hyslrSttus.xml | UTF-8 | XML |

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
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 법인명 | 법인명 |
| nm | 성명 | 홍길동 |
| relate | 관계 | 본인, 친인척 등 |
| stock_knd | 주식 종류 | 보통주 등 |
| bsis_posesn_stock_co | 기초 소유 주식 수 | 9,999,999,999 |
| bsis_posesn_stock_qota_rt | 기초 소유 주식 지분 율 | 0.00 |
| trmend_posesn_stock_co | 기말 소유 주식 수 | 9,999,999,999 |
| trmend_posesn_stock_qota_rt | 기말 소유 주식 지분 율 | 0.00 |
| rm | 비고 | 비고 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2019008"></a>

## 18. 최대주주 변동현황

- API ID: `2019008`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 최대주주 변동현황을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2019008)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/hyslrChgSttus.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/hyslrChgSttus.xml | UTF-8 | XML |

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
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 법인명 | 법인명 |
| change_on | 변동 일 | YYYY.MM.DD |
| mxmm_shrholdr_nm | 최대 주주 명 | 홍길동 |
| posesn_stock_co | 소유 주식 수 | 9,999,999,999 |
| qota_rt | 지분 율 | 0.00 |
| change_cause | 변동 원인 | - |
| rm | 비고 | - |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2019009"></a>

## 19. 소액주주 현황

- API ID: `2019009`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 소액주주 현황을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2019009)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/mrhlSttus.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/mrhlSttus.xml | UTF-8 | XML |

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
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 법인명 | 법인명 |
| se | 구분 | 소액주주 |
| shrholdr_co | 주주수 | 9,999,999,999 |
| shrholdr_tot_co | 전체 주주수 | 9,999,999,999 |
| shrholdr_rate | 주주 비율 | 0.00 |
| hold_stock_co | 보유 주식수 | 9,999,999,999 |
| stock_tot_co | 총발행 주식수 | 9,999,999,999 |
| hold_stock_rate | 보유 주식 비율 | 0.00 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2019010"></a>

## 20. 임원 현황

- API ID: `2019010`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 임원 현황을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2019010)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/exctvSttus.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/exctvSttus.xml | UTF-8 | XML |

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
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 법인명 | 법인명 |
| nm | 성명 | 홍길동 |
| sexdstn | 성별 | 남 |
| birth_ym | 출생 년월 | YYYY년 MM월 |
| ofcps | 직위 | 회장, 사장, 사외이사 등 |
| rgist_exctv_at | 등기 임원 여부 | 등기임원, 미등기임원 등 |
| fte_at | 상근 여부 | 상근, 비상근 |
| chrg_job | 담당 업무 | 대표이사, 이사, 사외이사 등 |
| main_career | 주요 경력 | - |
| mxmm_shrholdr_relate | 최대 주주 관계 | - |
| hffc_pd | 재직 기간 | - |
| tenure_end_on | 임기 만료 일 | - |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2019011"></a>

## 21. 직원 현황

- API ID: `2019011`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 직원 현황을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2019011)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/empSttus.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/empSttus.xml | UTF-8 | XML |

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
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 법인명 | 법인명 |
| fo_bbm | 사 업부문 | - |
| sexdstn | 성별 | 남, 여 |
| reform_bfe_emp_co_rgllbr | 개정 전 직원 수 정규직 | - |
| reform_bfe_emp_co_cnttk | 개정 전 직원 수 계약직 | - |
| reform_bfe_emp_co_etc | 개정 전 직원 수 기타 | - |
| rgllbr_co | 정규직 수 | 상근, 비상근 |
| rgllbr_abacpt_labrr_co | 정규직 단시간 근로자 수 | 대표이사, 이사, 사외이사 등 |
| cnttk_co | 계약직 수 | 9,999,999,999 |
| cnttk_abacpt_labrr_co | 계약직 단시간 근로자 수 | 9,999,999,999 |
| sm | 합계 | 9,999,999,999 |
| avrg_cnwk_sdytrn | 평균 근속 연수 | 9,999,999,999 |
| fyer_salary_totamt | 연간 급여 총액 | 9,999,999,999 |
| jan_salary_am | 1인평균 급여 액 | 9,999,999,999 |
| rm | 비고 | - |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2020013"></a>

## 22. 미등기임원 보수현황

- API ID: `2020013`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 미등기임원 보수현황을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020013)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/unrstExctvMendngSttus.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/unrstExctvMendngSttus.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| se | 구분 | 구분(미등기임원) |
| nmpr | 인원수 | 9,999,999,999 |
| fyer_salary_totamt | 연간급여 총액 | 9,999,999,999 |
| jan_salary_am | 1인평균 급여액 | 9,999,999,999 |
| rm | 비고 | 비고 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2020014"></a>

## 23. 이사·감사 전체의 보수현황(주주총회 승인금액)

- API ID: `2020014`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 이사·감사 전체의 보수현황(주주총회 승인금액)을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020014)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/drctrAdtAllMendngSttusGmtsckConfmAmount.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/drctrAdtAllMendngSttusGmtsckConfmAmount.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| se | 구분 | 구분 |
| nmpr | 인원수 | 인원수 |
| gmtsck_confm_amount | 주주총회 승인금액 | 9,999,999,999 |
| rm | 비고 | 비고 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |
| fscl_year | 사업연도 | 당기,전기,전전기 |

---

<a id="api-2019013"></a>

## 24. 이사·감사 전체의 보수현황(보수지급금액 - 이사·감사 전체)

- API ID: `2019013`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 이사·감사 전체의 보수현황(보수지급금액 - 이사·감사 전체)을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2019013)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/hmvAuditAllSttus.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/hmvAuditAllSttus.xml | UTF-8 | XML |

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
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 법인명 | 법인명 |
| nmpr | 인원수 | 9,999,999,999 |
| mendng_totamt | 보수 총액 | 9,999,999,999 |
| jan_avrg_mendng_am | 1인 평균 보수 액 | 9,999,999,999 |
| rm | 비고 | - |
| stlm_dt | 결산기준일 | YYYY-MM-DD |
| fscl_year | 사업연도 | 당기,전기,전전기 |
| stk_bsd_pd_mendng_totamt | 보수총액 중 주식기준보상 지급액 | 9,999,999,999 |
| stk_opt_exrcsbl_qty | 주식매수선택권 행사가능수량 | 9,999,999,999 |
| stk_opt_unexrcsbl_qty | 주식매수선택권 행사불가수량 | 9,999,999,999 |
| stk_opt_rmn_blce | 주식매수선택권 잔여금액 | 9,999,999,999 |
| othr_stk_bsd_cmpn_unpyd_qty | 그 외 주식기준 보상 미지급수량 | 9,999,999,999 |
| othr_stk_bsd_cmpn_mkt_vl | 그 외 주식기준 보상 시장가치 | 9,999,999,999 |

---

<a id="api-2020015"></a>

## 25. 이사·감사 전체의 보수현황(보수지급금액 - 유형별)

- API ID: `2020015`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 이사·감사 전체의 보수현황(보수지급금액 - 유형별)을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2020015)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/drctrAdtAllMendngSttusMendngPymntamtTyCl.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/drctrAdtAllMendngSttusMendngPymntamtTyCl.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(1) | Y | 사업연도(4자리) ※ 2015년 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| list | - | - |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| se | 구분 | 구분 |
| nmpr | 인원수 | 9,999,999,999 |
| pymnt_totamt | 보수총액 | 9,999,999,999 |
| psn1_avrg_pymntamt | 1인당 평균보수액 | 9,999,999,999 |
| rm | 비고 | 비고 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |
| fscl_year | 사업연도 | 당기,전기,전전기 |
| stk_bsd_pd_mendng_totamt | 보수총액 중 주식기준보상 지급액 | 9,999,999,999 |
| stk_opt_exrcsbl_qty | 주식매수선택권 행사가능수량 | 9,999,999,999 |
| stk_opt_unexrcsbl_qty | 주식매수선택권 행사불가수량 | 9,999,999,999 |
| stk_opt_rmn_blce | 주식매수선택권 잔여금액 | 9,999,999,999 |
| othr_stk_bsd_cmpn_unpyd_qty | 그 외 주식기준 보상 미지급수량 | 9,999,999,999 |
| othr_stk_bsd_cmpn_mkt_vl | 그 외 주식기준 보상 시장가치 | 9,999,999,999 |

---

<a id="api-2019012"></a>

## 26. 이사·감사의 개인별 보수현황(5억원 이상)

- API ID: `2019012`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 이사·감사의 개인별 보수현황(5억원 이상)을 제공합니다. ※ 2026년 4월까지 제출된 보고서해당
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2019012)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/hmvAuditIndvdlBySttus.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/hmvAuditIndvdlBySttus.xml | UTF-8 | XML |

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
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 법인명 | 법인명 |
| nm | 이름 | 홍길동 |
| ofcps | 직위 | 이사, 대표이사 등 |
| mendng_totamt | 보수 총액 | 9,999,999,999 |
| mendng_totamt_ct_incls_mendng | 보수 총액 비 포함 보수 | 9,999,999,999 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2026001"></a>

## 27. 이사·감사의 개인별 보수현황(5억원 이상) (Ver 2.0)

- API ID: `2026001`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 이사·감사의 개인별 보수현황(5억원 이상)을 제공합니다. ※ 2026년 5월 이후부터 제출된 보고서해당
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2026001)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/hmvAuditIndvdlBySttusV2.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/hmvAuditIndvdlBySttusV2.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(4) | Y | 사업연도(4자리) ※ 2026년 5월 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |
| group | - | - |
| nm | 이름 | 홍길동 |
| fscl_year | 사업연도 | 당기,전기,전전기 |
| ofcps | 직위 | 이사, 대표이사 등 |
| mendng_totamt | 보수 총액 | 9,999,999,999 |
| list | - | - |
| stk_bsd_pd_mendng_totamt_knd | 보수총액 중 주식기준보상 지급액-종류 | - |
| stk_bsd_pd_mendng_totamt_qty | 보수총액 중 주식기준보상 지급액-수량 | 9,999,999,999 |
| stk_bsd_pd_mendng_totamt_amt | 보수총액 중 주식기준보상 지급액-금액 | 9,999,999,999 |
| stk_opt_exrcsbl_qty | 주식매수선택권 행사가능수량 | 9,999,999,999 |
| stk_opt_unexrcsbl_qty | 주식매수선택권 행사불가수량 | 9,999,999,999 |
| stk_opt_exrc_pr | 주식매수선택권 행사가격 | 9,999,999,999 |
| stk_opt_rmn_blce | 주식매수선택권 잔여금액 | 9,999,999,999 |
| othr_stk_bsd_cmpn_unpyd_qty | 그 외 주식기준 보상 미지급수량 | 9,999,999,999 |
| othr_stk_bsd_cmpn_mkt_vl | 그 외 주식기준 보상 시장가치 | 9,999,999,999 |
| rm | 비고 | - |

---

<a id="api-2019014"></a>

## 28. 개인별 보수지급 금액(5억이상 상위5인)

- API ID: `2019014`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 개인별 보수지급 금액(5억이상 상위5인)을 제공합니다. ※ 2026년 4월까지 제출된 보고서해당
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2019014)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/indvdlByPay.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/indvdlByPay.xml | UTF-8 | XML |

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
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 법인명 | 법인명 |
| nm | 이름 | 홍길동 |
| ofcps | 직위 | 대표이사 등 |
| mendng_totamt | 보수 총액 | 9,999,999,999 |
| mendng_totamt_ct_incls_mendng | 보수 총액 비 포함 보수 | 9,999,999,999 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---

<a id="api-2026002"></a>

## 29. 개인별 보수지급 금액(5억이상 상위5인) (Ver 2.0)

- API ID: `2026002`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 이사·감사의 개인별 보수현황(5억원 이상)을 제공합니다. ※ 2026년 5월 이후부터 제출된 보고서해당
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2026002)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/indvdlByPayV2.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/indvdlByPayV2.xml | UTF-8 | XML |

### 요청 인자

| 요청 키 | 명칭 | 타입 | 필수 | 값 설명 |
|---|---|---|---|---|
| crtfc_key | API 인증키 | STRING(40) | Y | 발급받은 인증키(40자리) |
| corp_code | 고유번호 | STRING(8) | Y | 공시대상회사의 고유번호(8자리) ※ 개발가이드 > 공시정보 > 고유번호 참고 |
| bsns_year | 사업연도 | STRING(4) | Y | 사업연도(4자리) ※ 2026년 5월 이후 부터 정보제공 |
| reprt_code | 보고서 코드 | STRING(5) | Y | 1분기보고서 : 11013 반기보고서 : 11012 3분기보고서 : 11014 사업보고서 : 11011 |

### 응답 필드

| 응답 키 | 명칭 | 출력 설명 |
|---|---|---|
| result | - | - |
| status | 에러 및 정보 코드 | (※메시지 설명 참조) |
| message | 에러 및 정보 메시지 | (※메시지 설명 참조) |
| rcept_no | 접수번호 | 접수번호(14자리) ※ 공시뷰어 연결에 이용예시 - PC용 : https://dart.fss.or.kr/dsaf001/main.do?rcpNo=접수번호 |
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |
| group | - | - |
| nm | 이름 | 홍길동 |
| fscl_year | 사업연도 | 당기,전기,전전기 |
| ofcps | 직위 | 이사, 대표이사 등 |
| mendng_totamt | 보수 총액 | 9,999,999,999 |
| list | - | - |
| stk_bsd_pd_mendng_totamt_knd | 보수총액 중 주식기준보상 지급액-종류 | - |
| stk_bsd_pd_mendng_totamt_qty | 보수총액 중 주식기준보상 지급액-수량 | 9,999,999,999 |
| stk_bsd_pd_mendng_totamt_amt | 보수총액 중 주식기준보상 지급액-금액 | 9,999,999,999 |
| stk_opt_exrcsbl_qty | 주식매수선택권 행사가능수량 | 9,999,999,999 |
| stk_opt_unexrcsbl_qty | 주식매수선택권 행사불가수량 | 9,999,999,999 |
| stk_opt_exrc_pr | 주식매수선택권 행사가격 | 9,999,999,999 |
| stk_opt_rmn_blce | 주식매수선택권 잔여금액 | 9,999,999,999 |
| othr_stk_bsd_cmpn_unpyd_qty | 그 외 주식기준 보상 미지급수량 | 9,999,999,999 |
| othr_stk_bsd_cmpn_mkt_vl | 그 외 주식기준 보상 시장가치 | 9,999,999,999 |
| rm | 비고 | - |

---

<a id="api-2019015"></a>

## 30. 타법인 출자현황

- API ID: `2019015`
- 분류 코드: `DS002`
- 기능: 정기보고서(사업, 분기, 반기보고서) 내에 타법인 출자현황을 제공합니다.
- 공식 상세: [OpenDART 원문](https://opendart.fss.or.kr/guide/detail.do?apiGrpCd=DS002&apiId=2019015)

### 기본 정보

| 메서드 | 요청 URL | 인코딩 | 출력 형식 |
|---|---|---|---|
| GET | https://opendart.fss.or.kr/api/otrCprInvstmntSttus.json | UTF-8 | JSON |
| GET | https://opendart.fss.or.kr/api/otrCprInvstmntSttus.xml | UTF-8 | XML |

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
| corp_cls | 법인구분 | 법인구분 : Y(유가), K(코스닥), N(코넥스), E(기타) |
| corp_code | 고유번호 | 공시대상회사의 고유번호(8자리) |
| corp_name | 회사명 | 공시대상회사명 |
| inv_prm | 법인명 | 법인명 |
| frst_acqs_de | 최초 취득 일자 | 최초취득일자(YYYYMMDD) |
| invstmnt_purps | 출자 목적 | 출자목적(자회사 등) |
| frst_acqs_amount | 최초 취득 금액 | 9,999,999,999 |
| bsis_blce_qy | 기초 잔액 수량 | 9,999,999,999 |
| bsis_blce_qota_rt | 기초 잔액 지분 율 | 0.00 |
| bsis_blce_acntbk_amount | 기초 잔액 장부 가액 | 9,999,999,999 |
| incrs_dcrs_acqs_dsps_qy | 증가 감소 취득 처분 수량 | 9,999,999,999 |
| incrs_dcrs_acqs_dsps_amount | 증가 감소 취득 처분 금액 | 9,999,999,999 |
| incrs_dcrs_evl_lstmn | 증가 감소 평가 손액 | 9,999,999,999 |
| trmend_blce_qy | 기말 잔액 수량 | 9,999,999,999 |
| trmend_blce_qota_rt | 기말 잔액 지분 율 | 0.00 |
| trmend_blce_acntbk_amount | 기말 잔액 장부 가액 | 9,999,999,999 |
| recent_bsns_year_fnnr_sttus_tot_assets | 최근 사업 연도 재무 현황 총 자산 | 9,999,999,999 |
| recent_bsns_year_fnnr_sttus_thstrm_ntpf | 최근 사업 연도 재무 현황 당기 순이익 | 9,999,999,999 |
| stlm_dt | 결산기준일 | YYYY-MM-DD |

---
