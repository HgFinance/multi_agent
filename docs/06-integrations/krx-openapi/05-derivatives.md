# KRX Open API 파생상품 전체 참조

> [KRX 공식 서비스 화면](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES005_S1.cmd)과 상세 개발 명세를 2026-07-30 17:10:36 +09:00 에 구조화한 개발용 참조입니다. 실제 연동 전 승인된 서비스와 최신 계약을 다시 확인합니다.

## API 목록

API 6개

| 번호 | API | API ID | 제공 범위 | 최근 수정일 |
|---:|---|---|---|---|
| 1 | [선물 일별매매정보 (주식선물外)](#api-fut_bydd_trd) | `fut_bydd_trd` | 파생상품시장의 선물 중 주식선물을 제외한 선물의 매매정보 제공 ('10년01월04일 데이터부터 제공) | 2026/01/16 |
| 2 | [주식선물(유가) 일별매매정보](#api-eqsfu_stk_bydd_trd) | `eqsfu_stk_bydd_trd` | 파생상품시장의 주식선물 중 기초자산이 유가증권시장에 속하는 주식선물의 거래정보 제공 ('10년01월04일 데이터부터 제공) | 2026/01/16 |
| 3 | [주식선물(코스닥) 일별매매정보](#api-eqkfu_ksq_bydd_trd) | `eqkfu_ksq_bydd_trd` | 파생상품시장의 주식선물 중 기초자산이 코스닥시장에 속하는 주식선물의 거래정보 제공 ('15년08월03일 데이터부터 제공) | 2026/01/16 |
| 4 | [옵션 일별매매정보 (주식옵션外)](#api-opt_bydd_trd) | `opt_bydd_trd` | 파생상품시장의 옵션 중 주식옵션을 제외한 옵션의 매매정보 제공 ('10년01월04일 데이터부터 제공) | 2026/07/16 |
| 5 | [주식옵션(유가) 일별매매정보](#api-eqsop_bydd_trd) | `eqsop_bydd_trd` | 파생상품시장의 주식옵션 중 기초자산이 유가증권시장에 속하는 주식옵션의 거래정보 제공 ('10년01월04일 데이터부터 제공) | 2026/07/16 |
| 6 | [주식옵션(코스닥) 일별매매정보](#api-eqkop_bydd_trd) | `eqkop_bydd_trd` | 파생상품시장의 주식옵션 중 기초자산이 코스닥시장에 속하는 주식옵션의 거래정보 제공 ('17년06월26일 데이터부터 제공) | 2026/01/16 |

---

<a id="api-fut_bydd_trd"></a>

## 1. 선물 일별매매정보 (주식선물外)

| 항목 | 값 |
|---|---|
| API ID | `fut_bydd_trd` |
| 내부 명세 ID | `ilaVYOabbaicHbKTsqga` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 파생상품시장의 선물 중 주식선물을 제외한 선물의 매매정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/drv/fut_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/drv/fut_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES005_S2.cmd?BO_ID=ilaVYOabbaicHbKTsqga) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `PROD_NM` | 상품구분 | string | `-` | `-` |
| OutBlock_1 | `MKT_NM` | 시장구분(정규/야간) | string | `-` | `-` |
| OutBlock_1 | `ISU_CD` | 종목코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_NM` | 종목명 | string | `-` | `-` |
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `###0.00#` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `###0.00#` | `-` |
| OutBlock_1 | `SPOT_PRC` | 현물가 | string | `###0.00#` | `-` |
| OutBlock_1 | `SETL_PRC` | 정산가 | string | `###0.00#` | `0` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `ACC_OPNINT_QTY` | 미결제약정 | string | `###0` | `-` |

---

<a id="api-eqsfu_stk_bydd_trd"></a>

## 2. 주식선물(유가) 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `eqsfu_stk_bydd_trd` |
| 내부 명세 ID | `JzVvQnspImpuqtZlFWpJ` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 파생상품시장의 주식선물 중 기초자산이 유가증권시장에 속하는 주식선물의 거래정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/drv/eqsfu_stk_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/drv/eqsfu_stk_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES005_S2.cmd?BO_ID=JzVvQnspImpuqtZlFWpJ) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `PROD_NM` | 상품구분 | string | `-` | `-` |
| OutBlock_1 | `MKT_NM` | 시장구분(정규/야간) | string | `-` | `-` |
| OutBlock_1 | `ISU_CD` | 종목코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_NM` | 종목명 | string | `-` | `-` |
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `###0.00#` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `###0.00#` | `-` |
| OutBlock_1 | `SPOT_PRC` | 현물가 | string | `###0.00#` | `-` |
| OutBlock_1 | `SETL_PRC` | 정산가 | string | `###0.00#` | `0` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `ACC_OPNINT_QTY` | 미결제약정 | string | `###0` | `-` |

---

<a id="api-eqkfu_ksq_bydd_trd"></a>

## 3. 주식선물(코스닥) 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `eqkfu_ksq_bydd_trd` |
| 내부 명세 ID | `henfdJADfLTCUCBWIRCj` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 파생상품시장의 주식선물 중 기초자산이 코스닥시장에 속하는 주식선물의 거래정보 제공 ('15년08월03일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/drv/eqkfu_ksq_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/drv/eqkfu_ksq_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES005_S2.cmd?BO_ID=henfdJADfLTCUCBWIRCj) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `PROD_NM` | 상품구분 | string | `-` | `-` |
| OutBlock_1 | `MKT_NM` | 시장구분(정규/야간) | string | `-` | `-` |
| OutBlock_1 | `ISU_CD` | 종목코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_NM` | 종목명 | string | `-` | `-` |
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `###0.00#` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `###0.00#` | `-` |
| OutBlock_1 | `SPOT_PRC` | 현물가 | string | `###0.00#` | `-` |
| OutBlock_1 | `SETL_PRC` | 정산가 | string | `###0.00#` | `0` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `ACC_OPNINT_QTY` | 미결제약정 | string | `###0` | `-` |

---

<a id="api-opt_bydd_trd"></a>

## 4. 옵션 일별매매정보 (주식옵션外)

| 항목 | 값 |
|---|---|
| API ID | `opt_bydd_trd` |
| 내부 명세 ID | `AoTvuFpukvuBsfypkZbq` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/07/16 |
| 설명 | 파생상품시장의 옵션 중 주식옵션을 제외한 옵션의 매매정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/drv/opt_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/drv/opt_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES005_S2.cmd?BO_ID=AoTvuFpukvuBsfypkZbq) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `PROD_NM` | 상품구분 | string | `-` | `-` |
| OutBlock_1 | `RGHT_TP_NM` | 권리유형(CALL/PUT) | string | `-` | `-` |
| OutBlock_1 | `ISU_CD` | 종목코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_NM` | 종목명 | string | `-` | `-` |
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `###0.00#` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `###0.00#` | `-` |
| OutBlock_1 | `IMP_VOLT` | 내재변동성 | string | `###0.00#` | `-` |
| OutBlock_1 | `NXTDD_BAS_PRC` | 익일정산가 | string | `###0.00#` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `ACC_OPNINT_QTY` | 미결제약정 | string | `###0` | `-` |

---

<a id="api-eqsop_bydd_trd"></a>

## 5. 주식옵션(유가) 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `eqsop_bydd_trd` |
| 내부 명세 ID | `fwWKgzbevDVtAoECgkpA` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/07/16 |
| 설명 | 파생상품시장의 주식옵션 중 기초자산이 유가증권시장에 속하는 주식옵션의 거래정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/drv/eqsop_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/drv/eqsop_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES005_S2.cmd?BO_ID=fwWKgzbevDVtAoECgkpA) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `PROD_NM` | 상품구분 | string | `-` | `-` |
| OutBlock_1 | `RGHT_TP_NM` | 권리유형(CALL/PUT) | string | `-` | `-` |
| OutBlock_1 | `ISU_CD` | 종목코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_NM` | 종목명 | string | `-` | `-` |
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `###0.00#` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `###0.00#` | `-` |
| OutBlock_1 | `IMP_VOLT` | 내재변동성 | string | `###0.00#` | `-` |
| OutBlock_1 | `NXTDD_BAS_PRC` | 익일정산가 | string | `###0.00#` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `ACC_OPNINT_QTY` | 미결제약정 | string | `###0` | `-` |

---

<a id="api-eqkop_bydd_trd"></a>

## 6. 주식옵션(코스닥) 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `eqkop_bydd_trd` |
| 내부 명세 ID | `AFNbHSizSPnEssZoUqiS` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 파생상품시장의 주식옵션 중 기초자산이 코스닥시장에 속하는 주식옵션의 거래정보 제공 ('17년06월26일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/drv/eqkop_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/drv/eqkop_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES005_S2.cmd?BO_ID=AFNbHSizSPnEssZoUqiS) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `PROD_NM` | 상품구분 | string | `-` | `-` |
| OutBlock_1 | `RGHT_TP_NM` | 권리유형(CALL/PUT) | string | `-` | `-` |
| OutBlock_1 | `ISU_CD` | 종목코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_NM` | 종목명 | string | `-` | `-` |
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `###0.00#` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `###0.00#` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `###0.00#` | `-` |
| OutBlock_1 | `IMP_VOLT` | 내재변동성 | string | `###0.00#` | `-` |
| OutBlock_1 | `NXTDD_BAS_PRC` | 익일정산가 | string | `###0.00#` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `ACC_OPNINT_QTY` | 미결제약정 | string | `###0` | `-` |

---
