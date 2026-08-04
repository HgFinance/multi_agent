# 사용자 적합 포트폴리오 목록 계약

상태: TEST prototype

이 기능의 1차 목적은 사용자가 입력한 투자 성향, 투자 경험, 투자 기간, 감내 가능한 손실과 현금화 필요 기간을 바탕으로 사전에 등록된 포트폴리오 후보 목록을 제공하는 것이다. 이 결과는 투자 주문이나 자동 승인이 아니다.

## 흐름

```text
사용자 설문
  → InvestorProfile 정규화
  → 경험 수준을 반영한 유효 위험 상한 계산
  → 등록된 PortfolioCandidate 적합성 검사
  → Risk/QA 검증 컨텍스트
  → 근거가 연결된 포트폴리오 목록 + 제외 이유
```

Risk와 QA의 역할은 추천 문장을 생성하는 것이 아니다.

- Risk는 후보의 위험 수준이 사용자 프로필 상한과 손실 한도를 넘지 않는지 결정론적으로 확인한다.
- QA는 후보의 비중 합계, 근거 참조, 기준시각과 판정 재현성을 독립 검증한다.
- Hermes/LLM은 설명을 보조할 수 있지만 적합성 판정, 후보 생성, 한도 완화와 자동 주문을 소유하지 않는다.
- 일치 후보가 없으면 임의의 공격형 후보로 fallback하지 않고 `NO_MATCH`를 반환한다.

## 최소 입력

`InvestorProfile`은 다음을 요구한다.

| 필드 | 의미 |
|---|---|
| `mindset` | `SAFETY_FIRST`, `BALANCED`, `RISK_SEEKING` |
| `experience` | `BEGINNER`, `INTERMEDIATE`, `EXPERIENCED` |
| `investment_horizon_years` | 투자 가능 기간 |
| `max_drawdown_pct` | 허용 가능한 최대 손실폭 |
| `liquidity_need` | 자금 회수 긴급도 |
| `as_of` | 프로필 판단 기준시각 |

포트폴리오 후보는 위험 등급, 최소 경험, 최소 투자 기간, 최대 예상 손실폭, 최대 현금화 기간, 목표 비중 합계와 근거 참조를 가져야 한다. 근거와 기준시각이 없는 후보는 계약을 통과할 수 없다.

## 안전 규칙

`effective_risk_score = min(mindset_score, experience_score)`로 계산한다. 따라서 위험 선호도가 높아도 초보자에게 공격형 포트폴리오를 자동 추천하지 않는다. 모든 결과는 `manual_review_required=true`이며, 이 모듈은 주문·Risk 승인·Position·Ledger를 변경하지 않는다.

구현: [`suitability.py`](../../departments/05-accounting-portfolio/portfolio/suitability.py)
