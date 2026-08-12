# KRX Open API 지수 전체 참조

> [KRX 공식 서비스 화면](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES001_S1.cmd)과 상세 개발 명세를 2026-07-30 17:10:36 +09:00 에 구조화한 개발용 참조입니다. 실제 연동 전 승인된 서비스와 최신 계약을 다시 확인합니다.

## API 목록

API 5개

| 번호 | API | API ID | 제공 범위 | 최근 수정일 |
|---:|---|---|---|---|
| 1 | [KRX 시리즈 일별시세정보](#api-krx_dd_trd) | `krx_dd_trd` | KRX 시리즈 지수의 시세정보 제공 ('10년01월04일 데이터부터 제공) | 2026/01/16 |
| 2 | [KOSPI 시리즈 일별시세정보](#api-kospi_dd_trd) | `kospi_dd_trd` | KOSPI 시리즈 지수의 시세정보 제공 ('10년01월04일 데이터부터 제공) | 2026/01/16 |
| 3 | [KOSDAQ 시리즈 일별시세정보](#api-kosdaq_dd_trd) | `kosdaq_dd_trd` | KOSDAQ 시리즈 지수의 시세정보 제공 ('10년01월04일 데이터부터 제공) | 2026/01/16 |
| 4 | [채권지수 시세정보](#api-bon_dd_trd) | `bon_dd_trd` | 채권지수의 시세정보 제공 ('10년01월04일 데이터부터 제공) | 2026/01/16 |
| 5 | [파생상품지수 시세정보](#api-drvprod_dd_trd) | `drvprod_dd_trd` | 파생상품지수의 시세정보를 제공 ('10년01월04일 데이터부터 제공) | 2026/01/16 |

---

<a id="api-krx_dd_trd"></a>

## 1. KRX 시리즈 일별시세정보

| 항목 | 값 |
|---|---|
| API ID | `krx_dd_trd` |
| 내부 명세 ID | `SsgXTEspyJESKvyXZtCU` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/15 |
| 최근 수정일 | 2026/01/16 |
| 설명 | KRX 시리즈 지수의 시세정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/idx/krx_dd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/idx/krx_dd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES001_S2.cmd?BO_ID=SsgXTEspyJESKvyXZtCU) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `IDX_CLSS` | 계열구분 | string | `-` | `-` |
| OutBlock_1 | `IDX_NM` | 지수명 | string | `-` | `-` |
| OutBlock_1 | `CLSPRC_IDX` | 종가 | string | `###0.00` | `-` |
| OutBlock_1 | `CMPPREVDD_IDX` | 대비 | string | `-` | `-` |
| OutBlock_1 | `FLUC_RT` | 등락률 | string | `-` | `-` |
| OutBlock_1 | `OPNPRC_IDX` | 시가 | string | `###0.00` | `-` |
| OutBlock_1 | `HGPRC_IDX` | 고가 | string | `###0.00` | `-` |
| OutBlock_1 | `LWPRC_IDX` | 저가 | string | `###0.00` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `MKTCAP` | 상장시가총액 | string | `###0` | `-` |

---

<a id="api-kospi_dd_trd"></a>

## 2. KOSPI 시리즈 일별시세정보

| 항목 | 값 |
|---|---|
| API ID | `kospi_dd_trd` |
| 내부 명세 ID | `EREKZauXnMmxyIlqzeDN` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/15 |
| 최근 수정일 | 2026/01/16 |
| 설명 | KOSPI 시리즈 지수의 시세정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/idx/kospi_dd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/idx/kospi_dd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES001_S2.cmd?BO_ID=EREKZauXnMmxyIlqzeDN) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `IDX_CLSS` | 계열구분 | string | `-` | `-` |
| OutBlock_1 | `IDX_NM` | 지수명 | string | `-` | `-` |
| OutBlock_1 | `CLSPRC_IDX` | 종가 | string | `###0.00` | `-` |
| OutBlock_1 | `CMPPREVDD_IDX` | 대비 | string | `-` | `-` |
| OutBlock_1 | `FLUC_RT` | 등락률 | string | `-` | `-` |
| OutBlock_1 | `OPNPRC_IDX` | 시가 | string | `###0.00` | `-` |
| OutBlock_1 | `HGPRC_IDX` | 고가 | string | `###0.00` | `-` |
| OutBlock_1 | `LWPRC_IDX` | 저가 | string | `###0.00` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `MKTCAP` | 상장시가총액 | string | `###0` | `-` |

---

<a id="api-kosdaq_dd_trd"></a>

## 3. KOSDAQ 시리즈 일별시세정보

| 항목 | 값 |
|---|---|
| API ID | `kosdaq_dd_trd` |
| 내부 명세 ID | `nimebcamqFNIPNcRrHoO` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/15 |
| 최근 수정일 | 2026/01/16 |
| 설명 | KOSDAQ 시리즈 지수의 시세정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/idx/kosdaq_dd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/idx/kosdaq_dd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES001_S2.cmd?BO_ID=nimebcamqFNIPNcRrHoO) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `IDX_CLSS` | 계열구분 | string | `-` | `-` |
| OutBlock_1 | `IDX_NM` | 지수명 | string | `-` | `-` |
| OutBlock_1 | `CLSPRC_IDX` | 종가 | string | `###0.00` | `-` |
| OutBlock_1 | `CMPPREVDD_IDX` | 대비 | string | `-` | `-` |
| OutBlock_1 | `FLUC_RT` | 등락률 | string | `-` | `-` |
| OutBlock_1 | `OPNPRC_IDX` | 시가 | string | `###0.00` | `-` |
| OutBlock_1 | `HGPRC_IDX` | 고가 | string | `###0.00` | `-` |
| OutBlock_1 | `LWPRC_IDX` | 저가 | string | `###0.00` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `MKTCAP` | 상장시가총액 | string | `###0` | `-` |

---

<a id="api-bon_dd_trd"></a>

## 4. 채권지수 시세정보

| 항목 | 값 |
|---|---|
| API ID | `bon_dd_trd` |
| 내부 명세 ID | `vMxIKCtPBUeRytCqkoFv` |
| 명세 버전 | `1.0` |
| 등록일 | 2022/07/04 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 채권지수의 시세정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/idx/bon_dd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/idx/bon_dd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES001_S2.cmd?BO_ID=vMxIKCtPBUeRytCqkoFv) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `BND_IDX_GRP_NM` | 지수명 | string | `-` | `-` |
| OutBlock_1 | `TOT_EARNG_IDX` | 총수익지수_종가 | string | `###0.00` | `-` |
| OutBlock_1 | `TOT_EARNG_IDX_CMPPREVDD` | 총수익지수_대비 | string | `###0.00` | `-` |
| OutBlock_1 | `NETPRC_IDX` | 순가격지수_종가 | string | `###0.00` | `-` |
| OutBlock_1 | `NETPRC_IDX_CMPPREVDD` | 순가격지수_대비 | string | `###0.00` | `-` |
| OutBlock_1 | `ZERO_REINVST_IDX` | 제로재투자지수_종가 | string | `###0.00` | `-` |
| OutBlock_1 | `ZERO_REINVST_IDX_CMPPREVDD` | 제로재투자지수_대비 | string | `###0.00` | `-` |
| OutBlock_1 | `CALL_REINVST_IDX` | 콜재투자지수_종가 | string | `###0.00` | `-` |
| OutBlock_1 | `CALL_REINVST_IDX_CMPPREVDD` | 콜재투자지수_대비 | string | `###0.00` | `-` |
| OutBlock_1 | `MKT_PRC_IDX` | 시장가격지수_종가 | string | `###0.00` | `-` |
| OutBlock_1 | `MKT_PRC_IDX_CMPPREVDD` | 시장가격지수_대비 | string | `###0.00` | `-` |
| OutBlock_1 | `AVG_DURATION` | 듀레이션 | string | `###0.000` | `-` |
| OutBlock_1 | `AVG_CONVEXITY_PRC` | 컨벡시티 | string | `###0.000` | `-` |
| OutBlock_1 | `BND_IDX_AVG_YD` | YTM | string | `###0.000` | `-` |

---

<a id="api-drvprod_dd_trd"></a>

## 5. 파생상품지수 시세정보

| 항목 | 값 |
|---|---|
| API ID | `drvprod_dd_trd` |
| 내부 명세 ID | `rPBjbLtScMwmSXWDOYPd` |
| 명세 버전 | `1.0` |
| 등록일 | 2022/07/04 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 파생상품지수의 시세정보를 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/idx/drvprod_dd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/idx/drvprod_dd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES001_S2.cmd?BO_ID=rPBjbLtScMwmSXWDOYPd) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `IDX_CLSS` | 계열구분 | string | `-` | `-` |
| OutBlock_1 | `IDX_NM` | 지수명 | string | `-` | `-` |
| OutBlock_1 | `CLSPRC_IDX` | 종가 | string | `###0.00` | `-` |
| OutBlock_1 | `CMPPREVDD_IDX` | 대비 | string | `###0.00` | `-` |
| OutBlock_1 | `FLUC_RT` | 등락률 | string | `###0.00` | `-` |
| OutBlock_1 | `OPNPRC_IDX` | 시가 | string | `###0.00` | `-` |
| OutBlock_1 | `HGPRC_IDX` | 고가 | string | `###0.00` | `-` |
| OutBlock_1 | `LWPRC_IDX` | 저가 | string | `###0.00` | `-` |

---
