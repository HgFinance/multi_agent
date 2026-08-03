# Notion 부서별 데이터베이스 설계

> Current runtime 기준: 2026-08-03

Notion은 부서 산출물을 사람이 검토하기 위한 Projection이다. 결정론적 Risk/QA Engine, OMS, Ledger, NAV, Hermes 세션의 Source of Truth가 아니며 Notion 값을 수정해 binding decision을 바꿀 수 없다.

## 1. 현재 조직과 Reporter 상태

실제 조직은 CEO와 7개 Hermes 부서다. `ai-office`의 화면 Projection과 이 문서의 부서 DB를 혼동하지 않는다.

| 조직 | Profile | Reporter adapter | 현재 판정 |
|---|---|---|---|
| CEO | `ceo-agent` | `departments/00-ceo-office/notion_reporter.py` | adapter 존재; credential·HTTP 성공 별도 확인 |
| Research | `research-department` | `departments/01-research/notion_reporter.py` | adapter 존재; credential·HTTP 성공 별도 확인 |
| Trading | `trading-department` | `departments/02-trading/notion_reporter.py` | adapter 존재; OMS 결과와 분리 |
| Risk | `risk-management` | `departments/03-risk/notion_reporter.py` | adapter 존재; Risk Engine 결과의 Projection |
| Quant/Backtest | `quant-backtest-department` | 없음 | Reporter 미구현; 백테스트 발행 계약이 선행 |
| Accounting/Portfolio | `accounting-portfolio-department` | `departments/05-accounting-portfolio/notion_reporter.py` | adapter 존재; Ledger/NAV Source of Truth 아님 |
| AI QA/Audit | `qa-department` | `departments/06-ai-qa-audit/notion_reporter.py` | adapter 존재; Evidence QA 결과의 Projection |
| HR | `hr-department` | `departments/07-agent-workforce/notion_reporter.py` | adapter 존재; 인사·Lifecycle 결과의 Projection |

`adapter 존재`는 코드가 있다는 뜻일 뿐 업로드 성공을 의미하지 않는다. 실제 성공은 `credentials_configured`, HTTP 응답, `upload_succeeded`, `report_path`를 실행 로그에서 확인한다.

## 2. 보고서 렌더링 규칙

- Python Reporter는 결정론적 결과와 구조화 속성을 만든다.
- Markdown 본문은 `departments/notion_markdown.py`가 제목·문단·목록·표·인용·코드 블록을 Notion `children` block으로 변환한다.
- Notion `rich_text` 속성은 Markdown 렌더링 영역이 아니다. 숫자·ID·enum·짧은 설명 같은 구조화 값만 저장한다.
- `원본 리포트` 속성은 필수가 아니다. 전체 Markdown을 한 개의 rich-text 속성에 넣지 않으며, 필요하면 `report_path` 또는 `report_url`만 보존한다.
- Notion AI가 서술을 다듬더라도 verdict, decision, severity, reason code, input hash를 새로 만들거나 재해석하지 않는다.

## 3. 공통 속성

모든 부서 DB는 다음 공통 키를 우선 사용한다. 부서별 추가 속성은 실제 Reporter 반환 필드와 1:1로 맞춘다.

| 속성 | Notion 타입 | 규칙 |
|---|---|---|
| 제목 | Title | `department`와 `case_id` 또는 결정 ID를 포함 |
| case_id | Rich text | 부서 간 handoff 공통 키 |
| decision_id | Rich text | 부서별 결정·감사 ID |
| verdict/action | Select | 코드의 enum만 등록; 없는 옵션을 Notion에서 만들지 않음 |
| reason_codes | Multi-select 또는 Rich text | 결정론적 검사 결과를 그대로 저장 |
| input_hash | Rich text | 동일 입력 replay 식별용 SHA-256 |
| calculation_version | Rich text | Engine/Reporter 버전 |
| as_of / decision_time | Date 또는 Rich text | PIT와 생성 시각을 구분 |
| trace_id / run_id | Rich text | LangGraph·Hermes·DB audit 추적 키 |
| report_path | URL 또는 Rich text | 로컬/저장소 Markdown 경로. 전체 원문을 속성에 복사하지 않음 |
| upload_status | Select | `not_configured`, `uploaded`, `failed` 등 adapter 결과 |

