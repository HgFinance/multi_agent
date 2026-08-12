# 사용자 입력 백엔드 API 명세서 (온보딩)

> 문서 상태: **확정 계약·부분 구현** — 2026-08-10 기준. Mandate 조회·변경·승인과 Stateless mandate-assistant 일부 Endpoint가 구현됐으며, 남은 항목은 아래 미해결 절에 기록한다.
> 작성: 영주님 (CEO Office) · 작성일: 2026-08-05
> 제품 계약: [USER_INPUT_SPEC.md](../01-product/USER_INPUT_SPEC.md) — 무엇을 왜 받는지는 그 문서가 기준
> 현황 기록: 이 문서의 구현 상태 절과 `departments/00-ceo-office/api/app.py`가 현재 governance-api 계약이다.
> 상위 계약: [UNIFIED_DOMAIN_API_SPEC.md](UNIFIED_DOMAIN_API_SPEC.md)

이 문서는 **신설·변경할 계약**만 다룬다. 기존 그대로인 Route(예: `POST .../change-requests`, `advance`, `approvals/decide`)는 현황 문서를 따르며 여기서 반복하지 않는다.

## 1. 스키마 변경

### 1.1 `policy.py` — `UniversePolicy` 확장

```python
class UniversePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # --- 기존 ---
    allowed_markets: list[str] = Field(min_length=1)
    trading_start: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    trading_end: str = Field(pattern=r"^([01]\d|2[0-3]):[0-5]\d$")

    # --- 신설 (C-1) 자산군 정책. 빈 목록 = 제약 없음 ---
    allowed_asset_classes: list[str] = Field(default_factory=list)
    forbidden_asset_classes: list[str] = Field(default_factory=list)

    # --- 신설 (D) 업종 선호. KRX 업종 코드. 빈 목록 = 제약 없음 ---
    preferred_sectors: list[str] = Field(default_factory=list)
    excluded_sectors: list[str] = Field(default_factory=list)
```

**추가 validator (결정론)**

| 규칙 | 위반 시 |
|---|---|
| `allowed_asset_classes ∩ forbidden_asset_classes = ∅` | 400 `MANDATE_CONTRADICTORY_BOUNDS` |
| `preferred_sectors ∩ excluded_sectors = ∅` | 동일 |
| 각 목록 내 중복 금지 | 동일 |
| `allowed_asset_classes`가 비어있지 않은데 전부 `forbidden`에 포함 | 동일 (거래 가능 자산 0) |

> `asset_class` 표준 코드값은 **미확정**이다(도현·재일). 확정 전까지는 값 자체를 CHECK하지 않고 위 관계 규칙만 검증한다. 임의 표준값을 코드에 박지 않는다.

### 1.2 `policy.py` — `RiskBounds` 확장

```python
class RiskBounds(BaseModel):
    # --- 기존 7개 필드 유지 ---
    ...
    # --- 신설 (B) 전체(누적) 최대 손실 ---
    max_drawdown_pct: Decimal = Field(gt=0, le=1, description="누적 최대 손실폭 (기준 자본 대비)")
```

**신규 상호 모순 규칙**

```
max_daily_loss ≤ max_drawdown_pct
```

일일 손실 한도가 전체 한도보다 클 수 없다. 위반 시 400.

기존 포함관계(`instrument ≤ sector ≤ gross`)는 그대로 유지한다.

### 1.3 `service.py` — `classify_change()` 확장

신규 필드의 변경 방향 판정을 추가한다. **누락하면 확대(LOOSEN)가 완화로 오판되어 사용자 재승인을 건너뛴다.**

| 필드 | 확대(LOOSEN) | 완화(TIGHTEN) |
|---|---|---|
| `max_drawdown_pct` | 값 증가 | 값 감소 |
| `allowed_asset_classes` | 항목 추가 | 항목 제거 |
| `forbidden_asset_classes` | 항목 제거 | 항목 추가 |
| `excluded_sectors` | 항목 제거 | 항목 추가 |
| `preferred_sectors` | — (선호일 뿐 제약이 아니므로 NEUTRAL) | — |

> `preferred_sectors`를 방향 판정에서 제외하는 이유: 이것은 **허용 범위를 넓히는 값이 아니라 우선순위 힌트**다. 제약을 푸는 것이 아니므로 위험 확대가 아니다.

### 1.4 DB — `governance.mandate_versions`

**마이그레이션 없음.** `universe_policy`·`risk_bounds`가 jsonb라 컬럼 변경이 필요 없다. 내부 계약만 §1.1~1.2로 확장한다.

