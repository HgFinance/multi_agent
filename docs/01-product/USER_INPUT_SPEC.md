# 사용자 입력 명세서 (온보딩)

> 문서 상태: **확정** — 2026-08-05 팀 논의 결정(A-2, B, C-1, D, E-1, F, G-2, H, I, K-2) 반영
> 작성: 영주님 (CEO Office) · 작성일: 2026-08-05
> 결정 배경: [USER_INPUT_SCOPE_ANALYSIS.md](USER_INPUT_SCOPE_ANALYSIS.md) — 조사 자료. 이 문서가 그 §5 안건의 결론이다.
> 구현 계약: [USER_INPUT_API_SPEC.md](../02-engineering/USER_INPUT_API_SPEC.md)
> 상위 기준: [HEDGE_FUND_MASTER_PLAN.md](../HEDGE_FUND_MASTER_PLAN.md), [PORTFOLIO_SUITABILITY_SPEC.md](PORTFOLIO_SUITABILITY_SPEC.md), [HEDGE_FUND_IMPLEMENTATION_BACKLOG.md](../02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md) F01

## 1. 설계 원칙 — 편의성 우선

이 제품의 사용자는 **종목을 잘 모르는 개인 투자자**다. 따라서 입력 설계의 기준은 "모든 한도를 정확히 받는 것"이 아니라 **"사용자를 귀찮게 하지 않으면서 안전한 정책을 만드는 것"**이다.

입력을 세 계층으로 나눈다.

| 계층 | 무엇을 | 사용자 부담 |
|---|---|---|
| **1. 화면 직접 선택** | 초보도 이미 알고 있는 항목 (예산, 통화, 자산 종류, 승인 방식) | 선택형만. 자유 입력 없음 |
| **2. 프리셋 자동 채움 (은닉)** | 비중 한도처럼 초보가 판단하기 어려운 수치 | **묻지 않음.** 고급 설정에서만 조정 |
| **3. 챗봇 대화** | 업종·종목 선호, 투자 기간처럼 대화가 자연스러운 항목 | 대화하면서 자연히 채워짐 |

배분 기준은 하나다. **사용자가 이미 답을 알고 있으면 화면에서 받고, 모르면 묻지 않거나 대화로 유도한다.**

## 2. 계층 1 — 화면에서 직접 선택 (왼쪽 화면)

전부 **선택형**이다. 숫자 입력은 예산 하나뿐이다.

| # | 항목 | 컨트롤 | 저장 위치 | 필수 |
|---|---|---|---|---|
| 1 | 투자 성향 | 3지선다 (안정추구 / 균형 / 위험선호) | `InvestorProfile.mindset` | ✔ |
| 2 | 투자 경험 | 3지선다 (초보 / 중급 / 고수) | `InvestorProfile.experience` | ✔ |
| 3 | 기준 자본(예산) | 숫자 입력 | `Mandate.risk_bounds.base_capital` | ✔ |
| 4 | 통화 | 드롭다운 | `Mandate.risk_bounds.currency` | ✔ (Fund 통화로 기본 채움) |
| 5 | 자산군 허용/금지 | 토글 카드 7종 | `Mandate.universe_policy.{allowed,forbidden}_asset_classes` | ✔ |
| 6 | 전체 최대 손실 | 슬라이더 (%) | `Mandate.risk_bounds.max_drawdown_pct` + `InvestorProfile.max_drawdown_pct` | ✔ |
| 7 | 일일 최대 손실 | 슬라이더 (%) | `Mandate.risk_bounds.max_daily_loss` | ✔ |
| 8 | 주문 승인 방식 | 2지선다 (자동 / 매 주문 승인) | `Mandate.approval_rules.paper_order_mode` | ✔ |

**결정 근거**

- 1·2번은 프리셋(§3)의 두 축이므로 반드시 먼저 받는다. 성향은 3단계를 유지한다(**E-1**).
- 6·7번은 전체·일일 한도를 **둘 다** 받는다(**B**). 프리셋 값이 미리 채워져 있어 사용자는 조정만 하면 된다.
- 8번은 현재 On/Off 2단계를 유지한다(**F**). 세분화는 이후 과제다.
- 5번의 자산군 7종은 화면 시안을 따른다: 단일 주식 · ETF · 레버리지 · 선물 · 옵션 · 파생상품 · 암호화폐(**C-1**).

