# 사용자 입력 범위 분석과 미결정 안건

> ✅ **2026-08-05 팀 논의로 §5 안건이 결정됐다. 결론은 이 문서가 아니라 아래에 있다.**
>
> - 제품 계약: [USER_INPUT_SPEC.md](USER_INPUT_SPEC.md) — 무엇을 어떻게 받는지
> - 구현 계약: [USER_INPUT_API_SPEC.md](../02-engineering/USER_INPUT_API_SPEC.md) — 스키마·Route 변경
>
> 결정 요약: A-2(프리셋 + 고급설정 은닉) · B(전체·일일 한도 둘 다) · C-1(자산군 신규 필드) ·
> D(KRX 업종 코드, 리스크본부 집행) · E-1(성향 3단계 유지) · F(승인 2단계 유지) ·
> G-2(InvestorProfile은 회계/포트폴리오본부 소유) · H·I(경험×성향 9칸 프리셋) · K-2(선택형 + 자연어 보조)
>
> **이 문서는 그 결정에 이르기까지의 조사 기록으로 보존한다.** §5의 선택지 서술은 이미 결론이 난 내용이므로
> 설계 근거로 인용하지 않는다. 다만 §1~§4와 부록 A(속성 전수 표, Risk 어휘 불일치)는 **여전히 유효한 현황
> 조사**다.

> ⚠️ **이 문서는 명세(Spec)가 아니다. 확정된 계약이 아니며 구현 근거로 쓸 수 없다.**
>
> - 문서 종류: **분석·논의 자료** (결정 전 단계)
> - 작성: 영주님 (CEO Office) · 작성일: 2026-08-05
> - §1~§4와 부록 A는 **코드·DB를 읽어 확인한 현황**이다(사실).
> - §5와 부록 A.9의 안건 A~L은 **전부 미결정**이다. 어느 것도 결정으로 읽지 않는다.
> - 결정이 나면 [ADR](../02-engineering/adr/)로 승인한 뒤, 해당 계약 문서(`policy.py`, `suitability.py`, 하위 Spec)를 같은 변경에서 함께 갱신한다. 이 문서는 그때 갱신하거나 폐기한다.
>
> 상위 기준: [HEDGE_FUND_MASTER_PLAN.md](../HEDGE_FUND_MASTER_PLAN.md) §첫 사용자 가치, [PORTFOLIO_SUITABILITY_SPEC.md](PORTFOLIO_SUITABILITY_SPEC.md), [HEDGE_FUND_IMPLEMENTATION_BACKLOG.md](../02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md) F01
> 관련 문서: [USER_INPUT_API_SPEC.md](../02-engineering/USER_INPUT_API_SPEC.md) — governance-api의 실제 Route·응답·에러·멱등성 현황
>
> 화면 설계 문서는 두지 않는다. §5 결정 전에 UI를 확정하면 미결정 사항을 고른 셈이 되므로, 결정 후 새로 작성한다.

이 문서는 **"사용자에게 무엇을 물어보고, 그 값이 어디로 가는가"** 하나만 다룬다. 화면 레이아웃과 API Transport는 위 하위 문서가 담당하며 여기서 중복하지 않는다.

목적은 두 가지다.

1. 흩어져 있는 사용자 입력 계약(투자 성향 / Mandate / 사용자 설정)의 **현재 상태를 조사해 한 장에 모은다.**
2. Mandate로 받을 항목을 확정하기 위한 **팀 논의 안건을 §5에 정리**한다. 이 문서는 미결정 항목을 임의로 정하지 않는다(문서 규칙: 미결정 사항 임의 결정 금지).

## 0. 이 문서가 ADR이 아닌 이유와, ADR로 만들 단위

ADR은 **하나의 결정**을 배경·선택지·결정·결과로 기록한다([ADR-0001](../02-engineering/adr/0001-hermes-kanban-agent-status-bridge.md) 참고). 이 문서는 결정이 12개 열려 있고 아직 어느 것도 고르지 않았으므로 ADR이 아니라 **ADR을 쓰기 전의 조사 자료**다.

§5 안건이 확정되면 아래 단위로 ADR을 나누기를 제안한다(12개를 각각 쓰지 않는다).

| ADR 후보 | 묶는 안건 | 왜 한 덩어리인가 | 결정 주도 |
|---|---|---|---|
| 사용자 입력 최소 집합 | A, B, K | "무엇을 묻고 무엇을 자동으로 채우나"가 한 결정 | 영주 |
| 투자자 프로필 저장과 성향 체계 | E, G | 저장소 위치와 단계 수는 함께 정해야 함 | 도현 |
| Universe 분류 확장(상품유형·업종) | C, D | 둘 다 분류 taxonomy 확정이 선행 | 재일 |
| 포트폴리오 후보 카탈로그 생성 주체 | J | 제품 방향 결정. 마스터플랜 영향 가능 | 전원 |

F(승인 세분화)·H(프리셋)·I(정렬도)는 위 결정에 종속되므로 별도 ADR을 먼저 쓰지 않는다.

**L(Mandate → Risk 집행 경로)은 ADR 후보가 아니다.** 설계는 `UNIFIED_DOMAIN_API_SPEC.md` Governance/Risk 절에 이미 확정돼 있고 구현만 안 된 상태다 — ADR이 아니라 구현 백로그로 다룬다(A.9 §L). 다만 이 공백 때문에 **위 4개 ADR을 어떻게 결정하든 당장은 한도가 집행되지 않는다**는 점을 논의 전제로 공유해야 한다.

## 1. 사용자 입력은 3개의 다른 객체다

같은 입력 화면에 있어도 **성격과 소유 부서, 실패 시 동작이 다르다.** 하나로 합치지 않는다.