### 1.5 DB — `accounting.investor_profiles` 신설 (**G-2**)

`InvestorProfile`을 회계/포트폴리오본부가 소유한다 — Mandate 기준을 리스크본부가 갖는 것과 같은 원칙이다.

```sql
create table accounting.investor_profiles (
  investor_profile_id uuid primary key default gen_random_uuid(),
  user_id  uuid not null references governance.user_profiles(user_id),
  fund_id  uuid not null references accounting.funds(fund_id),
  version  integer not null check (version > 0),

  mindset    text not null check (mindset in ('SAFETY_FIRST','BALANCED','RISK_SEEKING')),
  experience text not null check (experience in ('BEGINNER','INTERMEDIATE','EXPERIENCED')),
  investment_horizon_years integer not null check (investment_horizon_years between 1 and 100),
  max_drawdown_pct numeric(9,8) not null check (max_drawdown_pct > 0 and max_drawdown_pct <= 1),
  liquidity_need text not null check (liquidity_need in ('HIGH','MEDIUM','LOW')),

  as_of      timestamptz not null,
  created_by uuid references governance.user_profiles(user_id),
  created_at timestamptz not null default now(),

  unique (user_id, fund_id, version)
);
```

`suitability.py`의 `InvestorProfile` Pydantic 모델과 1:1이다. `profile_version` → `version` 컬럼에 대응한다.

> **Version을 두는 이유**: 성향은 바뀔 수 있고, "그때 어떤 성향으로 추천했는가"가 감사 대상이다. 다만 Mandate와 달리 **승인 절차는 없다** — advisory 입력이라 Risk/QA 검토를 타지 않는다.

## 2. Route 변경

### 2.1 `GET /governance/v1/mandates/{mandate_id}/current` — 응답 확장 (**✅ 구현 완료, 2026-08-06**)

> **정정(2026-08-06)**: 이전 버전은 "화면이 기존 값을 채울 수 없다"고 적었으나, 실제로는 `ai-office/app/ops/PortfolioInterviewPanel.tsx`가 **localStorage**(`readSavedMandatePolicy()`)에 직전 제출값을 저장해두고 그걸로 우회하고 있었다. 화면 자체는 동작했다 — 다만 이 우회는 같은 브라우저·같은 세션에서만 유효하고, 다른 기기·새 브라우저·다른 사용자가 같은 Mandate를 열면 초기값을 못 채웠다. 이 절의 목적은 "막힌 것을 뚫는다"가 아니라 **"클라이언트 로컬 상태에 의존하는 우회를 서버 조회로 대체한다"**였다.
>
> **구현 완료**: `departments/00-ceo-office/api/app.py`의 `get_mandate_current()`가 `policy`/`objective_text`/`objective`/`content_hash`/`effective_from`/`effective_to`를 포함하도록 확장됐다. Version이 아직 없으면(`current_version=0`) 기존과 동일하게 최소 필드만 반환한다. `MandateVersionRepository`에 `get(mandate_id, version)`을 신설해 `InMemoryMandateVersionRepository`·`PostgresMandateVersionRepository` 양쪽에 구현했다(Postgres는 jsonb 컬럼이 psycopg2로 dict/list 자동 변환됨을 실 DB로 확인).
>
> **`by-fund` 조회도 별도 Route로 구현했다**: `GET /governance/v1/mandates/by-fund/{fund_id}/current`. `governance.mandates`가 `unique(fund_id, name)`이라 한 Fund에 이름이 다른 Mandate가 여러 개 있을 수 있어, `MandateVersionRepository.mandate_ids_for_fund(fund_id)`가 목록을 그대로 돌려주고 app.py가 0개=404, 1개=단일 조회, 2개 이상=409(모호, 임의 선택 안 함)로 판단한다. Route Registry에 등재 완료, `app.openapi()`와 정확히 일치 확인.
>
> 남은 것은 프론트가 이 Route를 실제로 호출해 localStorage 우회를 걷어내는 작업뿐이다(도현, §5).

`{mandate_id, current_version, status}` + `policy` 전체를 반환한다.