## 3. 계층 2 — 프리셋으로 자동 채움 (화면에서 은닉)

**투자 경험 3단계 × 투자 성향 3단계 = 9개 프리셋**을 정의한다(**H**, **I**). 사용자가 §2의 1·2번을 고르는 순간 아래 값이 전부 채워지고, **화면에는 보이지 않는다.** "고급 설정" 접기를 펼쳤을 때만 노출·조정 가능하다(**A-2**).

### 3.1 프리셋이 채우는 필드

| 필드 | 왜 은닉하나 |
|---|---|
| `max_instrument_weight` | 초보가 적정값을 판단할 근거가 없다 |
| `max_sector_weight` | 위와 동일. 값 자체는 사용자 설정값으로 취급한다(**D**) — 프리셋은 기본값일 뿐이고 고급 설정에서 바꿀 수 있다 |
| `max_gross_exposure` | 레버리지 개념이 필요해 초보에게 어렵다 |
| `max_concurrent_positions` | 분산 정도는 성향에서 도출하는 편이 정확하다 |

### 3.2 프리셋 매핑표 — **수치 미정, 정의 필요**

| 경험 \ 성향 | 안정추구 | 균형 | 위험선호 |
|---|---|---|---|
| **초보** | (정의 필요) | (정의 필요) | (정의 필요) |
| **중급** | (정의 필요) | (정의 필요) | (정의 필요) |
| **고수** | (정의 필요) | (정의 필요) | (정의 필요) |

각 칸은 §3.1의 4개 필드 값을 갖는다. **수치는 이 문서가 정하지 않는다** — 동규님(리스크) 확정 사항이다.

어떤 값을 넣든 아래 결정론 제약을 만족해야 하며, 위반 시 서버가 거절한다.

```
max_instrument_weight ≤ max_sector_weight ≤ max_gross_exposure
max_daily_loss ≤ max_drawdown_pct        (신규 제약, §5 참고)
0 < 모든 비중 ≤ 1                          (max_gross_exposure 제외, 1.0 초과 가능)
max_concurrent_positions > 0
```

또한 `effective_risk_score = min(mindset, experience)` 계약상 **초보 × 위험선호**는 실질 위험 등급이 `LOW`로 내려간다. 프리셋 수치도 이 방향과 모순되지 않아야 한다(초보 칸이 고수 칸보다 공격적이면 안 된다).

### 3.3 프리셋의 성격

- **서버가 관리하는 상수가 아니다.** 화면(프론트엔드) 상수로 두고, 화면이 값을 채워 서버로 전송한다(**H**).
- **버전 관리 대상이 아니다.** 프리셋이 바뀔 일이 없을 것으로 보기 때문이다(**H**).
- 따라서 서버 계약은 바뀌지 않는다 — 서버는 여전히 완전한 `MandatePolicy`를 받고 전 필드를 검증한다. **은닉은 화면의 표현일 뿐 전송 생략이 아니다.**

### 3.4 프리셋 없이 고정하는 값

사용자에게 묻지도, 프리셋으로 다루지도 않는다.

| 필드 | 고정값 | 근거 |
|---|---|---|
| `universe_policy.allowed_markets` | `["KRX"]` | 현재 단일 시장 |
| `universe_policy.trading_start` / `trading_end` | 정규장 시간 | 사용자가 좁힐 이유가 없다 |
| `approval_rules.risk_expansion_requires_user_approval` | `true` | 기본값 유지가 안전 방향 |
| `effective_from` | 즉시 | |

## 4. 계층 3 — 챗봇 대화로 채움

CEO Console의 AI Assistant와 대화하면서 채운다. 사용자가 대화를 건너뛰어도 온보딩은 완료된다(**전부 선택 항목**).