| 객체 | 계약 위치 | 담는 것 | 성격 | 소유 |
|---|---|---|---|---|
| `InvestorProfile` | `departments/05-accounting-portfolio/portfolio/suitability.py` | 투자 성향, 경험, 투자 기간, 손실 감내도, 유동성 요구 | **무엇을 추천받을지** — advisory, 주문·승인과 분리 | 회계/포트폴리오본부 |
| `MandatePolicy` | `departments/00-ceo-office/src/mandate/policy.py` | 자본, 통화, 비중 한도, 자산 목록, 승인 방식 | **에이전트가 무엇을 못 하게 할지** — 집행 한도 | CEO Office(정책) / Risk Engine(집행) |
| `UserPreferences` | `governance.user_preferences` (DB만 존재) | 리포트 주기, 알림, 설명 수준 | **어떻게 보여줄지** — 표현 계층 | CEO Office |

**왜 나누는가**: `InvestorProfile`은 틀려도 "덜 적합한 추천"으로 끝나지만, `MandatePolicy`는 틀리면 **한도가 잘못 집행된다.** 그래서 Mandate만 Version·승인·감사(Risk/QA 검토 → 사용자 승인)를 거치고, InvestorProfile은 그 경로를 타지 않는다.

이 구분은 마스터플랜이 이미 정한 것이다 — 추천은 "주문이나 투자 승인과 분리된 advisory 결과"이며 적합 후보가 없으면 `NO_MATCH`를 반환하고 위험한 후보로 대체하지 않는다.

## 2. 전체 입력 항목과 귀속

화면 시안(Mandate Configuration [F01])과 팀에서 나온 8개 항목을 합친 전체 목록이다.

### 2.1 확정 — 계약과 구현이 이미 있는 항목

| 사용자 입력 | 귀속 | 필드 | 비고 |
|---|---|---|---|
| 투자 목표(자연어) | Mandate | `objective_text` | **현재 유일한 자연어 입력 필드.** DB `not null`이라 반드시 받아야 한다. 상세 동작은 §2.4 |
| 투자 목적(구조화) | Mandate | `objective` (jsonb) | 자유 구조. 서버가 내용을 검증하지 않는다 |
| 투자 성향(안정/균형/위험선호) | InvestorProfile | `mindset` | 3단계. **Mandate 필드가 아니다** |
| 투자 경험 | InvestorProfile | `experience` | "투자 잘 모른다" = `BEGINNER` |
| 투자 기간 | InvestorProfile | `investment_horizon_years` | |
| 손실 감내도(누적) | InvestorProfile | `max_drawdown_pct` | |
| 현금화 필요 기간 | InvestorProfile | `liquidity_need` | |
| 예산(기준 자본) | Mandate | `risk_bounds.base_capital` | Paper 시작 자본. **한도 집행 분모가 아니다**(집행 분모는 회계의 당일 장 시작 NAV) |
| 통화 | Mandate | `risk_bounds.currency` | Fund `base_currency`와 일치 강제 |
| 최대 단일 종목 비중 | Mandate | `risk_bounds.max_instrument_weight` | |
| 최대 총 익스포저 | Mandate | `risk_bounds.max_gross_exposure` | |
| 일일 최대 손실 | Mandate | `risk_bounds.max_daily_loss` | |
| 주문 승인 방식 | Mandate | `approval_rules.paper_order_mode` | `AUTO` / `USER_APPROVAL` 2단계 |
| 적용 시각 | Mandate | `effective_from` | |

### 2.2 스키마 필수인데 화면 시안에 없는 항목

`policy.py`는 `extra="forbid"`이고 아래가 전부 **required**다. **현재 화면 시안 그대로는 제출이 422로 실패한다.**

| 필드 | 왜 필요한가 |
|---|---|
| `risk_bounds.max_sector_weight` | 포함관계 `instrument ≤ sector ≤ gross`를 서버가 검증한다. 화면이 종목 30%·총 200%만 받으면 그 사이 값을 알 수 없다 |
| `risk_bounds.max_concurrent_positions` | 동시 보유 종목 수 상한 |
| `risk_bounds.max_daily_loss` | 손실한도 — 8개 항목에 있는데 화면 시안에서 누락 |
| `universe_policy.allowed_markets` | 최소 1개 |
| `universe_policy.trading_start` / `trading_end` | 거래 시간대 |

`approval_rules.risk_expansion_requires_user_approval`은 기본값 `true`가 있어 생략 가능하지만, 끄는 것 자체가 Version 변경이므로 화면에 노출할지는 §5-A에서 다룬다.

### 2.3 미결정 — 계약이 없는 항목

| 사용자 입력 | 현재 상태 | 안건 |
|---|---|---|
| 업종 선택("반도체에 투자하고 싶다") | **어디에도 없음.** `UniversePolicy`에 업종 필드 없음 | §5-D |
| 자산군 허용/금지(주식·ETF·레버리지·선물·옵션·파생·암호화폐) | **개념 불일치.** `allowed_assets`는 `instrument_id` 목록(종목 단위)이지 자산군이 아님 | §5-C |
| 투자 성향 단계 세분화 | 3단계 고정 | §5-E |
| 주문 승인 방식 세분화 | 2단계 고정 | §5-F |
| "위험 성향과의 정렬도" 배지 | 서버 판정 로직 없음 | §5-I |
| 빠른 프리셋(Conservative/Growth) | 서버 개념 없음 | §5-H |
| 자유 프롬프트("난 투자 잘 모른다") | `explanation_level`이 유사하나 용도 다름 | §5-K |

### 2.4 자연어 입력의 현재 상태 — 딱 하나 있고, 실제로 쓰인다

**존재하는 자연어 필드는 `objective_text` 하나뿐이다.**

- `MandatePolicy`(`policy.py`)에는 자연어 필드가 **없다.** `objective_text`는 `MandatePolicy` 바깥, `propose_version()`의 별도 인자이자 `mandate_versions`의 별도 컬럼이다.
- `InvestorProfile`에는 자연어 필드가 **하나도 없다.** 전부 enum과 숫자다.
- 나머지 자유 텍스트(`reason`, `blocked_reason` 등)는 사용자 입력이 아니라 승인·거절 사유 기록용이다.

