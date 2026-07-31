# KRX Open API 주식 전체 참조

> [KRX 공식 서비스 화면](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S1.cmd)과 상세 개발 명세를 2026-07-30 17:10:36 +09:00 에 구조화한 개발용 참조입니다. 실제 연동 전 승인된 서비스와 최신 계약을 다시 확인합니다.

## API 목록

API 8개

| 번호 | API | API ID | 제공 범위 | 최근 수정일 |
|---:|---|---|---|---|
| 1 | [유가증권 일별매매정보](#api-stk_bydd_trd) | `stk_bydd_trd` | 유가증권시장에 상장되어 있는 주권의 매매정보 제공 ('10년01월04일 데이터부터 제공) | 2026/01/16 |
| 2 | [코스닥 일별매매정보](#api-ksq_bydd_trd) | `ksq_bydd_trd` | 코스닥시장에 상장되어 있는 주권의 매매정보 제공 ('10년01월04일 데이터부터 제공) | 2026/01/16 |
| 3 | [코넥스 일별매매정보](#api-knx_bydd_trd) | `knx_bydd_trd` | 코넥스시장에 상장되어 있는 주권의 매매정보 제공 ('13년07월01일 데이터부터 제공) | 2026/01/16 |
| 4 | [신주인수권증권 일별매매정보](#api-sw_bydd_trd) | `sw_bydd_trd` | 유가증권/코스닥시장에 상장되어 있는 신주인수권증권의 매매정보 제공 ('10년01월04일 데이터부터 제공) | 2026/01/16 |
| 5 | [신주인수권증서 일별매매정보](#api-sr_bydd_trd) | `sr_bydd_trd` | 유가증권/코스닥시장에 상장되어 있는 신주인수권증서의 매매정보 제공 ('10년02월12일 데이터부터 제공) | 2026/01/16 |
| 6 | [유가증권 종목기본정보](#api-stk_isu_base_info) | `stk_isu_base_info` | 유가증권 종목기본정보 ('10년01월04일 데이터부터 제공) | 2026/01/16 |
| 7 | [코스닥 종목기본정보](#api-ksq_isu_base_info) | `ksq_isu_base_info` | 코스닥 종목기본정보 ('10년01월04일 데이터부터 제공) | 2026/01/16 |
| 8 | [코넥스 종목기본정보](#api-knx_isu_base_info) | `knx_isu_base_info` | 코넥스 종목기본정보 ('13년07월01일 데이터부터 제공) | 2026/01/16 |

---

<a id="api-stk_bydd_trd"></a>

## 1. 유가증권 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `stk_bydd_trd` |
| 내부 명세 ID | `JvJFzlAENzZlPBDNGAWC` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 유가증권시장에 상장되어 있는 주권의 매매정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/sto/stk_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S2.cmd?BO_ID=JvJFzlAENzZlPBDNGAWC) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `ISU_CD` | 종목코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_NM` | 종목명 | string | `-` | `-` |
| OutBlock_1 | `MKT_NM` | 시장구분 | string | `-` | `-` |
| OutBlock_1 | `SECT_TP_NM` | 소속부 | string | `-` | `-` |
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `###0` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `###0` | `-` |
| OutBlock_1 | `FLUC_RT` | 등락률 | string | `###0.00` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `MKTCAP` | 시가총액 | string | `###0` | `-` |
| OutBlock_1 | `LIST_SHRS` | 상장주식수 | string | `###0` | `-` |

---

<a id="api-ksq_bydd_trd"></a>

## 2. 코스닥 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `ksq_bydd_trd` |
| 내부 명세 ID | `hZjGpkllgCBCWqeTsYFj` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 코스닥시장에 상장되어 있는 주권의 매매정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/sto/ksq_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/sto/ksq_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S2.cmd?BO_ID=hZjGpkllgCBCWqeTsYFj) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `ISU_CD` | 종목코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_NM` | 종목명 | string | `-` | `-` |
| OutBlock_1 | `MKT_NM` | 시장구분 | string | `-` | `-` |
| OutBlock_1 | `SECT_TP_NM` | 소속부 | string | `-` | `-` |
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `###0` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `###0` | `-` |
| OutBlock_1 | `FLUC_RT` | 등락률 | string | `###0.00` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `MKTCAP` | 시가총액 | string | `###0` | `-` |
| OutBlock_1 | `LIST_SHRS` | 상장주식수 | string | `###0` | `-` |

---

<a id="api-knx_bydd_trd"></a>

## 3. 코넥스 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `knx_bydd_trd` |
| 내부 명세 ID | `HSiRvxGSYnvaKuAuqpqp` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 코넥스시장에 상장되어 있는 주권의 매매정보 제공 ('13년07월01일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/sto/knx_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/sto/knx_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S2.cmd?BO_ID=HSiRvxGSYnvaKuAuqpqp) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `ISU_CD` | 종목코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_NM` | 종목명 | string | `-` | `-` |
| OutBlock_1 | `MKT_NM` | 시장구분 | string | `-` | `-` |
| OutBlock_1 | `SECT_TP_NM` | 소속부 | string | `-` | `-` |
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `###0` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `###0` | `-` |
| OutBlock_1 | `FLUC_RT` | 등락률 | string | `###0.00` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `MKTCAP` | 시가총액 | string | `###0` | `-` |
| OutBlock_1 | `LIST_SHRS` | 상장주식수 | string | `###0` | `-` |

---

<a id="api-sw_bydd_trd"></a>

## 4. 신주인수권증권 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `sw_bydd_trd` |
| 내부 명세 ID | `erXKnEAzTqcGnkcoSdGA` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 유가증권/코스닥시장에 상장되어 있는 신주인수권증권의 매매정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/sto/sw_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/sto/sw_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S2.cmd?BO_ID=erXKnEAzTqcGnkcoSdGA) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `MKT_NM` | 시장구분 | string | `-` | `-` |
| OutBlock_1 | `ISU_CD` | 종목코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_NM` | 종목명 | string | `-` | `-` |
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `###0` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `###0` | `-` |
| OutBlock_1 | `FLUC_RT` | 등락률 | string | `###0.00` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `MKTCAP` | 시가총액 | string | `###0` | `-` |
| OutBlock_1 | `LIST_SHRS` | 상장증권수 | string | `###0` | `-` |
| OutBlock_1 | `EXER_PRC` | 행사가격 | string | `###0` | `-` |
| OutBlock_1 | `EXST_STRT_DD` | 존속기간_시작일 | string | `-` | `-` |
| OutBlock_1 | `EXST_END_DD` | 존속기간_종료일 | string | `-` | `-` |
| OutBlock_1 | `TARSTK_ISU_SRT_CD` | 목적주권_종목코드 | string | `-` | `-` |
| OutBlock_1 | `TARSTK_ISU_NM` | 목적주권_종목명 | string | `-` | `-` |
| OutBlock_1 | `TARSTK_ISU_PRSNT_PRC` | 목적주권_종가 | string | `###0` | `-` |

---

<a id="api-sr_bydd_trd"></a>

## 5. 신주인수권증서 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `sr_bydd_trd` |
| 내부 명세 ID | `YieGrzzJtKhbaNLuKmhz` |
| 명세 버전 | `1.0` |
| 등록일 | 2022/07/04 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 유가증권/코스닥시장에 상장되어 있는 신주인수권증서의 매매정보 제공 ('10년02월12일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/sto/sr_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/sto/sr_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S2.cmd?BO_ID=YieGrzzJtKhbaNLuKmhz) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `MKT_NM` | 시장구분 | string | `-` | `-` |
| OutBlock_1 | `ISU_CD` | 종목코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_NM` | 종목명 | string | `-` | `-` |
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `###0` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `###0` | `-` |
| OutBlock_1 | `FLUC_RT` | 등락률 | string | `###0.00` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `MKTCAP` | 시가총액 | string | `###0` | `-` |
| OutBlock_1 | `LIST_SHRS` | 상장증서수 | string | `###0` | `-` |
| OutBlock_1 | `ISU_PRC` | 신주발행가 | string | `###0` | `-` |
| OutBlock_1 | `DELIST_DD` | 상장폐지일 | string | `-` | `-` |
| OutBlock_1 | `TARSTK_ISU_SRT_CD` | 목적주권_종목코드 | string | `-` | `-` |
| OutBlock_1 | `TARSTK_ISU_NM` | 목적주권_종목명 | string | `-` | `-` |
| OutBlock_1 | `TARSTK_ISU_PRSNT_PRC` | 목적주권_종가 | string | `###0` | `-` |

---

<a id="api-stk_isu_base_info"></a>

## 6. 유가증권 종목기본정보

| 항목 | 값 |
|---|---|
| API ID | `stk_isu_base_info` |
| 내부 명세 ID | `PiwgMdTwmsenXhmqqxuj` |
| 명세 버전 | `1.0` |
| 등록일 | 2022/05/06 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 유가증권 종목기본정보 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/sto/stk_isu_base_info` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/sto/stk_isu_base_info` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S2.cmd?BO_ID=PiwgMdTwmsenXhmqqxuj) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `ISU_CD` | 표준코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_SRT_CD` | 단축코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_NM` | 한글 종목명 | string | `-` | `-` |
| OutBlock_1 | `ISU_ABBRV` | 한글 종목약명 | string | `-` | `-` |
| OutBlock_1 | `ISU_ENG_NM` | 영문 종목명 | string | `-` | `-` |
| OutBlock_1 | `LIST_DD` | 상장일 | string | `-` | `-` |
| OutBlock_1 | `MKT_TP_NM` | 시장구분 | string | `-` | `-` |
| OutBlock_1 | `SECUGRP_NM` | 증권구분 | string | `-` | `-` |
| OutBlock_1 | `SECT_TP_NM` | 소속부 | string | `-` | `-` |
| OutBlock_1 | `KIND_STKCERT_TP_NM` | 주식종류 | string | `-` | `-` |
| OutBlock_1 | `PARVAL` | 액면가 | string | `-` | `-` |
| OutBlock_1 | `LIST_SHRS` | 상장주식수 | string | `-` | `-` |

---

<a id="api-ksq_isu_base_info"></a>

## 7. 코스닥 종목기본정보

| 항목 | 값 |
|---|---|
| API ID | `ksq_isu_base_info` |
| 내부 명세 ID | `CifLHplnUFMgpHIMMPXs` |
| 명세 버전 | `1.0` |
| 등록일 | 2022/05/06 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 코스닥 종목기본정보 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/sto/ksq_isu_base_info` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/sto/ksq_isu_base_info` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S2.cmd?BO_ID=CifLHplnUFMgpHIMMPXs) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `ISU_CD` | 표준코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_SRT_CD` | 단축코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_NM` | 한글 종목명 | string | `-` | `-` |
| OutBlock_1 | `ISU_ABBRV` | 한글 종목약명 | string | `-` | `-` |
| OutBlock_1 | `ISU_ENG_NM` | 영문 종목명 | string | `-` | `-` |
| OutBlock_1 | `LIST_DD` | 상장일 | string | `-` | `-` |
| OutBlock_1 | `MKT_TP_NM` | 시장구분 | string | `-` | `-` |
| OutBlock_1 | `SECUGRP_NM` | 증권구분 | string | `-` | `-` |
| OutBlock_1 | `SECT_TP_NM` | 소속부 | string | `-` | `-` |
| OutBlock_1 | `KIND_STKCERT_TP_NM` | 주식종류 | string | `-` | `-` |
| OutBlock_1 | `PARVAL` | 액면가 | string | `-` | `-` |
| OutBlock_1 | `LIST_SHRS` | 상장주식수 | string | `-` | `-` |

---

<a id="api-knx_isu_base_info"></a>

## 8. 코넥스 종목기본정보

| 항목 | 값 |
|---|---|
| API ID | `knx_isu_base_info` |
| 내부 명세 ID | `COgTLqgmGlqyJvaEFNIc` |
| 명세 버전 | `1.0` |
| 등록일 | 2022/05/06 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 코넥스 종목기본정보 ('13년07월01일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/sto/knx_isu_base_info` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/sto/knx_isu_base_info` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES002_S2.cmd?BO_ID=COgTLqgmGlqyJvaEFNIc) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `ISU_CD` | 표준코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_SRT_CD` | 단축코드 | string | `-` | `-` |
| OutBlock_1 | `ISU_NM` | 한글 종목명 | string | `-` | `-` |
| OutBlock_1 | `ISU_ABBRV` | 한글 종목약명 | string | `-` | `-` |
| OutBlock_1 | `ISU_ENG_NM` | 영문 종목명 | string | `-` | `-` |
| OutBlock_1 | `LIST_DD` | 상장일 | string | `-` | `-` |
| OutBlock_1 | `MKT_TP_NM` | 시장구분 | string | `-` | `-` |
| OutBlock_1 | `SECUGRP_NM` | 증권구분 | string | `-` | `-` |
| OutBlock_1 | `SECT_TP_NM` | 소속부 | string | `-` | `-` |
| OutBlock_1 | `KIND_STKCERT_TP_NM` | 주식종류 | string | `-` | `-` |
| OutBlock_1 | `PARVAL` | 액면가 | string | `-` | `-` |
| OutBlock_1 | `LIST_SHRS` | 상장주식수 | string | `-` | `-` |

---
