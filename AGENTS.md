# AGENTS.md

이 저장소의 Codex 작업 지침이다.

## 공통 지침

- 작업을 시작하기 전에 루트의 [CLAUDE.md](CLAUDE.md)를 읽고 저장소 구조, 명령어, 아키텍처, 권한 경계를 따른다.
- [`.claudeignore`](.claudeignore)에 지정된 파일과 폴더는 Claude와 Codex가 공통으로 무시한다. 특히 비밀값, 로컬 실행 상태, 대용량 산출물을 읽거나 공유하지 않는다.
- 작업 대상이 `ai-office/` 아래라면 [`ai-office/CLAUDE.md`](ai-office/CLAUDE.md)의 하위 지침도 적용한다.
- 시스템·개발자·사용자 지시가 이 문서보다 우선한다. 하위 디렉터리의 더 구체적인 지침은 해당 범위에서 우선한다.
- 기존 사용자 변경사항은 보존하고, 요청 범위를 벗어난 파일은 수정하지 않는다.
- 파일을 수정한 뒤에는 변경 범위에 맞는 테스트나 검증을 실행하고 결과를 보고한다.

## 저장소의 현재 상태

- 이 저장소는 완성된 서비스가 아니라 설계 문서, 실행 가능한 Prototype, Migration, Schema Contract Test가 함께 있는 초기 구현 단계다.
- `docs/`가 설계의 Source of Truth다. 문서 기준은 `docs/HEDGE_FUND_MASTER_PLAN.md`를 최상위로 하며, 하위 문서는 상위 문서를 임의로 변경할 수 없다.
- 현재 실행 기준은 `departments/<n>/` 아래의 8개 부서다. 구 경로인 `orchestration/hermes/`, 루트 `trading/`, `execution/`, `accounting/`, `fetch_news.py`를 새 실행 경로로 되살리지 않는다.
- 트레이딩·회계는 D0~D2 Prototype 수준이고, Risk에는 `skills/agentic-rag`의 `compliance-policy-agent`만 실제 LangGraph baseline으로 연결돼 있다. Profile에 적힌 모든 직원이 구현 완료됐다고 가정하지 않는다.
- DB Prototype 통합과 전체 구조 Gate는 아직 진행 전이다. 구현 상태는 각 부서 `config.yaml`의 `implementation:` 및 `agentic_rag.status`를 확인한다.

## 핵심 디렉터리

| 경로 | 역할 |
|---|---|
| `docs/` | 제품·조직·데이터·기술 설계 문서 |
| `departments/<n>/hermes/` | Git으로 관리하는 Hermes 부서 Profile (`config.yaml` + `SOUL.md`) |
| `skills/agentic-rag/` | Risk의 `compliance-policy-agent` Agentic RAG baseline |
| `departments/01-research/collectors/` | 시세·호가·체결·시장 파생 관측 전용 수집기 |
| `departments/01-research/api/external_*.py` | 뉴스·공시·거시의 비영속 요청형 MCP 조회 |
| `departments/02-trading/` | 계약, Paper OMS, Broker Prototype |
| `departments/05-accounting-portfolio/` | 원장·대사 Prototype |
| `supabase/migrations/` | Canonical 운영 DB Migration |
| `timescaledb/migrations/` | 시장 시계열 DB Migration |
| `db/001_execution.sql`~`db/004_seed.sql` | D0~D2 Prototype 전용 SQL |
| `ai-office/` | 현재 프론트엔드 Demo 실행 경로 |
| `apps/api/` | Read-only Projection + ADR-0007의 좁은 고정 fixture PAPER Command BFF |

`ai-office/`는 향후 `apps/operator-web/`로 이전할 목표 경로가 있지만, 현재 작업에서는 이름만 바꾸거나 금융 상태를 실제 운영 상태처럼 표현하지 않는다. `ai-office/app/game/`는 시뮬레이션 엔진이므로 커스터마이징 작업에서 수정하지 않는다.

## 아키텍처 원칙

### 부서와 직원의 실행 계층

- 부서는 Hermes Profile로 실행하고, 부서 안의 각 LLM 직원은 독립 LangGraph Worker Graph로 구현한다. 운영 AWS Worker는 공용 Qwen AWQ v1(`qwen2.5-14b-instruct-awq`)을 Worker Model Gateway와 Compose vLLM을 통해 사용한다. Ollama `qwen3:1.7b`는 로컬 개발 fallback으로만 남기며, 부서장은 Hermes가 Codex를 기본으로 호출하고 승인된 Claude Code를 대체 런타임으로 사용할 수 있다.
- Hermes는 부서 단위 오케스트레이션, Queue, Memory Namespace, Tool Allowlist를 담당한다.
- 현재 Profile의 `agent.personalities`는 호환 Alias·메타데이터일 수 있다. 실제 직원 실행 여부는 각 부서의 Worker Registry, Worker 구현과 `config.yaml`을 함께 확인한다.
- Worker 수·역할·trigger·tool 권한의 Source of Truth는 [WORKER_ROLE_BOUNDARIES.md](docs/02-engineering/WORKER_ROLE_BOUNDARIES.md)와 각 Profile의 `workers`/`runtime_personalities`다. 미구현 기능은 가짜 코드로 채우지 않고 구현 상태를 기록한다.