| # | 항목 | 저장 위치 | LLM 정제 |
|---|---|---|---|
| 9 | 투자 기간 | `InvestorProfile.investment_horizon_years` | ✔ "10년쯤 묻어둘 생각" → `10` |
| 10 | 자금 회수 긴급도 | `InvestorProfile.liquidity_need` | ✔ "당장 쓸 돈은 아니에요" → `LOW` |
| 11 | 선호 업종 | `Mandate.universe_policy.preferred_sectors` | ✔ "반도체 쪽에 관심 있어요" → KRX 업종 코드(**D**) |
| 12 | 제외 업종 | `Mandate.universe_policy.excluded_sectors` | ✔ 위와 동일 |
| 13 | 허용 종목 | `Mandate.allowed_assets` | ✔ 종목명 → `instrument_id` |
| 14 | 금지 종목 | `Mandate.forbidden_assets` | ✔ "담배 회사는 빼주세요" → `instrument_id` 목록 |
| 15 | 투자 목표 서술 | `Mandate.objective_text` | ✔ 대화 요약 → 초안 문장 |
| 16 | 고급 한도 조정 | §3.1 필드 | ✔ "조금 더 공격적으로" → 프리셋 대비 조정안 제시 |

### 4.1 LLM이 해도 되는 것과 안 되는 것

`suitability.py`가 **LLM의 성향 추론을 명시적으로 금지**한다. 이 경계를 지킨다.

| 구분 | 내용 |
|---|---|
| ✅ **허용** | 자연어 → 구조화 값 **제안** (업종 코드, 종목 ID, 연수, 긴급도 등). 별칭·동의어 매칭. 목표 문장 정리. 파라미터 영향 설명 |
| ❌ **금지** | `mindset`(투자 성향)·`experience`(투자 경험) 추론. 한도 값의 적정성 **판정**. 사용자 확인 없는 확정. 제약 위반 값의 자동 완화 |

**모든 LLM 산출물은 "제안"이며, 사용자가 화면에서 확인해야 확정된다.** 확정된 값은 다시 `policy.py`의 결정론 검증을 통과해야 저장된다. 이는 개발 원칙 2·10과 §11.5 권한 경계를 따른 것이다.

### 4.2 업종 매칭 방식 (**D**)

- 분류 체계는 **KRX 업종 코드**를 쓴다.
- 종목 → 업종 매핑은 별도 저장 테이블을 만들지 않고 **필요할 때마다 조회해 구성**한다.
- 사용자 자연어는 **별칭 사전**을 거쳐 업종 코드로 매칭한다("반도체", "칩", "semiconductor" → 같은 코드).
- `max_sector_weight`는 사용자 설정값이다(프리셋 기본값 + 고급 설정 조정).
- **집행 주체는 리스크본부**, **집행 시점은 전략이 완성되어 포트폴리오 풀을 구성할 때의 자가점검**이다.

> ⚠️ 현재 `reference.issuers.industry_code`는 **OpenDART 표준산업분류**이고 대부분 NULL이다. KRX 업종 코드와 다른 체계이므로 그대로 쓸 수 없다 — 별도 수집이 필요하다(§6 재일님 작업).

## 5. 자연어 입력 처리 (**K-2** 균형안)

- 선택지 조합으로 정책을 만들고, **자연어는 비워둘 수 있는 보조 입력**으로 둔다.
- `objective_text`는 DB `not null`이므로 사용자가 아무것도 쓰지 않으면 **선택 결과에서 자동 생성**한다(예: "균형 성향 · 장기 · 국내주식 중심").
- 챗봇 대화를 했다면 그 요약을 초안으로 제시하고 사용자가 수정할 수 있게 한다.
- 자연어는 **판정에 쓰지 않는다.** 저장되고, Risk/QA 검토자에게 맥락으로 전달될 뿐이다.

## 6. 스키마 신설·수정 요약

| 대상 | 변경 | 담당 |
|---|---|---|
| `policy.py` `UniversePolicy` | `allowed_asset_classes`, `forbidden_asset_classes` 신설(**C-1**) | 영주 |
| `policy.py` `UniversePolicy` | `preferred_sectors`, `excluded_sectors` 신설(**D**) | 영주 |
| `policy.py` `RiskBounds` | `max_drawdown_pct` 신설(**B**) | 영주 |
| `policy.py` `classify_change()` | 신규 필드의 TIGHTEN/LOOSEN 판정 추가 | 영주 |
| `accounting.investor_profiles` | **테이블 신설**(**G-2**) | 도현 |
| `reference.instruments.asset_class` | 표준값 확정 (현재 CHECK 없음) | 도현·재일 |
| KRX 업종 코드 · 종목↔업종 조회 | 신규 수집·조회 경로 | 재일 |
| 업종 별칭 사전 | 신설 | 재일 |