`objective_text`가 실제로 하는 일은 다음과 같다.

| 쓰임 | 실제 동작 |
|---|---|
| 저장 | `mandate_versions.objective_text` (`text not null`) — 생략 불가 |
| **Risk/QA 검토자에게 노출** | `change_workflow.submit()`이 이 값을 Case `reason`과 승인 요청 `reason`("Mandate 변경 RISK 검토 - {objective_text}")에 그대로 넣는다. **검토자가 보는 유일한 맥락 설명이다** |
| 판정 | **어디에도 쓰이지 않는다.** `classify_change()`(TIGHTEN/LOOSEN 판정)는 한도·자산·승인방식만 비교한다 |
| `content_hash` | **포함되지 않는다.** 해시는 `MandatePolicy`만 대상으로 한다 |

마지막 두 줄의 결과로 생기는 실제 동작 하나: **한도는 그대로 두고 목적 문구만 바꾸면 `content_hash`가 같아서 "동일 내용" 중복으로 거절된다.** 사용자가 "설명만 고치고 싶다"를 할 수 없다. 이게 의도인지 §5-K에서 확인이 필요하다.

## 3. 저장 위치 현황

| 객체 | 저장 테이블 | 상태 |
|---|---|---|
| `MandatePolicy` | `governance.mandate_versions` (`risk_bounds`/`universe_policy`/`approval_rules`/`execution_rules` jsonb + `allowed_assets`/`forbidden_assets` jsonb) | ✅ 존재. Version·content_hash·effective 구간 관리 |
| Mandate 승인 이력 | `governance.mandate_decisions`, `governance.approvals`, `governance.cases` | ✅ 존재 |
| `InvestorProfile` | **없음** | 🔴 `suitability.py`는 순수 함수이고 대응 테이블이 없다. 사용자가 성향을 입력해도 **저장할 곳이 없다** |
| `UserPreferences` | `governance.user_preferences` | ⚠️ 테이블만 있고 이를 쓰는 코드 없음 |
| 사용자 신원 | `governance.user_profiles` | ⚠️ `display_name`/`timezone`/`status`뿐. 투자 성향 필드 없음. 로컬 고정 fixture ID만 사용 |

**신규 필드를 넣을 자리**: `mandate_versions`의 jsonb 컬럼 4개는 스키마 변경 없이 확장 가능하다. 특히 `execution_rules`는 현재 `MandatePolicy`가 매핑하지 않는 빈 칸이라 §5-C/§5-F 결과를 담을 후보다. 단 jsonb 내부 계약은 `policy.py`가 정의하므로 Pydantic 모델을 함께 고쳐야 한다.

## 4. 서비스 목적과 현재 계약의 간극

목표는 "종목 정보가 없는 사용자에게, 에이전트가 차트 국면·재무·뉴스를 판단해 추천"이다. 마스터플랜과 일치하고 워크플로도 정의돼 있다([portfolio-recommendation.yaml](../../orchestration/workflows/portfolio-recommendation.yaml): `profile-suitability → research → trading → risk → qa → accounting → ceo`).

**다만 현재 계약에는 결정적 제약이 있다.**

`recommend_portfolios()`는 **사전 등록된 `PortfolioCandidate` 목록을 인자로 받아 필터링만** 한다. 후보 생성을 명시적으로 금지한다("임의의 후보를 생성하거나 위험한 후보를 낮춰서 통과시키지 않는다"). 즉 지금 계약대로면 **누군가 후보 포트폴리오를 미리 등록해 두어야** 추천이 나온다.

"업종을 말하면 관련 종목을 포함해 준다"가 성립하려면 그 후보 카탈로그를 **누가 어떤 근거로 만드는지**가 정해져야 하는데, 이건 어느 문서에도 없다. §5-J의 안건이며 **제품 관점에서 가장 큰 미결정 사항**이다.

또한 현재 워크플로는 `production_enabled: false`이고 결과는 항상 `manual_review_required=true`다.

## 5. 팀 논의 안건

각 안건은 **선택지만 제시하고 결정하지 않는다.** 결정되면 해당 계약 문서와 이 문서를 같은 변경에서 함께 갱신한다.

### A. 화면 미수집 필수 필드 6개 (§2.2) — 어떻게 채울 것인가

| 선택지 | 장점 | 단점 |
|---|---|---|
| A-1. 화면에 전부 노출 | 사용자가 모든 한도를 명시적으로 인지 | 입력 부담이 커져 "잘 모르는 사용자" 대상 제품과 상충 |
| A-2. 성향별 기본값을 서버가 채우고 고급 설정으로 숨김 | 입력 부담 최소 | 성향→한도 매핑표를 먼저 정의해야 함(§5-I와 같은 작업) |
| A-3. `policy.py`에서 일부를 optional + 기본값으로 변경 | 화면·서버 모두 단순 | **한도가 조용히 기본값으로 들어간다.** 개발 원칙 9(위험은 차단 방향)와 충돌 소지 |

→ 결정 필요: 영주(Mandate 소유) + 동규(Risk 집행)

### B. "손실한도"의 정의 — 일일인가 누적인가

현재 두 개념이 서로 다른 객체에 쪼개져 있다.

- `MandatePolicy.risk_bounds.max_daily_loss` — 일일, 집행 대상
- `InvestorProfile.max_drawdown_pct` — 누적 MDD, 추천 필터

사용자가 말하는 "예산의 n%"가 어느 쪽인지, 또는 둘 다 받을지 확정 필요. 둘 다 받는다면 화면에서 구분해 보여줘야 하고, 하나만 받는다면 나머지를 무엇으로 유도할지 정해야 한다.

→ 결정 필요: 동규(Risk) + 도현(회계 NAV 기준)

### C. 자산군 허용/금지 — 신규 필드

`reference.instruments.asset_class`(text)가 근거로 존재하나 **CHECK 제약이 없어 표준값이 정해져 있지 않다.**