### 8개 부서와 흐름

- 투자 본부는 리서치, 트레이딩, 리스크, 퀀트/백테스트, 회계·포트폴리오, AI QA·감사의 6개다.
- `hr-department`는 제7 투자 본부가 아니라 CEO 직속 Shared Service다.
- `workflow`: research → trading → risk → qa → accounting → ceo
- `strategy_research_cycle`: quant-backtest → qa → ceo
- `workforce_management_cycle`: 신규 Agent 채용
- `agent_evolution_cycle`: 기존 Agent Profile 개선
- `event_routing`: 이벤트 유형에 따라 필요한 페르소나만 동적으로 호출
- 모든 Step은 실패를 통과로 취급하지 않으며, 실패 시 REJECT/HOLD/DENY/ESCALATE/ROLLBACK 같은 안전한 방향으로 처리한다. 자동으로 승인·승격·권한부여하는 fallback을 만들지 않는다.

### 절대적인 권한·데이터 경계

- 부서 간 권한을 이전하거나 합치지 않는다. 담당자가 같아도 Risk와 QA, Trading과 Accounting의 권한은 분리한다.
- `Agent Decision` ≠ `Strategy Signal` ≠ `OrderIntent` ≠ `Order`다.
- Agent·alpha·전략 Worker·rebalancer가 만든 모든 자동 주문 후보는 결정론적 Risk Engine을 통과한다. Risk Agent는 근거와 권고만 만들고 바인딩 집행·한도 관리는 Risk Engine이 맡는다.
- `trader-pm-agent`를 포함한 Agent는 주문을 직접 전송하지 않는다. 자동 주문에는 Risk/Compliance Gate가 선행돼야 한다.
- 예외는 [ADR-0007](docs/02-engineering/adr/0007-authenticated-user-paper-directive-authority.md)의 `USER_DIRECTIVE`뿐이다. BFF가 고정 fixture로 선택한 사용자·ACTIVE Fund/Book의 명시적 PAPER 주문은 Agent 주문이 아닌 사용자 지시이며 Risk·alpha·rebalancer의 경제적 veto 대상이 아니다. 그래도 fixture actor map, membership, 결정론 parser, cash/position/reservation, lot/tick/TTL, 멱등성, durable PAPER store admission은 우회할 수 없다. 브라우저 로그인·세션은 만들지 않는다.
- Hermes는 위 사용자 권한을 갖지 않는다. 원문 명령을 자의로 보충하거나 주문을 만들지 않고 대화 인터페이스로만 전달하며, 결정론 parser와 BFF가 구조화한다. LIVE 주문은 허용하지 않는다.
- CEO는 주문 제출, 리스크 승인, 원장 수정, NAV 확정, Audit Finding 종결 권한이 없다.
- `quant-backtest-department`는 Production 승격을 직접 수행하지 않는다. CEO·Risk·QA 승인이 필요하다.
- LLM은 관련성 판단과 서술 작성에만 사용한다. PIT 필터, 인용 검증, 한도 검사, 상태 전이는 결정론적 코드가 담당한다.
- 위험한 기능은 실패 시 거래 확대가 아니라 신규 진입 차단 방향으로 동작한다.

## Hermes Profile 규칙

- 각 부서는 `departments/<n>/hermes/config.yaml`과 `SOUL.md` 한 쌍으로 관리한다.
- 저장소 Profile과 실제 Runtime Profile은 별개다. Runtime은 `~/.hermes/profiles/<department>/`이며 `auth.json`, `.env`, `memories/`, `sessions/`, `state.db*` 등을 포함할 수 있으므로 Git에 넣지 않는다.
- Profile을 수정하면 `./scripts/sync_hermes_profiles.sh push`, 로컬 Runtime에서 역으로 반영하면 `./scripts/sync_hermes_profiles.sh pull`을 사용한다.
- `config.yaml`의 `env`는 부서별로 다르므로 임의로 API Key 종류를 통일하지 않는다. 현재 저장소 기준 8개 Profile의 Head는 `provider: openai-codex` / `gpt-5.6-luna`이며, 승인된 Claude Code 대체 런타임을 허용한다. 직원은 Head 모델과 분리된 독립 LangGraph Worker + 운영 Qwen AWQ v1이다. 로컬 Ollama `qwen3:1.7b`는 명시적 개발 fallback이며, 과거 Nous/Laguna 값은 문서에 남길 때 `Historical snapshot`으로 표시한다. vLLM은 `scripts/model_plane/vllm_runtime.sh` 외의 수동 `docker run`/raw Compose 명령으로 시작하지 않는다.
- 미구현 기능은 가짜 코드로 채우지 말고 주석 백로그와 구현 상태 필드로 남긴다.
- 부서 Profile의 페르소나 프롬프트는 영어 2인칭(`You are the ...`)을 사용하고, 파일 상단 설명·주석은 한국어로 작성한다.