## 4. 부서별 바인딩 경계

### Risk

Risk Reporter는 `risk_engine.py`의 결정, check 결과, Risk Snapshot, fallback/reason code를 표시한다. `approve`, `resize`, `reject`, position cap, market tradable, stale input 등의 의미는 결정론적 Risk Engine이 소유한다. Hermes Head와 Risk Worker는 근거·context만 제공하며 주문·원장·한도를 직접 변경하지 않는다.

### AI QA/Audit

QA Reporter는 `evidence_qa_engine.py`의 `PASS/WARN/CONDITIONAL/FAIL`, claim 결과, finding, escalation, retry/fallback, trace 정보를 표시한다. Evidence QA Engine이 binding verdict를 소유하고, QA Worker와 Hermes Head는 검증 context와 설명만 제공한다.

### Research·Trading·Accounting·CEO·HR

각 Reporter는 해당 부서의 구조화 결과만 기록한다. Trading의 OrderIntent는 주문이 아니며, Accounting의 Projection은 Ledger Posting/NAV 확정이 아니고, CEO 페이지는 최종 요약일 뿐 주문·Risk 승인·원장 수정 권한을 갖지 않는다.

### Quant/Backtest

현재 Notion Reporter가 없다. Backtest 결과의 PIT·parameter·cost·Sharpe/MDD·artifact 계약과 QA/Risk/CEO 승격 Gate를 먼저 확정한 뒤 별도 adapter를 추가한다.

## 5. 저장·권한·보안

- Notion token과 DB ID는 `.env`, `ai-office/.dev.vars`, Cloudflare Secret에만 둔다. 문서·로그·Prompt·Frontend bundle에 실값을 기록하지 않는다.
- 부서별 Reporter는 자신의 DB만 쓰며, 다른 부서 DB에 직접 쓰지 않는다.
- Notion DB의 편집 권한은 사람이 부여하되, 승인·주문·원장·Risk Limit의 권한으로 확장하지 않는다.
- 업로드 실패는 부서 pipeline 성공으로 간주하지 않는다. 원본 Markdown과 실패 사유를 보존하고 안전한 fallback을 따른다.
- DB가 없거나 API가 실패해도 결정론적 판정과 로컬 report 생성은 독립적으로 검증 가능해야 한다.

## 6. 검증 체크리스트

1. Reporter 입력이 현재 부서 계약과 일치하는가.
2. `input_hash`, `run_id`, `trace_id`, `calculation_version`이 남는가.
3. Markdown이 `children` block으로 변환되는가.
4. 구조화 속성의 enum이 코드와 일치하는가.
5. 자격증명 미설정, DNS 오류, HTTP 오류가 `upload_failed`로 기록되는가.
6. Notion 내용을 다시 읽어 binding decision을 덮어쓸 수 없는가.
7. 동일 report를 재업로드할 때 idempotency 또는 중복 방지 키가 있는가.

## 7. Historical snapshot

다음 문구와 설계는 2026-08-03 이전의 초기 계획이며 현재 상태로 해석하지 않는다.

- “01/03/06만 자동 채움, 나머지 5개는 설계만”이라는 초기 연결 계획
- Markdown 원문 전체를 Notion 단일 `rich_text` 속성에 저장하던 방식
- `ai-office/company.config.ts`의 예전 범용 부서 수·이름
- 실제 DB 생성 전의 8개 Notion DB 예상 목록

현재 연결 여부는 이 문서의 Reporter matrix와 각 실행 결과의 `adapter_present`, `credentials_configured`, `upload_succeeded`로 판정한다.