```json
{
  "mandate_id": "uuid",
  "fund_id": "uuid",
  "current_version": 3,
  "status": "ACTIVE",
  "effective_from": "2026-08-05T00:00:00Z",
  "content_hash": "sha256...",
  "objective_text": "장기 성장",
  "objective": {"style": "balanced"},
  "policy": {
    "allowed_assets": [], "forbidden_assets": [],
    "risk_bounds": {
      "base_capital": "100000000", "currency": "KRW",
      "max_instrument_weight": "0.1", "max_sector_weight": "0.3",
      "max_gross_exposure": "1.0", "max_concurrent_positions": 10,
      "max_daily_loss": "0.03", "max_drawdown_pct": "0.20"
    },
    "universe_policy": {
      "allowed_markets": ["KRX"], "trading_start": "09:00", "trading_end": "15:30",
      "allowed_asset_classes": [], "forbidden_asset_classes": ["FUTURES", "OPTIONS"],
      "preferred_sectors": ["G2510"], "excluded_sectors": []
    },
    "approval_rules": {
      "paper_order_mode": "USER_APPROVAL",
      "risk_expansion_requires_user_approval": true
    }
  }
}
```

`fund_id` 기준 조회도 함께 지원한다(`GET /governance/v1/mandates/by-fund/{fund_id}/current` 또는 쿼리 파라미터).

> **정정(2026-08-06)**: 이 조회를 신설하는 이유는 "화면이 `mandate_id`를 몰라서"가 아니다. 실제로는 `PortfolioInterviewPanel.tsx`의 "고급 설정"에서 **사용자가 Mandate ID를 손으로 입력**하고 있다(필수 텍스트 필드). `fund_id` 조회가 생기면 이 수동 입력 필드를 없애고 Fund 선택만으로 화면이 알아서 현재 Mandate를 찾게 할 수 있다.

### 2.2 온보딩 제출 — 기존 Route 재사용

**신규 Route를 만들지 않는다.** `POST /governance/v1/mandates/{mandate_id}/change-requests`를 그대로 쓴다. 프리셋은 화면이 적용하므로(**H**) 서버는 완전한 `MandatePolicy`를 받는다.

화면이 전송 전 채워야 하는 것:

| 출처 | 필드 |
|---|---|
| 사용자 직접 선택 | `base_capital`, `currency`, `asset_classes`, `max_drawdown_pct`, `max_daily_loss`, `paper_order_mode` |
| 프리셋(화면 상수) | `max_instrument_weight`, `max_sector_weight`, `max_gross_exposure`, `max_concurrent_positions` |
| 고정값 | `allowed_markets`, `trading_start/end`, `risk_expansion_requires_user_approval`, `effective_from` |
| 챗봇 확정분 | `preferred_sectors`, `excluded_sectors`, `allowed_assets`, `forbidden_assets`, `objective_text` |

### 2.3 InvestorProfile — 신규 Route (회계/포트폴리오본부)

| Method/Path | 용도 |
|---|---|
| `POST /portfolio/v1/investor-profiles` | 프로필 생성 (항상 새 `version`) |
| `GET /portfolio/v1/investor-profiles/current?user_id=&fund_id=` | 현재 버전 조회 |

**Request**

```json
{
  "user_id": "uuid", "fund_id": "uuid",
  "mindset": "BALANCED", "experience": "BEGINNER",
  "investment_horizon_years": 10,
  "max_drawdown_pct": "0.20",
  "liquidity_need": "LOW",
  "as_of": "2026-08-05T00:00:00Z"
}
```

**Response** — 저장 결과 + 파생 값

```json
{
  "investor_profile_id": "uuid", "version": 1,
  "effective_risk_band": "LOW",
  "effective_risk_reason": "경험(BEGINNER)이 성향(BALANCED)보다 낮아 상한이 됩니다"
}
```

`effective_risk_band`는 `suitability.py`의 `min(mindset_score, experience_score)`를 그대로 노출한다. **화면이 재계산하지 않는다.**

### 2.4 챗봇 제안 — 신규 Route (**Stateless**) (**✅ 부분 구현, 2026-08-06**)

대화 중간 상태를 서버에 저장하지 않는다. 화면이 대화 이력을 들고 있다가 매번 함께 보낸다.

> **왜 draft 테이블을 만들지 않나**: 미완성 정책을 Mandate Version으로 만들면 `content_hash` 중복·승인 흐름 발동 문제가 생긴다. 별도 draft 테이블은 온보딩 한 번을 위해 수명주기·정리 정책을 새로 만들어야 한다. 화면 상태로 두면 둘 다 피할 수 있다.