## Agentic RAG 주의사항

- `skills/agentic-rag`는 `retrieve → grade → generate → hallucination_check → retry` 흐름이며 최대 3회 재시도한다.
- PIT 필터와 인용 검증은 Python 결정론적 코드, LLM은 grade/generate에만 사용한다.
- `corpus/compliance/*.md`는 모두 `SAMPLE_PLACEHOLDER`다. 실제 정책으로 교체하기 전 결과를 운영 판단에 사용하지 않는다.
- `grounded: false`는 성공이 아니라 inconclusive이며 escalate한다.
- Frontmatter 파서는 한 줄 `key: value`만 읽으므로 중첩 YAML·리스트를 가정하지 않는다.
- Retriever를 교체할 때는 `search()`의 `DocumentChunk` → `list[ScoredChunk]` 인터페이스를 유지한다.

## DB·프론트엔드 경계

- `db/`의 D0~D2 Prototype SQL은 `supabase/migrations/`와 같은 DB에 함께 적용하지 않는다. 통합 규칙은 [Database Schema Foundation](docs/database/README.md)을 따른다.
- 프론트엔드는 금융 상태의 Projection일 뿐이다. Supabase Service Role, Broker·LS Credential, Risk 계산, OMS 상태 전이, Ledger Posting을 소유하지 않는다.
- Notion·Discord 연동의 기본값은 미연동이며 정상 상태다. 실제 연동 요청이 있을 때만 `.dev.vars.example`에서 `.dev.vars`를 만들도록 안내한다.
- `.dev.vars`와 `.env*`, `auth.json`은 비밀값으로 취급한다. 내용을 출력하거나 커밋·압축·공유하지 않는다.

## 작업 시 우선순위

1. 문서와 데이터 계약을 확인한다.
2. 결정론적 계약·Risk·OMS·Ledger 경계를 먼저 안정화한다.
3. LLM 출력은 Pydantic Schema로 검증한다.
4. Backtest·Replay에는 미래 데이터와 실제 Broker Credential이 들어가지 않게 한다.
5. Position은 Fill 또는 승인된 Adjustment로만 변경한다.
6. 새 Library를 추가할 때는 기존 Stack으로 해결할 수 없는 이유와 제거 기준을 함께 기록한다.
7. 구현 완료는 코드 작성이 아니라 Acceptance Scenario 통과로 판단한다.

## Risk·QA 브랜치 실행 환경

- Risk·QA 부서 브랜치의 Python 테스트, 계약 검증, 실행 점검과 lint는 전용 Claude 환경을 사용한다.
  작업 시작 전에 `source ~/claude/bin/activate`를 실행하고 `python --version`과 `command -v ruff`를 확인한다.
- 저장소 `.venv`와 `~/claude` 환경을 혼용하지 않는다. CI는 workflow에 명시된 Python 버전을 사용하므로 로컬 결과와 CI 결과를 구분해 기록한다.
- `ruff`, `pyright`, `bandit`, `pip-audit`가 전용 환경에 없으면 임의로 저장소에 추가하지 말고 설치 명령과 미설치 상태를 보고한다.

## 자주 쓰는 검증 명령

```bash
pip install -r requirements.txt
python -m unittest discover -s tests/schema -p "test_*.py" -v
python departments/02-trading/contracts/contracts.py
python departments/02-trading/oms/oms.py
python departments/02-trading/broker/paper_broker.py
python departments/05-accounting-portfolio/ledger/ledger.py
python departments/05-accounting-portfolio/reconciliation/reconciliation.py
python apps/api/main.py
```

필요한 외부 키가 있는 경우에만 아래 명령을 실행한다.

```bash
python3 skills/agentic-rag/main.py --persona compliance-policy-agent \
  --query "Can we open a new long position in SYMBOL_A today?" --as-of 2026-07-29
python3 departments/01-research/collectors/collector_scheduler.py --check
python3 departments/01-research/api/external_sources.py
```

## 문서 동기화

`CLAUDE.md`는 저장소의 상세 작업 지침을, 이 파일은 Codex가 따라야 할 진입점과 공유 규칙을 담당한다. 공통 정책을 변경할 때는 두 도구가 동일하게 이해할 수 있도록 관련 문서를 함께 갱신한다.
