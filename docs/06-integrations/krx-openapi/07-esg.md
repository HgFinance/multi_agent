# KRX Open API ESG 전체 참조

> [KRX 공식 서비스 화면](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES007_S1.cmd)과 상세 개발 명세를 2026-07-30 17:10:36 +09:00 에 구조화한 개발용 참조입니다. 실제 연동 전 승인된 서비스와 최신 계약을 다시 확인합니다.

## API 목록

API 3개

| 번호 | API | API ID | 제공 범위 | 최근 수정일 |
|---:|---|---|---|---|
| 1 | [ESG 증권상품](#api-esg_etp_info) | `esg_etp_info` | ESG 증권상품 정보를 제공 ('20년01월02일 데이터부터 제공) | 2026/03/30 |
| 2 | [ESG 지수](#api-esg_index_info) | `esg_index_info` | ESG 지수 정보를 제공 ('20년01월02일 데이터부터 제공) | 2026/03/30 |
| 3 | [사회책임투자채권 정보](#api-sri_bond_info) | `sri_bond_info` | 사회책임투자채권 정보를 제공 ('19년01월01일 데이터부터 제공) | 2026/01/16 |

---

<a id="api-esg_etp_info"></a>

## 1. ESG 증권상품

| 항목 | 값 |
|---|---|
| API ID | `esg_etp_info` |
| 내부 명세 ID | `dpRoGGhdnfSZSrMFtUCz` |
| 명세 버전 | `1.0` |
| 등록일 | 2025/12/26 |
| 최근 수정일 | 2026/03/30 |
| 설명 | ESG 증권상품 정보를 제공 ('20년01월02일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/esg/esg_etp_info` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/esg/esg_etp_info` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES007_S2.cmd?BO_ID=dpRoGGhdnfSZSrMFtUCz) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | - | `-` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `ISU_ABBRV` | 종목명 | string | `-` | `-` |
| OutBlock_1 | `TDD_CLSPRC` | 현재가 | string | `-` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 전일비 | string | `-` | `-` |
| OutBlock_1 | `FLUC_RT` | 등락률 | string | `-` | `-` |
| OutBlock_1 | `LIST_SHRS` | 상장좌수 | string | `-` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량(좌) | string | `-` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금(원) | string | `-` | `-` |

---

<a id="api-esg_index_info"></a>

## 2. ESG 지수

| 항목 | 값 |
|---|---|
| API ID | `esg_index_info` |
| 내부 명세 ID | `WgFYvEvsseQMARfMVZCq` |
| 명세 버전 | `1.0` |
| 등록일 | 2025/12/26 |
| 최근 수정일 | 2026/03/30 |
| 설명 | ESG 지수 정보를 제공 ('20년01월02일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/esg/esg_index_info` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/esg/esg_index_info` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES007_S2.cmd?BO_ID=WgFYvEvsseQMARfMVZCq) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | - | `-` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `IDX_NM` | 지수명 | string | `-` | `-` |
| OutBlock_1 | `CLSPRC_IDX` | 현재가 | string | `-` | `-` |
| OutBlock_1 | `PRV_DD_CMPR` | 전일비 | string | `-` | `-` |
| OutBlock_1 | `UPDN_RATE` | 등락률 | string | `-` | `-` |
| OutBlock_1 | `TRD_ISU_CNT` | 구성종목수 | string | `-` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량(천주) | string | `-` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금(백만원) | string | `-` | `-` |

---

<a id="api-sri_bond_info"></a>

## 3. 사회책임투자채권 정보

| 항목 | 값 |
|---|---|
| API ID | `sri_bond_info` |
| 내부 명세 ID | `MwsSXzVIceQhMSJUeCdp` |
| 명세 버전 | `1.0` |
| 등록일 | 2023/11/15 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 사회책임투자채권 정보를 제공 ('19년01월01일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/esg/sri_bond_info` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/esg/sri_bond_info` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES007_S2.cmd?BO_ID=MwsSXzVIceQhMSJUeCdp) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `ISUR_NM` | 발행기관 | string | `-` | `-` |
| OutBlock_1 | `ISU_CD` | 표준코드 | string | `-` | `-` |
| OutBlock_1 | `SRI_BND_TP_NM` | 채권종류 | string | `-` | `-` |
| OutBlock_1 | `ISU_NM` | 종목명 | string | `-` | `-` |
| OutBlock_1 | `LIST_DD` | 상장일 | string | `-` | `-` |
| OutBlock_1 | `ISU_DD` | 발행일 | string | `-` | `-` |
| OutBlock_1 | `REDMPT_DD` | 상환일 | string | `-` | `-` |
| OutBlock_1 | `ISU_RT` | 표면이자율 | string | `###0.00000` | `-` |
| OutBlock_1 | `ISU_AMT` | 발행금액 | string | `###0` | `-` |
| OutBlock_1 | `LIST_AMT` | 상장금액 | string | `###0` | `-` |
| OutBlock_1 | `BND_TP_NM` | 채권유형 | string | `-` | `-` |

---