> **구현 범위(2026-08-06)**: `departments/00-ceo-office/src/mandate/mandate_assistant.py` + `app.py`의 `POST /governance/v1/mandate-assistant/suggest`. 아래 예시 중 **`investment_horizon_years`/`liquidity_need`/`objective_text` 3개 필드만 실제로 동작한다.** `universe_policy.preferred_sectors`(업종)와 `forbidden_assets`(종목)는 §2.5(재일, KRX 업종 코드·종목 검색)가 없어 **`ALLOWED_SUGGESTION_FIELDS`에 아직 포함하지 않았다** — LLM이 존재하지 않는 코드를 지어내지 않도록 의도적으로 막은 것이다. §2.5가 붙으면 이 문서와 `ALLOWED_SUGGESTION_FIELDS`를 함께 확장한다.

`POST /governance/v1/mandate-assistant/suggest`

```json
{
  "fund_id": "uuid",
  "messages": [
    {"role": "user", "content": "10년쯤 투자할 생각이고, 급하게 현금화할 일은 없어요."}
  ],
  "current_draft": { "...": "화면이 현재 들고 있는 policy 초안 (선택, 맥락용)" }
}
```

**Response — 제안일 뿐 확정이 아니다**

```json
{
  "reply": "장기 투자와 낮은 유동성 필요를 확인했습니다.",
  "suggestions": [
    {"field": "investment_horizon_years", "value": 10,
     "label": "10년", "confidence": "HIGH", "source": "llm_extraction"},
    {"field": "liquidity_need", "value": "LOW",
     "label": "낮음", "confidence": "HIGH", "source": "llm_extraction"}
  ],
  "requires_user_confirmation": true,
  "dropped_fields": []
}
```

업종·종목처럼 아직 지원하지 않는 필드를 LLM이 언급해도(§2.5 붙기 전) 응답에 나타나지 않는다 — allow-list 밖 출력은 `dropped_fields`에 필드명만 기록되고 버려진다.

**계약 불변식 (전부 구현·자체 점검으로 확인)**

1. **`requires_user_confirmation`은 항상 `true`다.** 이 응답만으로 저장되는 값은 없다.
2. `suggestions[].field`는 **allow-list**(`ALLOWED_SUGGESTION_FIELDS`)된 경로만 허용한다. `mindset`·`experience`는 목록에 없다 — LLM이 성향을 추론하지 않는다(`suitability.py` 계약). 목록 밖 필드는 `dropped_fields`에 남고 조용히 사라지지 않는다.
3. 이 Route는 **어떤 상태도 변경하지 않는다.** 저장은 §2.2·§2.3 경로로만 일어난다.
4. LLM 실패(Schema 2회 위반, API 오류, 패키지 미설치, Rate limit 등)는 endpoint가 **500이 아니라 빈 제안 + 안내 문구**로 감싼다 — 채팅 UI가 LLM 장애 한 번으로 멈추지 않는다. **추측값으로 채우지는 않는다**(개발 원칙 9) — 실패 시 채워지는 값은 없다, reply만 안내 문구로 바뀐다.

**현재 allow-list (`ALLOWED_SUGGESTION_FIELDS`)**

```
investment_horizon_years, liquidity_need, objective_text
```

**확장 예정, 아직 없음** — §2.5 완료 후 추가

```
universe_policy.preferred_sectors, universe_policy.excluded_sectors,
allowed_assets, forbidden_assets
```

**영구 금지 (allow-list에 절대 넣지 않음)**

```
mindset, experience,               # 성향·경험 추론 금지 (suitability.py 계약)
risk_bounds.*,                     # 한도 값 적정성 판정 금지 - "조정 제안" UX가 생기면 별도 계약 필요
approval_rules.*, base_capital, currency
```

### 2.5 업종·종목 매칭 — 신규 Route (리서치본부)

| Method/Path | 용도 | 담당 |
|---|---|---|
| `GET /research/v1/sectors/resolve?q=반도체` | 자연어 → KRX 업종 코드 후보 (별칭 사전) | 재일 |
| `GET /research/v1/sectors/{sector_code}/instruments` | 업종 → 종목 목록 (매번 구성, **D**) | 재일 |
| `GET /research/v1/instruments/search?q=` | 종목명 → `instrument_id` | 재일 |

`sectors/resolve` 응답 예시

```json
{
  "query": "반도체",
  "matches": [
    {"sector_code": "G2510", "sector_name": "반도체와반도체장비",
     "matched_alias": "반도체", "confidence": "HIGH"}
  ]
}
```

> ⚠️ **현재 `reference.issuers.industry_code`는 OpenDART 표준산업분류이고 대부분 NULL이다.** KRX 업종 코드와 다른 체계이므로 그대로 쓸 수 없다. 별도 수집 경로가 선행돼야 한다.