| 선택지 | 내용 |
|---|---|
| C-1. `universe_policy`에 `allowed_asset_classes`/`forbidden_asset_classes` 추가 | Universe 제약이라는 의미에 부합 |
| C-2. `execution_rules`에 추가 | 빈 칸 활용, 다만 의미상 실행 규칙은 아님 |
| C-3. 기존 `allowed_assets`를 종목·자산군 혼용으로 확장 | 권장하지 않음 — 판정 코드가 두 단위를 구분해야 해서 복잡해짐 |

선행 작업: `asset_class` 표준값 확정(주식/ETF/레버리지 ETF/선물/옵션/기타 파생/암호화폐 등) 및 CHECK 제약 추가 여부.

→ 결정 필요: 도현(트레이딩 상품 범위) + 재일(Universe 데이터)

### D. 업종 — 가장 선행 작업이 많은 항목

현재 상태를 정확히 적으면 이렇다.

- `UniversePolicy`에 업종 필드 없음.
- `reference.instruments`에 sector 컬럼 **없음**. 업종 근거는 `reference.issuers.industry_code`(nullable text)에만 있고 **표준 taxonomy 미정**.
- `risk_bounds.max_sector_weight`는 `policy.py`에 있지만 **리스크본부에 집행 코드가 없다**(`departments/03-risk`에 sector 관련 구현 없음).

즉 지금은 섹터 한도를 받아 저장은 하지만 **아무도 집행하지 않는 상태**다. 이건 업종 기능과 별개로 그 자체가 문제다.

필요한 결정 순서:

1. 업종 분류 체계 확정(KRX 업종 / GICS / 자체 분류 중 택1)
2. `instrument → 업종` 매핑을 어디에 둘지(`issuers.industry_code` 재사용 vs `instruments`에 컬럼 추가 vs 별도 매핑 테이블)
3. `max_sector_weight` 집행 주체와 시점
4. 그 위에서 "업종 선택 → 관련 종목 포함" UX 정의

→ 결정 필요: 재일(Universe·분류 체계) 주도, 동규(집행) 필수 참여

### E. 투자 성향 단계 세분화

현재 `mindset` 3단계는 `_MINDSET_SCORE`(1~3) → `effective_risk_score` → `PortfolioRiskBand`(LOW/MEDIUM/HIGH)로 **3단계끼리 1:1 대응**한다. 5단계로 늘리면 enum 추가만으로 끝나지 않고 점수 매핑과 밴드 체계를 함께 재설계해야 한다.

| 선택지 | 영향 |
|---|---|
| E-1. 3단계 유지, 화면에서만 슬라이더로 표현 | 코드 변경 없음. 실제 판정은 3단계 |
| E-2. 5단계로 확장 + 밴드도 5단계로 | `suitability.py` 점수·밴드·`PortfolioCandidate.risk_band` 전부 변경. 등록된 후보 재분류 필요 |
| E-3. 성향은 3단계 유지하되 세부 조정은 Mandate 한도로 | 성향=거친 분류, 한도=정밀 조정으로 역할 분리 |

→ 결정 필요: 도현(포트폴리오 적합성 소유) + 영주

### F. 주문 승인 방식 세분화

현재 `AUTO` / `USER_APPROVAL` 2단계. 세분화(예: "일정 금액 초과 주문만 승인") 시 주의점이 있다.

`classify_change()`가 현재 `USER_APPROVAL → AUTO`를 **단순 비교로 LOOSEN(확대) 판정**한다. 중간 단계가 생기면 어떤 전환이 확대인지 판정 로직을 함께 고쳐야 하며, 이건 사용자 재승인 필요 여부를 바꾸는 변경이라 Risk/QA 검토 대상이다.

→ 결정 필요: 도현(OMS) + 동규(Risk 판정) + 영주(Mandate)

### G. `InvestorProfile` 저장소 신설

현재 저장 테이블이 없다(§3). 성향 입력을 받으려면 반드시 선행돼야 한다.

| 선택지 | 내용 |
|---|---|
| G-1. `governance.investor_profiles` 신설 | 사용자 소유 데이터라 governance에 부합. CEO Office 관리 |
| G-2. `accounting.investor_profiles` 신설 | `suitability.py`가 회계/포트폴리오본부 소유라 코드 위치와 일치 |
| G-3. `governance.user_preferences` 확장 | 신규 테이블 없이 처리. 다만 "표현 설정"과 "투자 성향"이 섞임 |

Version 관리 필요 여부(성향이 바뀌면 이력을 남길지)도 함께 결정한다. `InvestorProfile`에는 이미 `profile_version` 필드가 있다.

→ 결정 필요: 도현(코드 소유) + 영주(DB 스키마 소유)

### H. 빠른 프리셋

서버가 프리셋을 제공할지, 화면 상수로 둘지. 서버 제공 시 프리셋도 버전 관리 대상인지(프리셋이 바뀌면 기존 사용자에게 영향이 있는지) 확정 필요.

→ 결정 필요: 영주 + 동규(프리셋 값의 Risk 타당성)

### I. "위험 성향과의 정렬도" 판정

화면 시안은 "보수적" 상태에서 총 익스포저 200% + 레버리지 허용인데도 **"✓적정"으로 표시**한다. 현재 서버에 이 판정 로직이 없으므로 화면이 임의로 판단하고 있는 셈이다.

성향↔한도 매핑표를 정의하고 **결정론적 코드로** 판정해야 한다(LLM 판정 금지 — 개발 원칙). §5-A의 A-2를 택하면 같은 매핑표를 재사용할 수 있다.

정렬도 위반 시 동작도 정해야 한다: 경고만 할지, 제출을 막을지, Risk 검토 필수로 승격할지.

→ 결정 필요: 동규(Risk) 주도

### J. 포트폴리오 후보 카탈로그를 누가 만드는가 — 제품 최대 이슈

§4의 간극이다. 선택지를 적으면:

| 선택지 | 내용 | 고려사항 |
|---|---|---|
| J-1. 사람이 사전 등록 | 현재 계약 그대로. 가장 안전 | "업종 말하면 관련 종목" 같은 동적 요구를 충족 못 함 |
| J-2. 리서치·퀀트본부가 주기적으로 후보 생성 | 에이전트가 실제로 일하는 모습에 부합 | 후보 생성 자체가 QA·Risk 검증 대상이 되어야 함. `evidence_refs` 필수 계약 유지 필요 |
| J-3. 사용자 입력(업종 등)에 따라 실시간 생성 | 요구에 가장 가까움 | PIT·근거·재현성 계약을 실시간으로 만족시켜야 해 난이도 최고. `NO_MATCH` fallback 금지 원칙과 충돌 여지 |

→ 결정 필요: 전원. 재일(생성 주체) + 동규(검증) + 도현(적합성) + 영주(제품 범위)

### K. 자연어 입력을 유지할 것인가, 전부 선택형으로 갈 것인가

§2.4가 현재 상태다. 자연어 필드는 `objective_text` 하나이고, DB `not null`이라 **지금도 사용자는 반드시 뭔가를 써야 한다.** 이걸 유지할지가 안건이다.

**먼저 확인해야 할 제약**: `suitability.py`는 **LLM이 성향을 추론하는 것을 명시적으로 금지**한다. 따라서 어떤 선택지를 택하든 **자유 문장을 성향·한도 판정의 입력으로 쓸 수는 없다.** 자연어의 용도는 (a) 사람이 읽는 맥락 기록, (b) 설명 톤 조절 둘 중 하나로 제한된다.

| 선택지 | 내용 | 잃는 것 / 얻는 것 |
|---|---|---|
| K-1. 전부 선택형, 자연어 제거 | `objective_text`를 선택지 조합에서 자동 생성(예: "성장 중심 · 장기 · 국내주식") | 입력 부담 최소. **다만 Risk/QA 검토자가 보는 맥락이 사라진다**(§2.4) — 검토자는 "왜 이 사용자가 한도를 늘리려는지"를 알 수 없게 됨. DB `not null` 대응은 자동 생성으로 해결 |
| K-2. 선택형 + 자연어 보조(선택 입력) | 선택지로 정책을 만들고, 자연어는 비워둘 수 있는 메모로 | 균형안. `not null`이라 빈 문자열 허용 여부를 정해야 함(현재 API는 `min_length=1`) |
| K-3. 자연어 우선, 선택지는 보조 | 사용자가 문장으로 쓰면 화면이 선택지를 제안 | **문장→정책 변환을 누가 하는지가 문제.** LLM이 하면 판정 금지 원칙과 경계가 모호해짐. 하려면 "LLM이 제안 → 사용자가 선택지로 확정"만 허용해야 함 |

**"다 선택하라고 하는 게 낫나"에 대해 확인할 것 하나**: 제품 목적이 "종목을 잘 모르는 사용자"인데, 선택형만 남기면 사용자는 **자기가 뭘 모르는지 표현할 방법이 없어진다.** 반대로 자연어만 남기면 그 문장을 판정에 못 쓰므로 정책이 비게 된다. 그래서 실질적 선택은 K-1과 K-2 사이이며, 핵심 질문은 **"Risk/QA 검토자에게 맥락을 남길 필요가 있는가"**다(§2.4).

**"난 투자에 대해서 잘 모른다"는 자연어가 아니어도 이미 처리된다.**

| 방법 | 내용 |
|---|---|
| `InvestorProfile.experience = BEGINNER` | `effective_risk_score = min(mindset, experience)`로 초보자에게 공격형 추천이 자동 차단된다. **이미 결정론적으로 동작하는 경로다** |
| `user_preferences.explanation_level = DETAILED` | 이미 존재하는 컬럼(BRIEF/STANDARD/DETAILED). 설명 자세함 조절용 |

즉 이 문장은 자연어로 받을 이유가 가장 적은 항목이다 — 위 두 필드가 더 정확하고 판정에 쓸 수 있다.

**추가 결정 사항**: §2.4 마지막 문단의 "목적 문구만 수정 불가" 동작을 유지할지. 유지하지 않으려면 `content_hash` 계산에 `objective_text`를 포함하도록 `compute_content_hash()`를 바꿔야 하는데, 이는 기존 저장된 Version의 해시와 불일치를 만들므로 마이그레이션 영향을 함께 검토해야 한다.

→ 결정 필요: 영주(Mandate) + 동규(검토 맥락 필요 여부) + 도현(적합성 입력)

## 5-A. 구현 반영 정정 (2026-08-05)

이 분석 문서의 §2·§5·부록 A에는 결정 전 조사 시점의 표현이 남아 있다. 아래 구현 상태가 현재 코드의 사실 기준이며, 설계 문장과 운영 완료를 혼동하지 않는다.

| 항목 | 현재 상태 | 결정론적 연결 |
|---|---|---|
| 9개 경험×성향 프리셋 | 구현·테스트 완료 | `departments/03-risk/mandate_presets.py`가 Risk 소유 상수와 정렬 검증 제공 |
| `max_drawdown_pct` | 구현·테스트 완료 | `RiskBounds`의 비율 필드이며 `max_daily_loss <= max_drawdown_pct`를 검증 |
| 자산군 허용/금지 | 구현·테스트 완료 | `UniversePolicy` → `MandateScope` → Risk pre-trade gate. 금지 또는 메타데이터 누락은 fail-closed |
| 선호/제외 업종 | 구현·테스트 완료 | 선호는 후보 맥락, 제외 업종은 신규 노출 차단. `max_sector_weight`는 현재 섹터 노출과 함께 검사 |
| LLM 제안 경계 | 구현·테스트 완료 | LLM은 제안만 하고 `MandateConfirmationGate`가 정책 스키마·Risk·QA·사용자 확인을 모두 통과시켜야 활성화 |

