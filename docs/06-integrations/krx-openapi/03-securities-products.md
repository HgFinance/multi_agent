# KRX Open API 증권상품 전체 참조

> [KRX 공식 서비스 화면](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES003_S1.cmd)과 상세 개발 명세를 2026-07-30 17:10:36 +09:00 에 구조화한 개발용 참조입니다. 실제 연동 전 승인된 서비스와 최신 계약을 다시 확인합니다.

## API 목록

API 3개

| 번호 | API | API ID | 제공 범위 | 최근 수정일 |
|---:|---|---|---|---|
| 1 | [ETF 일별매매정보](#api-etf_bydd_trd) | `etf_bydd_trd` | ETF(상장지수펀드)의 매매정보 제공 ('10년01월04일 데이터부터 제공) | 2026/01/16 |
| 2 | [ETN 일별매매정보](#api-etn_bydd_trd) | `etn_bydd_trd` | ETN(상장지수증권)의 매매정보 제공 ('14년11월17일 데이터부터 제공) | 2026/01/16 |
| 3 | [ELW 일별매매정보](#api-elw_bydd_trd) | `elw_bydd_trd` | ELW(주식위런트증권)의 매매정보 제공 ('10년01월04일 데이터부터 제공) | 2026/01/16 |

---

<a id="api-etf_bydd_trd"></a>

## 1. ETF 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `etf_bydd_trd` |
| 내부 명세 ID | `nrEpCLaZpoLCTzPUMxuF` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | ETF(상장지수펀드)의 매매정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/etp/etf_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/etp/etf_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES003_S2.cmd?BO_ID=nrEpCLaZpoLCTzPUMxuF) |

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
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `###0` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `###0` | `-` |
| OutBlock_1 | `FLUC_RT` | 등락률 | string | `###0.00` | `-` |
| OutBlock_1 | `NAV` | 순자산가치(NAV) | string | `###0.00` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `MKTCAP` | 시가총액 | string | `###0` | `-` |
| OutBlock_1 | `INVSTASST_NETASST_TOTAMT` | 순자산총액 | string | `###0` | `-` |
| OutBlock_1 | `LIST_SHRS` | 상장좌수 | string | `###0` | `-` |
| OutBlock_1 | `IDX_IND_NM` | 기초지수_지수명 | string | `-` | `-` |
| OutBlock_1 | `OBJ_STKPRC_IDX` | 기초지수_종가 | string | `###0.00` | `-` |
| OutBlock_1 | `CMPPREVDD_IDX` | 기초지수_대비 | string | `###0.00` | `-` |
| OutBlock_1 | `FLUC_RT_IDX` | 기초지수_등락률 | string | `###0.00` | `-` |

---

<a id="api-etn_bydd_trd"></a>

## 2. ETN 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `etn_bydd_trd` |
| 내부 명세 ID | `VujebrcOsZQMybnUuwLk` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | ETN(상장지수증권)의 매매정보 제공 ('14년11월17일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/etp/etn_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/etp/etn_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES003_S2.cmd?BO_ID=VujebrcOsZQMybnUuwLk) |

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
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `###0` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `###0` | `-` |
| OutBlock_1 | `FLUC_RT` | 등락률 | string | `###0.00` | `-` |
| OutBlock_1 | `PER1SECU_INDIC_VAL` | 지표가치(IV) | string | `###0.00` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `MKTCAP` | 시가총액 | string | `###0` | `-` |
| OutBlock_1 | `INDIC_VAL_AMT` | 지표가치총액 | string | `###0` | `-` |
| OutBlock_1 | `LIST_SHRS` | 상장증권수 | string | `###0` | `-` |
| OutBlock_1 | `IDX_IND_NM` | 기초지수_지수명 | string | `-` | `-` |
| OutBlock_1 | `OBJ_STKPRC_IDX` | 기초지수_종가 | string | `###0.00` | `-` |
| OutBlock_1 | `CMPPREVDD_IDX` | 기초지수_대비 | string | `###0.00` | `-` |
| OutBlock_1 | `FLUC_RT_IDX` | 기초지수_등락률 | string | `###0.00` | `-` |

---

<a id="api-elw_bydd_trd"></a>

## 3. ELW 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `elw_bydd_trd` |
| 내부 명세 ID | `brBhSEuDCUNpmfsCslfM` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | ELW(주식위런트증권)의 매매정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/etp/elw_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/etp/elw_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES003_S2.cmd?BO_ID=brBhSEuDCUNpmfsCslfM) |

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
| OutBlock_1 | `TDD_CLSPRC` | 종가 | string | `###0` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 대비 | string | `###0` | `-` |
| OutBlock_1 | `TDD_OPNPRC` | 시가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_HGPRC` | 고가 | string | `###0` | `-` |
| OutBlock_1 | `TDD_LWPRC` | 저가 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |
| OutBlock_1 | `MKTCAP` | 시가총액 | string | `###0` | `-` |
| OutBlock_1 | `LIST_SHRS` | 상장증권수 | string | `###0` | `-` |
| OutBlock_1 | `ULY_NM` | 기초자산_자산명 | string | `-` | `-` |
| OutBlock_1 | `ULY_PRC` | 기초자산_종가 | string | `-` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC_ULY` | 기초자산_대비 | string | `###0.##` | `-` |
| OutBlock_1 | `FLUC_RT_ULY` | 기초자산_등락률 | string | `###0.00` | `-` |

---