## 3. Route Registry 반영

신규·변경 Route는 [route-registry.v1.json](contracts/route-registry.v1.json)에 등재해야 계약 테스트를 통과한다.

| App | 추가 | 상태 |
|---|---|---|
| `governance-api` | `POST /governance/v1/mandate-assistant/suggest` | 신규 |
| `governance-api` | `GET /governance/v1/mandates/by-fund/{fund_id}/current` | 신규 |
| (미정) | `POST /portfolio/v1/investor-profiles` 등 | 회계본부 App 결정 후 |
| `research-*` | `sectors/resolve`, `sectors/{code}/instruments` | 리서치본부 App |

`GET .../current`의 응답 확장 자체는 Route 목록이 바뀌지 않아 Registry 변경이 없었지만, `by-fund` 조회는 신규 Route라 위 표대로 등재했다(2026-08-06, `app.openapi()`와 exact match 확인).

## 4. 결정론 경계 요약

| 판정 | 주체 |
|---|---|
| 값 범위·상호 모순 (포함관계, `daily ≤ drawdown`, 교집합) | `policy.py` (결정론) |
| 변경 방향 TIGHTEN/LOOSEN/NEUTRAL | `classify_change()` (결정론) |
| `effective_risk_band` | `suitability.py` `min(mindset, experience)` (결정론) |
| 자연어 → 구조화 값 **제안** | LLM |
| 업종 별칭 매칭 | 사전 기반 (결정론) + LLM 보조 |
| 한도 집행 | Risk Engine (§6 공백 참고) |

**LLM은 어느 판정도 소유하지 않는다.** 제안하고, 사람이 확인하고, 결정론 코드가 검증한다.

## 5. 본부별 구현 작업

| 담당 | 작업 |
|---|---|
| **영주** (CEO Office) | ~~§1.1~1.3 `policy.py`·`service.py` 확장~~ ✅(main 병합 완료, classify_change 보완 포함) / ~~§2.1 응답 확장 + fund_id 조회~~ ✅(2026-08-06) / ~~§2.4 챗봇 제안 API~~ ✅(2026-08-06, 3개 필드만 — 업종·종목은 §2.5 대기) / ~~§3 Registry 등재~~ ✅(by-fund·mandate-assistant Route) |
| **동규** (리스크·QA) | 프리셋 9칸 수치 확정 / 프리셋 이탈 처리 규칙 / `max_sector_weight` 집행(포트폴리오 풀 자가점검) / `forbidden_asset_classes` 집행 / §2.4 allow-list가 실제로 지켜지는지 QA 검증(자체 점검은 있으나 독립 검증 아님) |
| **도현** (트레이딩·회계, Frontend Platform) | §1.5 마이그레이션 + Repository / §2.3 Route / `suitability.py` 저장 연동 / BFF에 **portfolio Router 신설**(governance Router는 이미 등록됨, §6.1) + **`by-fund`·`mandate-assistant` 프록시 추가**(신규 Route 2개 모두 아직 BFF에 안 뚫려 있음) / 온보딩 화면의 localStorage 우회를 §2.1 서버 조회로 교체 / `asset_class` 표준값 |
| **재일** (리서치·퀀트) | §2.5 3개 Route / KRX 업종 코드 수집 / 업종 별칭 사전 / 종목 검색 — **완료되면 §2.4 `ALLOWED_SUGGESTION_FIELDS`에 4개 필드 추가하는 작업이 뒤따른다(영주)** |

## 6. 선행·미해결