위 연결은 코드·계약 테스트 기준으로 `TEST_VERIFIED`다. Supabase에 PIT 국내주식 instrument/sector와 시장 스냅샷이 없으면 추천 후보를 만들지 않으며, 그 실행은 `DEGRADED/HOLD`다. 따라서 위 구현 완료를 실거래 운영 준비 완료로 해석하지 않는다.

## 6. 결정 후 작업 순서 (의존 관계)

```text
G(InvestorProfile 저장소)  ──┐
                             ├─→ 성향 입력 화면 구현
E(성향 단계)              ──┘

A(필수 필드) ─→ I(정렬도 매핑표) ─→ H(프리셋)
                                  └─→ Mandate 입력 화면 구현

B(손실한도 정의) ─→ A

C(자산군) ──→ Mandate 스키마 확장 ─→ 화면 자산 정책 섹션
D(업종) ──→ taxonomy 확정 ─→ instrument 매핑 ─→ Risk 집행 ─→ 업종 선택 UX
F(승인 세분화) ─→ classify_change 수정 ─→ Risk/QA 검토

J(후보 카탈로그) ─→ 추천 결과 화면 (별도 트랙)
```

**병렬 착수 가능**: A·B·G·K는 서로 독립적이라 먼저 결정해도 된다.
**가장 긴 경로**: D(업종)는 taxonomy → 매핑 → 집행까지 3단계 선행 작업이 있다.

## 7. 이 문서와 하위 문서의 관계

| 문서 | 담당 범위 |
|---|---|
| 이 문서 | 무엇을 물어보는가, 어디에 귀속되는가, 무엇이 미결정인가 |
| (화면 설계 문서 없음) | §5 결정 후 신규 작성 |
| [USER_INPUT_API_SPEC.md](../02-engineering/USER_INPUT_API_SPEC.md) | Mandate Transport — 실제 Route, 요청/응답 스키마, 에러 코드, 멱등성 한계 |
| [PORTFOLIO_SUITABILITY_SPEC.md](PORTFOLIO_SUITABILITY_SPEC.md) | `InvestorProfile` 계약과 적합성 판정 규칙 |

§5 안건이 확정되면 해당 하위 문서와 `policy.py`/`suitability.py`를 같은 변경에서 함께 갱신한다. 마스터플랜을 바꿔야 하는 결정(예: J-3)은 ADR을 먼저 승인한다.

---

# 부록 A. Mandate 속성 전수 표 (팀 논의용)

지금까지 언급된 모든 Mandate 속성이다. "무엇을 사용자에게 물어볼 것인가"를 정하기 위한 표이며, 논의 후 결정사항을 기록한다.

## A.0 먼저 알아야 할 전제 — Mandate 한도는 현재 Risk Engine으로 흘러가지 않는다

표를 읽기 전에 이 사실을 확인해야 한다. `departments/03-risk/api/risk_context_repository.py`의 `_load_mandate()`/`_load_limits()`는 **`risk.policies`와 `risk.limits`에서 값을 읽는다.** `governance.mandate_versions`는 `EXISTS` 조건으로 **"활성 Mandate가 있는가"만 확인하고 값은 한 컬럼도 읽지 않는다.**

그 결과 두 계층의 어휘가 다르다.

| Risk Engine (`LimitSet`/`MandateScope`) | Mandate (`MandatePolicy`) | 관계 |
|---|---|---|
| `soft_single_issuer_pct` / `hard_single_issuer_pct` | `max_instrument_weight` | 유사하나 **발행사 단위 + soft/hard 2단계**로 다름 |
| `max_daily_loss` (금액 `Decimal`) | `max_daily_loss` (비율 0~1) | 이름은 같고 **단위가 다름** |
| `max_drawdown_pct` | 없음 (InvestorProfile에 있음) | 계층이 어긋남 |
| `allowed_instrument_ids` | `allowed_assets` | 대응됨 |
| `min_order_notional` / `max_order_notional` | 없음 | Risk에만 존재 |
| `max_daily_turnover_notional` / `max_daily_order_count` | 없음 | Risk에만 존재 |
| 없음 | `max_sector_weight`, `max_gross_exposure`, `max_concurrent_positions`, `allowed_markets`, `trading_start/end` | Mandate에만 존재, **집행 주체 없음** |

**단, 이것은 "연결할지 말지"의 문제가 아니다.** [UNIFIED_DOMAIN_API_SPEC.md](../02-engineering/UNIFIED_DOMAIN_API_SPEC.md) Governance/Risk 절이 이미 "Risk Engine이 governance에서 비율을 조회한다"는 설계를 확정해 뒀다. 실제 코드가 그 설계를 아직 구현하지 않았을 뿐이다 — 상세와 남은 작업은 A.9 §L을 본다.

지금 단계에서 중요한 점은 하나다. **어떤 필드를 물어보든 현재는 집행되지 않으므로, 화면과 문서가 "이 한도로 차단됩니다"라고 서술하면 안 된다.**

## A.1 `risk_bounds` (jsonb) — `RiskBounds`, 전부 필수

| # | 속성 | 타입·제약 | 필수 | 저장 시 검증 | 변경방향 판정 | 실제 집행 | 논의 포인트 |
|---|---|---|---|---|---|---|---|
| 1 | `base_capital` | Decimal > 0 | ✔ | 값 범위 | ✖ | ✖ | 한도 분모가 아님(집행 분모는 회계 NAV). 사용자에게 "예산"으로 물을지, 회계 초기 현금에서 끌어올지 |
| 2 | `currency` | `^[A-Z]{3}$` | ✔ | Fund `base_currency` 일치 | ✖ | ✖ | Fund가 이미 통화를 가지므로 **묻지 않고 채울 수 있음** |
| 3 | `max_instrument_weight` | 0 < w ≤ 1 | ✔ | ≤ `max_sector_weight` | ✔ | ✖ (Risk는 `soft/hard_single_issuer_pct` 사용) | 종목 단위 vs 발행사 단위 정합. soft/hard 2단계를 사용자에게 노출할지 |
| 4 | `max_sector_weight` | 0 < w ≤ 1 | ✔ | ≤ `max_gross_exposure` | ✔ | ✖ **집행 코드 없음** | 섹터 분류 체계 자체가 미정(§5-D). 집행 못 하는 값을 물어볼지 |
| 5 | `max_gross_exposure` | > 0 (1.0=100%) | ✔ | 포함관계 상한 | ✔ | ✖ | 100% 초과 = 레버리지. 초보 사용자에게 물을 개념인지 |
| 6 | `max_concurrent_positions` | int > 0 | ✔ | 값 범위 | ✔ | ✖ | 성향에서 자동 도출 가능한 값인지 |
| 7 | `max_daily_loss` | 0 < w ≤ 1 | ✔ | 값 범위 | ✔ | ✖ (Risk는 동명이지만 **금액** 단위) | 비율↔금액 단위 통일 필요. InvestorProfile의 `max_drawdown_pct`(누적)와 역할 구분(§5-B) |

