# LS 계좌 TR 회계 소비 계약

상위 공식 필드 카탈로그: [주식 계좌/주문 전체 필드](14-37d22d4d.md)
LS 공식 서비스: [주식 계좌/주문 API](https://openapi.ls-sec.co.kr/apiservice?api_id=37d22d4d-83cd-40a4-a375-81b010a4a627)

## 목적과 권한

`portfolio-bff`가 LS `/stock/accno`의 읽기 전용 TR을 수집하고
`accounting.broker-evidence.v1`로 정규화한다. 이 계약은 회계 보고와 대사의 독립
근거이며 공식 원장이나 공식 NAV가 아니다. 모든 응답은
`authoritative=false`, `is_official=false`다. 주문, 분개 Posting, Break 종결,
NAV 확정 경로를 열지 않는다.

```text
LS OPEN API /stock/accno (read only)
  -> portfolio-bff: 연속조회·호출간격·캐시·자격 제거
  -> /internal/accounting/broker-evidence
  -> accounting_advisory_context: 행/필드 수 제한
  -> Accounting Hermes task attachment
  -> 회계 보고 설명 및 Accounting Engine 대사
```

## TR별 사용

| TR | 회계 정보 | 정규화 위치 |
|---|---|---|
| `CDPCQ04700` | 기간 거래, 정산액, 수수료·세금, 실현손익, 입출금 | `activity.settled_period` |
| `CSPAQ00600` | 종목·가격별 신용/대출 한도와 담보 | `credit_limit` |
| `CSPAQ12200` | 예수금, 주문/출금 가능액, 평가와 결제 예정 | `account_summary` |
| `CSPAQ12300` | BEP 기준 보유, 평가손익, 결제·담보 | `positions`, `account_summary` |
| `CSPAQ13700` | 당일 주문·체결 이력 | `activity.order_history` |
| `CSPAQ22200` | 예수금·주문가능액 2차 대조 | `account_cross_checks` |
| `CSPBQ00200` | 종목·가격·매매구분별 증거금 주문가능수량 | `margin_capacity` |
| `FOCCQ33600` | 기간 수익률 요약과 일별 시계열 | `performance` |
| `t0150` | 당일 매매일지와 수수료·세금 | `activity.today` |
| `t0151` | 지정 전일 매매일지와 수수료·세금 | `activity.previous_day` |
| `t0424` | 체결 기준 잔고와 평균단가 대조 | `position_check`, `position_reconciliation` |
| `t0425` | 체결/미체결 주문 현황 | `activity.execution_status` |

계좌 전체 정기 보고는 10개 TR을 수집한다. `CSPAQ00600`은 대출상세분류,
종목, 주문가가 필요하고 `CSPBQ00200`은 매매구분, 종목, 주문가가 필요하므로
입력이 없는 기본 보고에서 `NEEDS_PARAMETERS`로 남긴다. 임의 종목이나 시세를
만들어 호출하지 않는다.

## 연속조회와 완전성

- BFF는 응답 헤더 `tr_cont`와 `tr_cont_key`를 다음 요청에 전달한다.
- `t0150`, `t0151`은 OutBlock의 네 CTS 필드, `t0424`는 `cts_expcode`,
  `t0425`는 `cts_ordno`도 전달한다.
- 반복 토큰 또는 설정된 최대 페이지에 도달하면 `complete=false`,
  `truncated=true`로 남긴다.
- TR 하나의 실패가 다른 결과를 지우지 않는다. 각 `coverage[TR]`은 `OK`,
  `EMPTY`, `ERROR`, `UNAVAILABLE`, `NEEDS_PARAMETERS` 중 하나이며 실패 사유와
  페이지 완전성을 보존한다.
- 계좌번호는 끝 네 자리만 남기고 비밀번호, 계좌명, 의뢰인명은 계약에 싣지 않는다.

## 회계 해석 규칙

1. Accounting Engine snapshot이 수치 정본이다. LS 값은 대사와 보고 설명 근거다.
2. `CSPAQ12300`의 BEP 단가와 `t0424`의 평균단가는 의미가 다르므로 별도 표시한다.
3. `CDPCQ04700` 결제 활동과 `t0150/t0151` 매매일 활동을 단순 합산하지 않는다.
4. D+1/D+2 결제예정액, 출금가능액, 현금주문가능액을 서로 대체하지 않는다.
5. `CSPAQ13700/t0425` 미체결 주문을 포지션이나 실현 거래로 잡지 않는다.
6. `account_cross_checks`, `position_reconciliation`, `exceptions`의 차이는 열린
   Break 후보로 보고한다. 이를 맞추려고 분개를 수정하거나 차이를 0으로 만들지 않는다.

런타임 해석 지침은
`skills/finance/ls-accounting-evidence/SKILL.md`와 그
`references/tr-mapping.md`가 소유한다.