> **2026-08-12 갱신** — §1.5, §2.3, §6.1 #2가 구현됐다.
>
> - **§1.5 `accounting.investor_profiles`** ✅ — `supabase/migrations/20260812000200_accounting_investor_profiles.sql`(이 문서 DDL 그대로) + `departments/05-accounting-portfolio/portfolio/investor_profile_repository.py`. version 할당은 `insert ... select max+1` 한 문장이라 조회·삽입 사이 경합이 없고, 겹치면 `unique(user_id, fund_id, version)`이 잡는다.
> - **§2.3 `/portfolio/v1/investor-profiles`** ✅ — `departments/05-accounting-portfolio/api/app.py`(accounting-api와 같은 앱). §6.2 미확정 5번("portfolio-api를 어느 App으로 띄울지")은 **새 서비스를 만들지 않고 accounting-api에 실는 것**으로 정리했다 — §2.3이 이 Route를 회계/포트폴리오본부에 배정했고 그 본부 API가 이미 있다. `effective_risk_band`는 `suitability.effective_risk_band()`(신설 공개 함수)가 `_risk_band()` 표를 재사용한다.
> - **§6.1 #2 BFF 프록시** ✅ — `by-fund`·`mandate-assistant`·`investor-profiles`(2개)에 더해 **Mandate 부모 행 생성**(`POST /ui/mandates`)과 `versions`까지 6개를 뚫었다. Registry 등재 완료, `app.openapi()`와 일치 확인.
> - **새로 발견해 메운 공백**: `governance.mandates` INSERT가 `change_workflow.py` 자체 점검 코드 안에만 있어 **최초 Mandate를 만들 API가 없었다** — Version 제안 경로는 전부 `mandate_id`를 path로 받으므로 온보딩 첫 사용자는 시작할 수 없었다. `POST /governance/v1/mandates` 신설(DRAFT/v0 반환, `unique(fund_id,name)` 충돌은 409 + 기존 id).
> - **§3.2 프리셋 9칸** — 수치는 여전히 동규님 확정 대기다. 구조와 검증만 `ai-office/app/lib/mandatePresets.ts`에 `PROVISIONAL`로 표시해 두었다(제약 검증 함수 포함). 잠정값은 스펙에 이미 있는 `min(mindset, experience)` 규칙에서만 끌어내 새 위험 판단을 만들지 않았고, **그 결과 9칸이 3등급으로 수렴한다** — 경험·성향이 `min()` 말고 다른 방식으로도 한도에 영향을 줘야 하는지가 동규님께 드리는 질문이다.
> - **요청자 판정** — `apps/api/current_user.py`로 모았다. `X-User-Id`는 인증이 아니며(서명·만료 없음) 공개 배포 전 교체 대상이다.

### 6.1 반드시 선행돼야 하는 것

1. ~~**§2.1 응답 확장**~~ — ✅ 완료(2026-08-06). 남은 건 프론트가 이 값을 실제로 써서 localStorage 우회를 걷어내는 작업(도현).
1a. ~~**§2.4 챗봇 제안 API**~~ — ✅ 완료(2026-08-06), 3개 필드만. `anthropic` 패키지는 `requirements.txt`에 있으나 현재 개발 환경에 설치돼 있지 않다(자체 점검이 이 경우도 커버 — LLM 실패 시 500 대신 빈 제안으로 감싸는 경로가 바로 이 상태로 검증됐다). 배포 전 `pip install anthropic` 확인 필요.
2. **portfolio Router 신설 + by-fund 프록시** — `apps/api/main.py`에 **governance Router는 이미 등록돼 있다**(`/ui/mandates/{id}/change-requests`, `/ui/mandates/{id}/current`, `/ui/mandate-cases/{id}/advance`, `/ui/mandate-cases/{id}/timeline`, `/ui/mandate-approvals`, `/ui/mandate-approvals/{id}/decide` 6개, `_governance_request()`가 governance-api로 프록시). 신규 `by-fund/{fund_id}/current`(§2.1)는 아직 이 프록시 목록에 없다 — governance-api 자체는 구현됐지만 BFF를 안 거치면 Frontend가 직접 호출할 수 없다(AI_OFFICE_FRONTEND_PLAN §6). §2.3 `investor-profiles` Route를 실을 portfolio Router도 여전히 없다.
3. **KRX 업종 코드 수집** — §2.5가 이것 없이는 동작하지 않는다.

### 6.2 미확정

1. 프리셋 9칸 수치
2. `asset_class` 표준 코드값
3. `max_drawdown_pct` 이중 저장 정합 — Mandate와 InvestorProfile 양쪽 값이 항상 같아야 하는지
4. 프리셋 이탈 시 처리 (경고/차단/검토 승격)
5. `portfolio-api`를 어느 App으로 띄울지 (§3 Registry 항목 미정)

### 6.3 집행 공백 (변함 없음)

Risk Engine은 여전히 `risk.policies`·`risk.limits`를 읽고 `governance.mandate_versions`를 읽지 않는다([USER_INPUT_SPEC.md](../01-product/USER_INPUT_SPEC.md) 및 SCOPE_ANALYSIS 부록 A.9 §L).

**이 명세를 전부 구현해도 그 공백이 남아 있는 한 한도는 집행되지 않는다.** 화면·API 문서 어디에도 "이 한도로 차단된다"고 쓰지 않는다.