`governance.mandate_versions`는 `universe_policy`·`risk_bounds`가 jsonb라 **DB 마이그레이션이 필요 없다.** 내부 계약만 `policy.py`에서 확장한다.

상세 계약은 [USER_INPUT_API_SPEC.md](../02-engineering/USER_INPUT_API_SPEC.md)를 따른다.

## 7. 본부별 작업

### 영주 (CEO Office / Governance)

1. `policy.py` 확장 — `UniversePolicy` 4개 필드, `RiskBounds` 1개 필드, 상호 모순 validator 추가
2. `classify_change()` 확장 — 자산군·업종·`max_drawdown_pct`의 확대/완화 판정
3. `GET /governance/v1/mandates/.../current` 응답에 `policy` 전체 포함 (현재 3개 필드만 반환)
4. 챗봇 제안 API 계약 정의·구현
5. 프리셋 매핑표 문서화 (수치는 동규님 확정 후 반영)

### 동규 (리스크 / AI QA·감사)

1. **프리셋 9칸 수치 확정** (§3.2) — Risk 관점 적정성
2. **성향↔한도 매핑 검증** — 고급 설정에서 프리셋을 벗어난 값을 넣었을 때의 처리 규칙(경고/차단/검토 승격) 정의
3. `max_sector_weight` 집행 구현 — 포트폴리오 풀 구성 시 자가점검(**D**)
4. `forbidden_asset_classes` 집행 구현
5. LLM 제안 → 사용자 확인 → 결정론 검증 경로의 QA 검증 (§4.1 경계가 실제로 지켜지는지)

### 도현 (트레이딩 / 회계·포트폴리오, 공통 Frontend Platform)

1. `accounting.investor_profiles` 마이그레이션 + Repository (**G-2**)
2. `suitability.py`의 `InvestorProfile`을 저장소와 연결
3. BFF에 governance·portfolio Router 등록 (현재 `apps/api`에 없음)
4. 온보딩 화면 구현 — 프리셋 적용, 고급 설정 접기, 챗봇 패널
5. `reference.instruments.asset_class` 표준값 확정·적용

### 재일 (리서치 / 퀀트·백테스트)

1. **KRX 업종 코드 수집** — 현재 `industry_code`는 DART 분류라 사용 불가
2. 종목 → 업종 코드 조회 경로 제공 (매번 구성, 별도 저장 없음)
3. **업종 별칭 사전** 구축 (자연어 → 업종 코드)
4. 종목명 → `instrument_id` 검색 제공 (기존 evidence 검색 활용 가능)

## 8. 아직 정해지지 않은 것

이 문서는 아래를 정하지 않는다. 담당자 확정 후 이 문서를 갱신한다.

1. **프리셋 9칸 수치** (§3.2) — 동규님
2. **`asset_class` 표준 코드값** — 화면 7종의 실제 코드 문자열
3. **`max_drawdown_pct` 이중 저장 정합** — `Mandate.risk_bounds`와 `InvestorProfile` 양쪽에 두는데, 두 값이 항상 같아야 하는지 아니면 독립인지
4. **프리셋 이탈 시 처리** — 고급 설정에서 프리셋보다 공격적인 값을 넣으면 경고만 할지, 차단할지, Risk 검토로 승격할지
5. **주문 승인 세분화** — **F**에서 이후 과제로 미룸

## 9. 전제 — 한도는 아직 집행되지 않는다

[USER_INPUT_SCOPE_ANALYSIS.md](USER_INPUT_SCOPE_ANALYSIS.md) 부록 A.9 §L의 구현 공백이 남아 있다. Risk Engine은 여전히 `risk.policies`·`risk.limits`를 읽고 `governance.mandate_versions`의 값을 읽지 않는다.

**따라서 이 문서대로 입력을 받아도, 그 공백이 메워지기 전까지 주문이 그 한도로 차단되지 않는다.** 화면 문구에 "이 한도로 차단됩니다"라고 쓰지 않는다 — 없는 보호를 있다고 알리는 것이 된다.
