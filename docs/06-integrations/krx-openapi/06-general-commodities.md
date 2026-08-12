# KRX Open API 일반상품 전체 참조

> [KRX 공식 서비스 화면](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES006_S1.cmd)과 상세 개발 명세를 2026-07-30 17:10:36 +09:00 에 구조화한 개발용 참조입니다. 실제 연동 전 승인된 서비스와 최신 계약을 다시 확인합니다.

## API 목록

API 3개

| 번호 | API | API ID | 제공 범위 | 최근 수정일 |
|---:|---|---|---|---|
| 1 | [석유시장 일별매매정보](#api-oil_bydd_trd) | `oil_bydd_trd` | KRX 석유시장의 매매정보 제공 ('12년03월30일 데이터부터 제공) | 2026/01/16 |
| 2 | [금시장 일별매매정보](#api-gold_bydd_trd) | `gold_bydd_trd` | KRX 금시장 매매정보 제공 ('14년03월24일 데이터부터 제공) | 2026/01/16 |
| 3 | [배출권 시장 일별매매정보](#api-ets_bydd_trd) | `ets_bydd_trd` | KRX 탄소배출권 시장의 매매정보 제공 ('15년01월12일 데이터부터 제공) | 2026/01/16 |

---

<a id="api-oil_bydd_trd"></a>

## 1. 석유시장 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `oil_bydd_trd` |
| 내부 명세 ID | `rTvrZvAFKfcaLPOggJtW` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | KRX 석유시장의 매매정보 제공 ('12년03월30일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/gen/oil_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/gen/oil_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES006_S2.cmd?BO_ID=rTvrZvAFKfcaLPOggJtW) |

### 요청 인자

| 블록 | 요청 키 | 명칭 | 타입 | 길이 | 공식 샘플 값 |
|---|---|---|---|---:|---|
| InBlock_1 | `basDd` | 기준일자 | string | 8 | `20200414` |

### 응답 필드

| 블록 | 출력 키 | 항목명 | 타입 | 형식 | 기본값 |
|---|---|---|---|---|---|
| OutBlock_1 | `BAS_DD` | 기준일자 | string | `-` | `-` |
| OutBlock_1 | `OIL_NM` | 유종구분 | string | `-` | `-` |
| OutBlock_1 | `WT_AVG_PRC` | 가중평균가격_경쟁 | string | `###0.00` | `-` |
| OutBlock_1 | `WT_DIS_AVG_PRC` | 가중평균가격_협의 | string | `###0.00` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |

---

<a id="api-gold_bydd_trd"></a>

## 2. 금시장 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `gold_bydd_trd` |
| 내부 명세 ID | `sxveSnWzWNzWxQASsgEG` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | KRX 금시장 매매정보 제공 ('14년03월24일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/gen/gold_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/gen/gold_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES006_S2.cmd?BO_ID=sxveSnWzWNzWxQASsgEG) |

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
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `####` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `####` | `-` |
| OutBlock_1 | `FLUC_RT` | 등락률 | string | `###0.00` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `####` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `####` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `####` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `####` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `####` | `-` |

---

<a id="api-ets_bydd_trd"></a>

## 3. 배출권 시장 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `ets_bydd_trd` |
| 내부 명세 ID | `IZiYdcgRQFMeENJPEMKG` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | KRX 탄소배출권 시장의 매매정보 제공 ('15년01월12일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/gen/ets_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/gen/ets_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES006_S2.cmd?BO_ID=IZiYdcgRQFMeENJPEMKG) |

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
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `####` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `####` | `-` |
| OutBlock_1 | `FLUC_RT` | 등락률 | string | `###0.00` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `####` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `####` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `####` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `####` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `####` | `-` |

---