## A.2 `universe_policy` (jsonb) — `UniversePolicy`, 전부 필수

| # | 속성 | 타입·제약 | 필수 | 저장 시 검증 | 변경방향 판정 | 실제 집행 | 논의 포인트 |
|---|---|---|---|---|---|---|---|
| 8 | `allowed_markets` | list[str], 최소 1개, 중복 불가 | ✔ | 개수·중복 | ✖ | ✖ | 현재 KRX 단일이면 **묻지 않고 고정 가능** |
| 9 | `trading_start` | `HH:MM` | ✔ | < `trading_end` | ✖ | ✖ | 시장 정규장 시간이 이미 정해져 있음. 사용자가 좁힐 이유가 있는지 |
| 10 | `trading_end` | `HH:MM` | ✔ | > `trading_start` | ✖ | ✖ | 위와 동일 |

## A.3 `approval_rules` (jsonb) — `ApprovalRules`

| # | 속성 | 타입·제약 | 필수 | 저장 시 검증 | 변경방향 판정 | 실제 집행 | 논의 포인트 |
|---|---|---|---|---|---|---|---|
| 11 | `paper_order_mode` | `AUTO` \| `USER_APPROVAL` | ✔ | enum | ✔ (`AUTO` 전환 = LOOSEN) | ✖ (OMS 미연결) | **사용자가 반드시 알아야 할 핵심 선택.** 세분화 시 판정 로직 동반 수정(§5-F) |
| 12 | `risk_expansion_requires_user_approval` | bool, 기본 `true` | 기본값 有 | — | ✖ | ✖ | 기본 `true`를 그대로 두면 **묻지 않아도 됨** |

## A.4 자산 허용/제외 — `MandatePolicy` 최상위

| # | 속성 | 타입·제약 | 필수 | 저장 시 검증 | 변경방향 판정 | 실제 집행 | 논의 포인트 |
|---|---|---|---|---|---|---|---|
| 13 | **`allowed_assets` (허용 종목)** | list[str] (`instrument_id`), 기본 `[]` | 기본값 有 | `forbidden`과 교집합 금지 / 전부 금지목록이면 거부 | ✔ (추가 = LOOSEN) | 대응 있음 (`MandateScope.allowed_instrument_ids`) — 단 `risk.policies`에서 읽음 | **빈 배열 = 전체 허용**. 초보 사용자가 종목을 지정할 수 있는지 |
| 14 | **`forbidden_assets` (제외 종목)** | list[str] (`instrument_id`), 기본 `[]` | 기본값 有 | 위와 동일 | ✔ (제거 = LOOSEN) | ✖ (Risk의 `RestrictedItem`은 거래정지·관리종목용 별개 개념) | "이 종목은 싫다"는 실제 사용자 요구. 사용자 제외와 시스템 제한을 구분해 저장할지 |
| 15 | **허용 상품유형 (asset_class)** | **없음** | — | — | — | — | `reference.instruments.asset_class` 존재하나 **CHECK 없어 표준값 미정**. 신규 필드 위치(§5-C) |
| 16 | **제외 상품유형 (asset_class)** | **없음** | — | — | — | — | 화면 시안의 선물·옵션·파생·암호화폐 금지가 여기 해당. 계약 없음 |
| 17 | **허용 업종** | **없음** | — | — | — | — | `issuers.industry_code`(nullable)만 존재, taxonomy 미정(§5-D) |
| 18 | **제외 업종** | **없음** | — | — | — | — | 위와 동일 |

> 13~14는 **종목(instrument) 단위**, 15~18은 **분류(class/sector) 단위**다. 지금 계약에는 종목 단위만 있다. 네 종류를 한 필드에 섞으면 판정 코드가 단위를 구분해야 해서 복잡해지므로 별도 필드를 권한다.

## A.5 `mandate_versions` 컬럼 — `MandatePolicy` 바깥

| # | 속성 | 타입·제약 | 필수 | 성격 | 논의 포인트 |
|---|---|---|---|---|---|
| 19 | `objective_text` | `text not null` | ✔ | 사용자 입력 (자연어) | **현재 유일한 자연어 필드.** Risk/QA 검토자가 보는 유일한 맥락(§2.4, §5-K) |
| 20 | `objective` | `jsonb not null` | ✔ | 사용자 입력 (구조화) | 서버가 내용 검증 안 함. 스타일 태그 등 |
| 21 | `effective_from` | timestamptz | ✔ | 사용자 입력 | 기본 "즉시"면 **묻지 않아도 됨** |
| 22 | `execution_rules` | `jsonb`, 기본 `{}` | 기본값 有 | **빈 칸** | `MandatePolicy`가 매핑하지 않음. 15~18 신규 필드를 담을 후보 자리 |
| 23 | `effective_to` | timestamptz null | — | 서버 관리 | 활성화 시 이전 Version을 닫는 값 |
| 24 | `content_hash` | text | — | 서버 생성 | `MandatePolicy`만 해시. `objective_text` 미포함(§2.4) |
| 25 | `version` | int > 0 | — | 서버 생성 | |
| 26 | `created_by` | uuid null | — | 서버/세션 | `governance.user_profiles` FK |

