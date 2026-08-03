# Notion 부서별 데이터베이스 설계

> **현재 기준(2026-08-03)**: 이 문서의 아래 초기 설계 문장 중 “01/03/06만 자동 채움”과 “원본 리포트 Rich Text 보관”은 폐기된 과거 기준이다. 현재 Reporter는 구조화 속성을 저장하고, Markdown 리포트는 Notion `children` block으로 렌더링한다. 실제 업로드 성공 여부는 자격증명·DB ID·API 응답으로만 판정한다.

## 0. 현재 연동 상태

| 영역 | 현재 상태 | 성공 판정 |
|---|---|---|
| 부서 Reporter | CEO, Research, Trading, Risk, Accounting, QA, HR 어댑터 구현 | 함수 존재만으로 연결 완료로 표시하지 않음 |
| Quant / Backtest | Notion Reporter 미구현 | 백테스트 결과 발행 계약과 DB 스키마 확정 후 구현 |
| Markdown 표시 | `departments/notion_markdown.py`가 제목·표·목록을 block으로 변환 | `children` payload와 HTTP 200 응답 확인 |
| 원본 리포트 속성 | 필수 아님; 전문을 rich-text 속성에 넣지 않음 | `report_artifact_ref` 또는 페이지 본문으로 추적 |
| Source of Truth | 로컬 report artifact와 운영 DB | Notion은 사람이 보는 Projection |

Reporter 결과 필드는 `adapter_present`, `credentials_configured`, `upload_succeeded`, `url`처럼 분리해 기록한다. `upload_succeeded=false`는 부서 판정 실패가 아니며, Notion 장애가 Risk/QA/Accounting의 바인딩 결정을 바꾸지 않는다.

담당: 동규 (리스크/QA) 제안, 8개 본부 전체에 적용
근거: [ai-office/worker/report.ts](../../../ai-office/worker/report.ts)의 기존 "김비서 일일 브리핑" 단일 DB 연동,
      [CLAUDE.md](../../../CLAUDE.md) 부서 토폴로지·권한 분리 원칙,
      departments/{01-research,03-risk,06-ai-qa-audit}/scripts.py의 실제 산출 필드

