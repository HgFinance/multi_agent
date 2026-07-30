# LS증권 Open API 전체 참조

> [LS증권 Open API 공식 문서](https://openapi.ls-sec.co.kr/apiservice)를 2026-07-29 14:37:54 +09:00 에 수집한 개발용 인덱스입니다.

## 수집 결과

| 항목 | 결과 |
|---|---:|
| 대분류 | 8개 |
| API 묶음 | 42 개 |
| TR | 365 개 |
| 요청·응답 필드 | 16141 개 |
| 필드 조회 실패 | 0건 |
| 공식 상세 미제공 기록 | 2건 |

화면의 대분류 버튼, API 버튼, TR 상세 펼치기가 호출하는 공식 공개 API를 같은 순서로 순회했다. 각 API 문서에는 모든 TR과 요청 헤더, 요청 바디, 응답 헤더, 응답 바디 필드를 기록했다.

공식 사이트의 장문 설명과 대형 예제 Payload는 통째로 복제하지 않는다. 각 문서의 원문 링크에서 최신 설명과 예제를 확인하고, 이 저장소에서는 구현·검증에 필요한 전체 인터페이스 계약을 관리한다.

## 필드 형식 코드

| 코드 | 형식 |
|---|---|
| `A0001` | `String` |
| `A0002` | `Array` |
| `A0003` | `Object` |
| `A0004` | `Number` |
| `A0005` | `Object Array` |

## 전체 API 목록

### OAuth 인증

API 2개, TR 2개

| API | 프로토콜 | 방식 | 접속 경로 | TR |
|---|---|---|---|---:|
| [접근토큰 발급](01-oauth/01-33bd887a.md) | REST | POST | `/oauth2/token` | 1 |
| [접근토큰 폐기](01-oauth/02-2d923333.md) | REST | POST | `/oauth2/revoke` | 1 |

### 업종

API 3개, TR 9개

| API | 프로토콜 | 방식 | 접속 경로 | TR |
|---|---|---|---|---:|
| [\[업종\] 시세](02-industry/01-88a7c0d3.md) | REST | POST | `/indtp/market-data` | 5 |
| [\[업종\] 차트](02-industry/02-5b483d74.md) | REST | POST | `/indtp/chart` | 3 |
| [\[업종\] 실시간 시세](02-industry/03-3c2b0280.md) | WEBSOCKET | POST | `/websocket/indtp` | 1 |

### 주식

API 16개, TR 197개

| API | 프로토콜 | 방식 | 접속 경로 | TR |
|---|---|---|---|---:|
| [\[주식\] 시세](03-stock/01-54a99b02.md) | REST | POST | `/stock/market-data` | 25 |
| [\[주식\] 거래원](03-stock/02-3dbce945.md) | REST | POST | `/stock/exchange` | 3 |
| [\[주식\] 투자정보](03-stock/03-580d2770.md) | REST | POST | `/stock/investinfo` | 8 |
| [\[주식\] 프로그램](03-stock/04-6b554636.md) | REST | POST | `/stock/program` | 7 |
| [\[주식\] 투자자](03-stock/05-c148a42f.md) | REST | POST | `/stock/investor` | 7 |
| [\[주식\] 외인/기관](03-stock/06-90378c39.md) | REST | POST | `/stock/frgr-itt` | 3 |
| [\[주식\] ELW](03-stock/07-3d58c125.md) | REST | POST | `/stock/elw` | 20 |
| [\[주식\] ETF](03-stock/08-30b6dfd6.md) | REST | POST | `/stock/etf` | 5 |
| [\[주식\] 섹터](03-stock/09-8f027fa6.md) | REST | POST | `/stock/sector` | 5 |
| [\[주식\] 종목검색](03-stock/10-6b67369a.md) | REST | POST | `/stock/item-search` | 8 |
| [\[주식\] 상위종목](03-stock/11-d3d0ef41.md) | REST | POST | `/stock/high-item` | 9 |
| [\[주식\] 차트](03-stock/12-12320341.md) | REST | POST | `/stock/chart` | 7 |
| [\[주식\] 기타](03-stock/13-316495d3.md) | REST | POST | `/stock/etc` | 10 |
| [\[주식\] 계좌](03-stock/14-37d22d4d.md) | REST | POST | `/stock/accno` | 12 |
| [\[주식\] 주문](03-stock/15-d0e216e0.md) | REST | POST | `/stock/order` | 3 |
| [\[주식\] 실시간 시세](03-stock/16-9a2800c3.md) | WEBSOCKET | POST | `/websocket/stock` | 65 |

### 선물/옵션

API 7개, TR 91개

| API | 프로토콜 | 방식 | 접속 경로 | TR |
|---|---|---|---|---:|
| [\[선물/옵션\] 시세](04-derivatives/01-9f467798.md) | REST | POST | `/futureoption/market-data` | 30 |
| [\[선물/옵션\] 투자자](04-derivatives/02-47005ce6.md) | REST | POST | `/futureoption/investor` | 4 |
| [\[선물/옵션\] 차트](04-derivatives/03-a9b39b08.md) | REST | POST | `/futureoption/chart` | 5 |
| [\[선물/옵션\] 계좌](04-derivatives/04-09a668df.md) | REST | POST | `/futureoption/accno` | 13 |
| [\[선물/옵션\] 주문](04-derivatives/05-b579d38a.md) | REST | POST | `/futureoption/order` | 7 |
| [\[선물/옵션\] 기타](04-derivatives/06-98373ce4.md) | REST | POST | `/futureoption/etc` | 1 |
| [\[선물/옵션\] 실시간 시세](04-derivatives/07-57936c91.md) | WEBSOCKET | POST | `/websocket/futureoption` | 31 |

### 해외선물

API 5개, TR 35개

| API | 프로토콜 | 방식 | 접속 경로 | TR |
|---|---|---|---|---:|
| [\[해외선물\] 시세](05-overseas-futures/01-d61d4f85.md) | REST | POST | `/overseas-futureoption/market-data` | 14 |
| [\[해외선물\] 계좌](05-overseas-futures/02-44c1c082.md) | REST | POST | `/overseas-futureoption/accno` | 7 |
| [\[해외선물\] 주문](05-overseas-futures/03-b820f925.md) | REST | POST | `/overseas-futureoption/order` | 3 |
| [\[해외선물\] 차트](05-overseas-futures/04-906d2d0a.md) | REST | POST | `/overseas-futureoption/chart` | 4 |
| [\[해외선물\] 실시간 시세](05-overseas-futures/05-3dc1c51b.md) | WEBSOCKET | POST | `/websocket/overseas-futureoption` | 7 |

### 해외주식

API 5개, TR 24개

| API | 프로토콜 | 방식 | 접속 경로 | TR |
|---|---|---|---|---:|
| [\[해외주식\] 계좌](06-overseas-stock/01-45b5abe1.md) | REST | POST | `/overseas-stock/accno` | 4 |
| [\[해외주식\] 시세](06-overseas-stock/02-06f2b1bc.md) | REST | POST | `/overseas-stock/market-data` | 5 |
| [\[해외주식\] 실시간 시세](06-overseas-stock/03-0c023f96.md) | WEBSOCKET | POST | `/websocket/overseas-stock` | 7 |
| [\[해외주식\] 주문](06-overseas-stock/04-6bafc43c.md) | REST | POST | `/overseas-stock/order` | 4 |
| [\[해외주식\] 차트](06-overseas-stock/05-4903400b.md) | REST | POST | `/overseas-stock/chart` | 4 |

### 기타

API 3개, TR 4개

| API | 프로토콜 | 방식 | 접속 경로 | TR |
|---|---|---|---|---:|
| [\[기타\] 시간조회](07-misc/01-3c452f0d.md) | REST | POST | `/etc/time-search` | 1 |
| [\[기타\] 실시간 시세](07-misc/02-eddd61f7.md) | WEBSOCKET | POST | `/websocket/etc` | 2 |
| [\[기타\] 핀테크인](07-misc/03-507f87b5.md) | REST | POST | `/etc/fintechin` | 1 |

### 실시간 시세 투자정보

API 1개, TR 3개

| API | 프로토콜 | 방식 | 접속 경로 | TR |
|---|---|---|---|---:|
| [\[실시간 시세 투자정보\] 투자정보](08-realtime-investment/01-d67d0790.md) | WEBSOCKET | POST | `/websocket/investinfo` | 3 |

## 재수집 방법

저장소 루트에서 다음 명령을 실행한다.

``powershell
.\scripts\collect_ls_openapi_docs.ps1
``

수집 결과와 개수는 [manifest.json](manifest.json)에 기록된다. 재수집 전후의 API·TR·필드 개수 차이는 LS 문서 계약 변경으로 보고 검토한다.