## A.6 화면·논의에서 나왔으나 Mandate 속성이 아닌 것

혼동을 막기 위해 명시한다. 이 값들은 Mandate에 넣지 않는다.

| 속성 | 실제 귀속 | 근거 |
|---|---|---|
| 투자 성향(안정~위험선호) | `InvestorProfile.mindset` | 추천 필터이지 집행 한도가 아님 |
| 투자 경험 / 투자 기간 / 유동성 요구 | `InvestorProfile` | 위와 동일 |
| 누적 최대 손실폭 | `InvestorProfile.max_drawdown_pct` | Risk Engine에도 동명 필드가 있어 §5-B에서 정리 필요 |
| 설명 자세함 | `governance.user_preferences.explanation_level` | 표현 계층 |
| 빠른 프리셋 | 미정 | 값의 묶음이지 필드가 아님(§5-H) |
| 위험 성향 정렬도 배지 | 미정 | 파생 판정값(§5-I) |

## A.7 역방향 — Risk Engine에는 있는데 Mandate에 없는 것

사용자에게 물어볼지 여부를 함께 논의해야 한다.

| 속성 | Risk Engine 위치 | 논의 포인트 |
|---|---|---|
| `min_order_notional` / `max_order_notional` | `MandateScope` | 주문 1건의 최소·최대 금액. 사용자 관심사일 수 있음 |
| `max_daily_turnover_notional` | `LimitSet` | 일일 회전율 한도 |
| `max_daily_order_count` | `LimitSet` | 일일 주문 건수 한도 |
| `max_drawdown_pct` | `LimitSet` | InvestorProfile과 중복. 어느 쪽이 권위인지 정리 필요 |
| soft/hard 2단계 구분 | `LimitSet` | soft=RESIZE, hard=REJECT. Mandate는 단일값이라 표현력이 부족 |

## A.8 요약 통계

| 구분 | 개수 |
|---|---|
| `MandatePolicy` 필드 (사용자 입력 대상) | 14 (필수 12 + 기본값 2) |
| `mandate_versions` 추가 사용자 입력 | 3 (`objective_text`, `objective`, `effective_from`) |
| 서버 생성·관리 | 4 |
| 계약이 없는 논의 항목 (상품유형·업종) | 4 |
| Risk Engine에만 있는 한도 | 5 |
| **현재 Risk Engine이 Mandate 값으로 집행하는 것** | **0** |

마지막 줄이 이 표의 핵심이다. 지금은 사용자가 무엇을 입력하든 집행에 도달하지 않는다.


### L. Mandate → Risk 집행 경로 — 결정이 아니라 **구현 공백**이다

> **정정(2026-08-05)**: 이 안건을 처음에 "연결할 것인가"라는 열린 질문으로 적었으나 부정확했다. **연결은 이미 설계로 확정돼 있다.**

[UNIFIED_DOMAIN_API_SPEC.md](../02-engineering/UNIFIED_DOMAIN_API_SPEC.md) Governance/Risk 절이 호출 경로를 명시한다.

```
risk-management / trading-department → GET /governance/v1/mandates/{fund_id}/current → governance
```

> "Risk Engine이 한도를 판정하려면 Mandate 비율이 필요하다. (…) Risk Engine은 두 곳을 각각 조회한다.
> 비율 ← governance / 기준 자본 ← 회계 API의 당일 장 시작 `nav_runs.total_nav`"

즉 의도된 구조는 **Mandate가 비율을 주고, 회계가 분모를 주고, Risk Engine이 판정**하는 것이다. 그런데 실제 코드는 그렇게 동작하지 않는다. 막고 있는 것은 두 가지 구현 공백이다.

| # | 공백 | 현재 상태 | 필요한 작업 |
|---|---|---|---|
| 1 | `GET .../mandates/{id}/current`가 **정책을 반환하지 않는다** | `{mandate_id, current_version, status}`만 응답. `risk_bounds`가 없어 Risk가 조회해도 비율을 못 받는다. Path도 `fund_id`가 아니라 `mandate_id` | 응답에 `policy` 포함, `fund_id` 조회 지원 |
| 2 | Risk Engine이 `risk.policies`·`risk.limits`를 읽는다 | `_load_mandate()`는 `mandate_versions`를 `EXISTS`로만 확인 | governance 조회로 전환하거나, Mandate→`risk.policies` 동기화 계층 신설 |

추가로 해소해야 할 **어휘·단위 불일치**(A.0 표):

- `max_daily_loss` — Mandate는 비율, Risk `LimitSet`은 금액. 분모를 회계 NAV로 두는 §2.1 계약대로면 Risk가 비율×NAV로 환산해야 한다.
- `max_instrument_weight`(종목) ↔ `soft/hard_single_issuer_pct`(발행사, 2단계) — 단위와 단계 수가 다르다.
- `max_sector_weight`, `max_gross_exposure`, `max_concurrent_positions` — Risk에 대응 필드가 아예 없다. 신규 구현 대상이다.

**그래서 이 항목의 성격**: 팀이 "할지 말지"를 정하는 안건이 아니라, **누가 언제 구현하는가**의 문제다. 다만 아래 하나는 실제로 결정이 필요하다.

| 결정 필요 | 선택지 |
|---|---|
| 구현 방식 | (a) Risk가 governance API를 직접 조회 (§5.1 문언 그대로) / (b) Mandate 활성화 시 `risk.policies`로 동기화하는 변환 계층 |
| 대응 필드가 없는 3개 한도 | Risk Engine에 신규 구현할지, Mandate에서 제거할지 |

**이 공백이 해소되기 전까지는 화면·문서 어디에서도 "이 한도로 주문이 차단된다"고 서술하지 않는다.** 없는 보호를 있다고 알리는 것이기 때문이다.

→ 주도: 동규(Risk Engine) · 참여: 영주(governance API 응답 확장) · 도현(회계 분모 제공)