이 문서는 Notion AI(또는 Notion 쪽에서 DB를 실제로 만드는 사람/에이전트)에게 그대로 넘길 수 있는
설계 스펙이다. **Notion에 새 판단 기준을 만들지 않는다** — 아래 모든 Select/Number/Checkbox
속성은 이미 코드가 반환하는 필드를 그대로 옮겨 적는 것이고, 이 저장소의 원칙("LLM은 관련성
판단과 서술 작성에만 쓴다")이 Notion 쪽에도 그대로 적용된다.

⚠️ **`ai-office/company.config.ts`의 12개 부서(`research/brand/strategy1/qa/...`)와 이 문서의
8개 본부(`departments/00~07`)는 다른 것이다.** 전자는 픽셀 오피스 데모의 범용 커스터마이징
설정이고, 이 문서가 다루는 건 실제 헤지펀드 백엔드 8개 본부다. 섞지 않는다.

---

## 1. 원칙

1. **Notion은 Projection이지 Source of Truth가 아니다.** 결정론적 판정의 원본은
   `departments/<n>/reports/*.md`(각 부서 `_render_report_md`가 생성)와 예정된 Supabase
   테이블([Database Schema Foundation](../../database/README.md))이다. Notion 페이지는 그
   결과를 사람이 보기 좋게 복제한 것일 뿐 — Notion 쪽 값을 고쳐서 판정을 바꿀 수 없다.
2. **속성 값은 코드 출력을 그대로 옮긴다.** `run_risk_department()`, `run_qa_department()`,
   `run_research_department()`가 반환하는 dict의 키·enum 값을 속성명·Select 옵션에 1:1로
   맞춘다. Notion AI가 서술을 요약하거나 번역하는 건 되지만, verdict/severity 같은 판정 값을
   새로 만들거나 재해석하지 않는다(이 저장소 원칙: "LLM은 관련성 판단과 서술 작성에만").
3. **부서 간 쓰기 권한을 Notion에서도 섞지 않는다.** 각 부서 DB는 그 부서의 파이프라인(또는
   나중에 붙을 Worker)만 쓴다. 다른 부서는 Relation으로 읽을 뿐 그 DB에 항목을 만들지 않는다 —
   "Risk의 거부권, QA의 감사 권한을 다른 부서가 대신 수행하지 않는다" 원칙의 Notion 버전.
4. **아직 파이프라인이 없는 부서는 빈 DB만 만들고 자동 채움을 약속하지 않는다.** 지금 실제
   LangGraph 파이프라인(`scripts.py`)이 있는 곳은 01-research, 03-risk, 06-ai-qa-audit
   셋뿐이다(CLAUDE.md 기준). 나머지 5개는 스키마만 먼저 만들고, "언제 채워지나" 절을 DB
   설명(Description)에 그대로 남긴다 — 비어 있는 걸 연동된 것처럼 보이게 하지 않는다
   (ai-office CLAUDE.md의 "연결 안 된 걸 연결됐다고 표시하지 않는다"와 같은 원칙).

---

## 2. 전체 구조

```
HgFinance AI Office (워크스페이스 최상위 페이지)
└── 00 · CEO 오피스               ← 기존 "김비서 일일 브리핑" DB 재사용 (NOTION_BRIEFING_DB)
└── 01 · 리서치본부 DB              [자동 채움 가능 — run_research_department]
└── 02 · 트레이딩본부 DB            [설계만 — OMS 이벤트 연동 전]
└── 03 · 리스크본부 DB              [자동 채움 가능 — run_risk_department]
└── 04 · 퀀트·백테스트본부 DB        [설계만 — 백테스트 결과 발행 파이프라인 전]
└── 05 · 회계·포트폴리오본부 DB      [설계만 — 원장/대사 리포트 발행 파이프라인 전]
└── 06 · AI QA·감사본부 DB          [자동 채움 가능 — run_qa_department]
└── 07 · Agent 인사팀 DB            [설계만 — 채용 승인 파이프라인 전]
```

8개 DB를 페이지 하나 아래 나란히 두고, "허브" 페이지 상단에 8개 DB 링크만 나열한다. 회사
전체 롤업은 새로 만들지 않는다 — 지금 있는 00번 브리핑 DB가 그 역할이다(worker/report.ts가
이미 씀, 안 건드림).

---

## 3. 공통 속성 스펙 (8개 DB 전부 동일하게 시작)

새 DB를 만들 때 아래를 먼저 넣고, 부서별 절의 속성을 추가한다.

| 속성명 | 타입 | 값 출처 |
|---|---|---|
| 제목 | Title | 부서별로 다름 (아래 표) |
| trade_case_id | Rich Text | `contracts.py OrderIntent.trade_case_id` — 부서 간 Relation의 공통 키. 지금 각 부서 리포트가 이 값을 표면에 안 드러내는 경우가 있으니(예: risk 리포트는 side/quantity/instrument만 보여줌), Notion에 쓸 때는 반환 dict 밖의 원본 `order_intent`에서 가져와 채운다 |
| 판정 | Select | 부서별 verdict/decision enum (아래 표, 코드에 없는 옵션 추가 금지) |
| escalate | Checkbox | 각 파이프라인 `out["escalate"]` 그대로 |
| 서술 | Rich Text | 각 파이프라인 `out["narrative"]` (또는 QA의 `claim_narrative`) 그대로 — Notion AI가 다듬더라도 판정 값과 모순되면 안 됨 |
| calculation_version | Rich Text | `out["calculation_version"]` 또는 `PIPELINE_VERSION` — 재현성 추적용 |
| input_hash | Rich Text | 있으면 채움(risk/qa). 같은 입력이면 같은 값이어야 한다는 재현성 계약을 Notion에서도 볼 수 있게 |
| 원본 리포트 | URL 또는 Rich Text | `departments/<n>/reports/<파일명>.md` 경로. Notion 페이지 본문에 전체 리포트를 복사하지 않는다 — 원본이 바뀌면 Notion이 stale해지므로 링크만 |
| 생성 시각 | Date | 실행 시각(UTC) |

---

## 4. 부서별 DB — 지금 코드로 채울 수 있는 3개

### 4.1 `03 · 리스크본부 DB` (`departments/03-risk/scripts.py`)

| 속성명 | 타입 | 출처 / 옵션 |
|---|---|---|
| 제목 | Title | `f"{side} {quantity} {instrument_id}"` 같은 1줄 요약 |
| 판정 | Select | `approve` / `resize` / `reject` (`RiskVerdict`, `risk_engine.py`) |
| 승인 수량 | Number | `out["approved_quantity"]` |
| reason_codes | Multi-select | `RejectReason` enum(`risk_engine.py`) 17개 전체 — `stale_snapshot`, `market_not_tradable`, `outside_mandate`, `restricted_instrument`, `notional_below_minimum`, `notional_above_maximum`, `insufficient_buying_power`, `oversell`, `concentration_limit_soft`, `concentration_limit_hard`, `turnover_limit`, `order_count_limit`, `trading_state_blocked`, `loss_limit_breached`, `drawdown_limit_breached`, `counterparty_unhealthy`, `resized_to_zero`. 새 코드를 Notion에서 만들지 않는다 |
| check_results | Rich Text | `out["check_results"]` 10개 항목을 표로 옮김 (pass/fail + detail) |
| counterparty_narrative | Rich Text | `out["counterparty"]["counterparty_narrative"]` — **비어 있을 수 있다.** `counterparty_health` 체크가 DOWN/DEGRADED로 플래그된 case만 채워진다(조건부 노드, 2026-08-02 추가). 채워지지 않은 게 정상이라는 걸 Description에 명시 |
| compliance_verdict | Select | `out["compliance"]["answer"]["verdict"]` — `no_breach`/`breach`/`ambiguous` 3개 전체(`skills/agentic-rag/src/nodes.py`의 `compliance-policy-agent` 페르소나 프롬프트가 강제하는 값. 앞서 잘못 적었던 `grounded`/`inconclusive`는 이 필드 값이 아니다 — `grounded`는 `compliance["grounded"]` boolean 필드의 이름과 혼동한 것). REJECT 조기 종료 케이스는 compliance 자체가 없다 → 빈 값 정상 |
| trading_state | Select | `ENABLED`/`REDUCE_ONLY`/`ENTRY_BLOCKED`/`HALTED`(`TradingState`) |
| counterparty_health(원본) | Select | `ok`/`degraded`/`down`(`CounterpartyHealth`) — `counterparty_narrative`와 별개로, 어떤 상태였길래 조건부 노드가 불렸는지 그 자체를 남긴다 |

### 4.2 `06 · AI QA·감사본부 DB` (`departments/06-ai-qa-audit/scripts.py`)

| 속성명 | 타입 | 출처 / 옵션 |
|---|---|---|
| 제목 | Title | 감사 대상 Artifact 식별자 |
| 판정 | Select | `QaDecisionValue`(`evidence_qa_engine.py`) 4개 전체 — `PASS`/`WARN`/`CONDITIONAL`/`FAIL` (PASS·FAIL만 있는 게 아니다) |
| reason_codes | Multi-select | `CheckFailureReason` 8개 전체 — `evidence_not_found`, `evidence_access_denied`, `evidence_not_yet_valid`(PIT 위반), `numeric_citation_mismatch`, `fact_without_evidence`, `unacknowledged_contradiction`, `tool_summary_deviation`, `partial_evidence_set` |
| findings.severity | Select | `FindingSeverity` 4개 전체 — `LOW`/`MEDIUM`/`HIGH`/`CRITICAL` (findings 리스트 자체는 finding_type+severity+description 묶음이라 Rich Text 표로 유지, severity만 개별 Select로 뽑아 필터링용으로 둔다) |
| findings | Rich Text | `out["findings"]` — finding_type + severity + description 표 |
| claim_checks 판정 | Multi-select | `ClaimCheckResult` 5개 전체 — `SUPPORTED`/`PARTIAL`/`UNSUPPORTED`/`CONTRADICTED`/`NOT_APPLICABLE` |
| claim_checks | Rich Text | 개별 Claim 별 위 판정 + 근거 요약 표 |
| claim_narrative | Rich Text | `out["claim_narrative"]` |

### 4.3 `01 · 리서치본부 DB` (`departments/01-research/scripts.py`)

| 속성명 | 타입 | 출처 / 옵션 |
|---|---|---|
| 제목 | Title | `symbol` |
| evidence_quality | Select | `sufficient`/`partial`/`insufficient_evidence` 3개 전체 (`departments/01-research/scripts.py`가 소문자 토큰 3개로 검증) |
| 수치 재대조 | Checkbox | `numeric_check.ok` |
| halted | Rich Text | `out["reason"]` — HALTED 케이스만 채워짐(정상 케이스는 packet 전체가 옴, 최상위에 `verdict: HALTED`는 없음) |
| 분석가 판정 6종 | Rich Text × 6 | sentiment/technical/fundamental/regime/geopolitical/microstructure 각각 별도 속성. **Select로 전환하지 않는다** — 6개 analyst 파일을 확인한 결과 이들은 `risk_engine.py`의 `RejectReason`처럼 하나의 닫힌 StrEnum이 아니라, 분석가별로 다른 값 집합을 쓰고 일부는 LLM 서술(`note.stance`/`note.regime` 등)에서 준결정론적으로 도출된다. 지금까지 코드에서 관측된 값(완전한 목록이라는 보장 없음): technical(BULLISH/BEARISH/NEUTRAL/INSUFFICIENT_DATA), news_sentiment(SCORED/NO_EVIDENCE/INCONCLUSIVE — docstring에 정확히 3개로 명시된 유일한 케이스), fundamental(NOTED/READOUT_ONLY/INSUFFICIENT_DATA), sector_regime(BREADTH_THRUST/CAPITULATION/NEUTRAL/RISK_ON/RISK_OFF/INSUFFICIENT_DATA), geopolitical(SHOCK/CALM/ELEVATED/BALANCED/INSUFFICIENT_DATA), microstructure(STRESSED/ORDERLY/DEPTH_SKEW/ONE_SIDED_FLOW/SPREAD_WIDE/INSUFFICIENT_DATA). Select로 굳히면 코드에 없는 옵션을 강제로 지어내게 되므로 news_sentiment 외에는 Rich Text 유지가 맞다 |

---

## 5. 부서별 DB — 설계만 (아직 자동 채움 파이프라인 없음)

이 5개는 지금 `departments/<n>/scripts.py`가 없다(CLAUDE.md "다른 본부는 대부분 Profile과
설계 문서 단계"). DB Description에 아래 문구를 그대로 넣어 미연동 상태를 감춘 것처럼 보이지
않게 한다: *"이 DB는 스키마만 준비됨 — 자동 채움은 해당 부서 LangGraph 파이프라인이 결정론적
리포트를 반환한 뒤에 연결한다."*

| DB | 제목 후보 | 판정 Select 후보(코드 근거) | 비고 |
|---|---|---|---|
| `00 · CEO 오피스` | 브리핑명 | — (이미 운영 중, 4절 참고) | 신규 아님, 그대로 유지 |
| `02 · 트레이딩본부` | order_intent_id | `RiskVerdict` 아님 — OMS 자체 상태(`contracts.py` OMS state) | Order 상태 전이가 정의되면 확정. 지금은 `OrderIntent`/`RiskDecision` 필드만 훑을 수 있다 |
| `04 · 퀀트·백테스트본부` | strategy_id + backtest run | `BacktestResult`(`backtest_runner.py`) 지표(수익률/Sharpe/MDD 등) | Production 승격은 CEO·Risk·QA 3자 승인 — 승인 3개를 별도 Checkbox 3개로 분리, 하나로 합치지 않는다(권한 분리 원칙) |
| `05 · 회계·포트폴리오본부` | journal_id | `severity`: `Severity` 4개 전체 — `low`/`medium`/`high`/`material`. `match_method`: `MatchMethod` 5개 전체 — `broker_id`/`client_order_id`/`attribute`/`fuzzy_candidate`/`unmatched` (둘 다 `reconciliation.py`) | Ledger 원장은 append-only라 Notion에서 절대 수정 유도 UI를 두지 않는다 |
| `07 · Agent 인사팀` | 후보 role_code | `AGENT_EMPLOYEE_PROFILES.md`의 `RSK-01` 같은 코드 체계, `hiring_priority.tier` | "hr은 자기 후보를 스스로 최종 승인할 수 없다" — QA 독립검증/CEO 승인/IAM 생성을 3개의 별도 Checkbox + 담당자 Person 속성으로 분리 |

---

## 6. 부서 간 Relation

- 공통 키는 `trade_case_id`(3절)다. `02 트레이딩` 항목이 먼저 생기고, `03 리스크`·`05 회계`가
  같은 `trade_case_id`로 뒤따르는 순서이므로, Notion Relation 속성은 트레이딩 DB를 기준으로
  리스크/회계 DB를 향해 건다(리스크·회계가 트레이딩을 향해 걸면 트레이딩 항목이 아직 없을 때
  깨진다).
- `06 QA` DB는 감사 대상 Artifact가 어느 부서 것이든 될 수 있으므로, 특정 DB로 고정 Relation을
  걸지 않는다 — `대상 부서`(Select) + `대상 trade_case_id`(Rich Text) 조합으로 느슨하게 참조한다.
- Rollup은 만들지 않는다(예: "이 주문의 최종 상태"를 여러 DB에서 자동 합산). Case의 최종 상태는
  Supabase 쪽 `risk.risk_decisions` 등이 갖는 것이 맞고, Notion Rollup이 그 역할을 대신하면
  Projection이 Source of Truth 행세를 하게 된다(1절 원칙 위반).

---

## 7. 환경 변수 규칙

기존 패턴(`ai-office/worker/report.ts`, `ai-office/.dev.vars.example`)을 그대로 따른다 — 부서마다
DB ID를 별도 변수로 둔다. **토큰은 `NOTION_TOKEN` 하나로 8개 DB 전부 공유**(같은 Notion
Internal Integration에 8개 DB를 모두 연결).

```
# ai-office/.dev.vars (로컬, gitignore — 커밋 금지)
NOTION_TOKEN=                    # 기존 그대로
NOTION_BRIEFING_DB=              # 기존 00-ceo-office 그대로
NOTION_RESEARCH_DB=              # 신규 01
NOTION_TRADING_DB=                # 신규 02 (스키마만)
NOTION_RISK_DB=                  # 신규 03
NOTION_QUANT_BACKTEST_DB=        # 신규 04 (스키마만)
NOTION_ACCOUNTING_DB=            # 신규 05 (스키마만)
NOTION_QA_DB=                    # 신규 06
NOTION_HR_DB=                    # 신규 07 (스키마만)
```

배포 값은 파일이 아니라 `wrangler secret put`(ai-office/CLAUDE.md 기존 규칙 그대로). 이 문서
자체나 커밋되는 어떤 파일에도 실제 토큰·DB ID 값을 적지 않는다 — 변수 이름만 코드/문서에 남고,
실값은 `.dev.vars`(로컬)와 Cloudflare Secret(배포)에만 있다.

---

## 8. 다음 단계 (이 문서 밖 — 별도 작업)

1. Notion에서 위 8개 DB를 실제로 생성하고 Integration에 연결(사람 또는 Notion AI가 수행).
2. `ai-office/worker/report.ts`의 `sendNotion`을 부서별로 일반화(`sendNotionForDepartment(dept, payload, env)`) —
   지금은 `NOTION_BRIEFING_DB` 하나만 하드코딩돼 있어 부서별 분기가 없다. 이 작업은 이 문서
   범위가 아니라 별도 구현 티켓이다.
3. 01/03/06(자동 채움 가능한 3개)부터 연결하고, 나머지 5개는 각 부서 `scripts.py`가 생긴 뒤
   순서대로 붙인다 — 없는 파이프라인을 위해 Notion 쪽 코드를 먼저 만들지 않는다(YAGNI).
