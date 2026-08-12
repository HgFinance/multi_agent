# KRX Open API 채권 전체 참조

> [KRX 공식 서비스 화면](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES004_S1.cmd)과 상세 개발 명세를 2026-07-30 17:10:36 +09:00 에 구조화한 개발용 참조입니다. 실제 연동 전 승인된 서비스와 최신 계약을 다시 확인합니다.

## API 목록

API 3개

| 번호 | API | API ID | 제공 범위 | 최근 수정일 |
|---:|---|---|---|---|
| 1 | [국채전문유통시장 일별매매정보](#api-kts_bydd_trd) | `kts_bydd_trd` | 국채전문유통시장에 상장되어있는 채권의 매매정보 제공 ('10년01월04일 데이터부터 제공) | 2026/01/16 |
| 2 | [일반채권시장 일별매매정보](#api-bnd_bydd_trd) | `bnd_bydd_trd` | 일반채권시장에 상장되어있는 채권의 매매정보 제공 ('10년01월04일 데이터부터 제공) | 2026/01/16 |
| 3 | [소액채권시장 일별매매정보](#api-smb_bydd_trd) | `smb_bydd_trd` | 소액채권시장에 상장되어있는 채권의 매매정보 제공 ('10년01월04일 데이터부터 제공) | 2026/01/16 |

---

<a id="api-kts_bydd_trd"></a>

## 1. 국채전문유통시장 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `kts_bydd_trd` |
| 내부 명세 ID | `CEnOyORzHgXWpdbUfWyf` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 국채전문유통시장에 상장되어있는 채권의 매매정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/bon/kts_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/bon/kts_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES004_S2.cmd?BO_ID=CEnOyORzHgXWpdbUfWyf) |

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
| OutBlock_1 | `BND_EXP_TP_NM` | 만기년수 | string | `-` | `-` |
| OutBlock_1 | `GOVBND_ISU_TP_NM` | 종목구분 | string | `-` | `-` |
| OutBlock_1 | `CLSPRC` | 종가_가격 | string | `###0.0` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 종가_대비 | string | `###0.0` | `-` |
| OutBlock_1 | `CLSPRC_YD` | 종가_수익률 | string | `###0.000` | `-` |
| OutBlock_1 | `OPNPRC` | 시가_가격 | string | `###0.0` | `-` |
| OutBlock_1 | `OPNPRC_YD` | 시가_수익률 | string | `###0.000` | `-` |
| OutBlock_1 | `HGPRC` | 고가_가격 | string | `###0.0` | `-` |
| OutBlock_1 | `HGPRC_YD` | 고가_수익률 | string | `###0.000` | `-` |
| OutBlock_1 | `LWPRC` | 저가_가격 | string | `###0.0` | `-` |
| OutBlock_1 | `LWPRC_YD` | 저가_수익률 | string | `###0.000` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |

---

<a id="api-bnd_bydd_trd"></a>

## 2. 일반채권시장 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `bnd_bydd_trd` |
| 내부 명세 ID | `JfStBNhXISpVVfBHgspT` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 일반채권시장에 상장되어있는 채권의 매매정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/bon/bnd_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/bon/bnd_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES004_S2.cmd?BO_ID=JfStBNhXISpVVfBHgspT) |

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
| OutBlock_1 | `CLSPRC` | 종가_가격 | string | `###0.0` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 종가_대비 | string | `###0.0` | `-` |
| OutBlock_1 | `CLSPRC_YD` | 종가_수익률 | string | `###0.000` | `-` |
| OutBlock_1 | `OPNPRC` | 시가_가격 | string | `###0.0` | `-` |
| OutBlock_1 | `OPNPRC_YD` | 시가_수익률 | string | `###0.000` | `-` |
| OutBlock_1 | `HGPRC` | 고가_가격 | string | `###0.0` | `-` |
| OutBlock_1 | `HGPRC_YD` | 고가_수익률 | string | `###0.000` | `-` |
| OutBlock_1 | `LWPRC` | 저가_가격 | string | `###0.0` | `-` |
| OutBlock_1 | `LWPRC_YD` | 저가_수익률 | string | `###0.000` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |

---

<a id="api-smb_bydd_trd"></a>

## 3. 소액채권시장 일별매매정보

| 항목 | 값 |
|---|---|
| API ID | `smb_bydd_trd` |
| 내부 명세 ID | `yrTTOsXuYzHprbWLuYzd` |
| 명세 버전 | `1.0` |
| 등록일 | 2020/09/22 |
| 최근 수정일 | 2026/01/16 |
| 설명 | 소액채권시장에 상장되어있는 채권의 매매정보 제공 ('10년01월04일 데이터부터 제공) |
| 승인 API URL | `https://data-dbg.krx.co.kr/svc/apis/bon/smb_bydd_trd` |
| 샘플 API URL | `https://data-dbg.krx.co.kr/svc/sample/apis/bon/smb_bydd_trd` |
| 응답 형식 | 기본 JSON, `.json` JSON, `.xml` XML |
| 인증 | HTTP 요청 헤더 `AUTH_KEY` |
| 공식 상세 | [KRX 원문](https://openapi.krx.co.kr/contents/OPP/USES/service/OPPUSES004_S2.cmd?BO_ID=yrTTOsXuYzHprbWLuYzd) |

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
| OutBlock_1 | `CLSPRC` | 종가_가격 | string | `###0.0` | `-` |
| OutBlock_1 | `CMPPREVDD_PRC` | 종가_대비 | string | `###0.0` | `-` |
| OutBlock_1 | `CLSPRC_YD` | 종가_수익률 | string | `###0.000` | `-` |
| OutBlock_1 | `OPNPRC` | 시가_가격 | string | `###0.0` | `-` |
| OutBlock_1 | `OPNPRC_YD` | 시가_수익률 | string | `###0.000` | `-` |
| OutBlock_1 | `HGPRC` | 고가_가격 | string | `###0.0` | `-` |
| OutBlock_1 | `HGPRC_YD` | 고가_수익률 | string | `###0.000` | `-` |
| OutBlock_1 | `LWPRC` | 저가_가격 | string | `###0.0` | `-` |
| OutBlock_1 | `LWPRC_YD` | 저가_수익률 | string | `###0.000` | `-` |
| OutBlock_1 | `ACC_TRDVOL` | 거래량 | string | `###0` | `-` |
| OutBlock_1 | `ACC_TRDVAL` | 거래대금 | string | `###0` | `-` |

---
