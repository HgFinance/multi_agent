# ADR-0003: `CalibrationGuidelineV1` 저장·회수 구현 — LDM-v1 (Ledger-Derived Memory)

> **Status: Proposed — 아직 채택되지 않음. 팀 검토 대기.**
>
> - 작성: 재일 (리서치본부 / 퀀트·백테스트본부)
> - 작성일: 2026-08-03
> - **상위 기준**: [RESEARCH_QUANT_AGENTIC_FRAMEWORK.md](../RESEARCH_QUANT_AGENTIC_FRAMEWORK.md)
>   8절(Calibration과 자기 개선), 8.2절(`CalibrationGuidelineV1`), Phase RQF-4.
>   **이 ADR은 그 프레임워크를 대체하지 않는다.** 8.2절이 정한 계약을 *어떤 저장·회수 구조로
>   구현할 것인가* 하나만 다룬다. 계약 필드와 승격 절차는 프레임워크가 이긴다.
> - 제안 배경: "부서 에이전트들 메모리는 계층적 메모리 사용하고 있어?" → 실측 결과 **저장된 기억 0건**
> - 근거: 지반 조사 4건 + 독립 설계안 3건 + 적대적 심사 12건(PIT·결정론·권한·운영 렌즈) + 실측 재확인 8건
> - 최종 결정권자: 재일. 부서 경계와 승격 경로는 동규님(QA)·영주님(HR) 리뷰 필요
> - 관련: [ADR-0002](0002-per-department-redis-instances.md),
>   [TECH_STACK_DECISIONS.md](../TECH_STACK_DECISIONS.md),
>   [HEDGE_FUND_MASTER_PLAN.md 5.11절](../../HEDGE_FUND_MASTER_PLAN.md),
>   [TEAM_JAEIL 가이드 12.2.1](../../05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md)

---

## 0. 프레임워크와의 관계 — 무엇이 이기는가

`RESEARCH_QUANT_AGENTIC_FRAMEWORK.md`가 리서치·퀀트의 **단일 구현 기준**이다(마스터플랜 5.11절이 그렇게 지정했다). 이 ADR을 쓰는 동안 그 문서가 갱신됐고, 아래 세 가지는 **프레임워크를 따르고 이 ADR의 초안을 폐기한다.**

| 항목 | 이 ADR 초안 (폐기) | 프레임워크 (채택) |
|---|---|---|
| 지침 계약 필드 | 자체 설계(`scope_kind`/`condition`/`adjustment` …) | **8.2절 `CalibrationGuidelineV1`** — `candidate_id`, `scope`, `failure_pattern`, `supporting_case_ids`, `minimum_cases`, `proposed_change`, `baseline_eval_id`, `challenger_eval_id`, `heldout_delta`, `expires_at`, `status` |
| 망각 기준 | "180일 무갱신 시 RETIRED" | **`expires_at` 명시 만료** — 지침이 스스로 수명을 들고 있는 쪽이 낫다 |
| 최소 표본 | `n ≥ 30` (내가 정함) | **`minimum_cases: 30`** — 같은 값이지만 계약 필드로 들어간다 |
| 우선순위 | "1단계로 지금 착수" | **Phase RQF-4 / P1.** 12절 "지금 바로 구현할 것"은 V2 계약·PIT fail-closed·Claim/Evidence Graph·Branch/Fan-in·Lineage·Quant Worker다 |

**따라서 이 ADR의 8절 구현 순서는 RQF-4에 편입되며, 지금 착수하는 것은 RQF-0이다.** 다만 아래 1~7절의 실측과 PIT 기제는 그대로 유효하고, 특히 RQF-0 완료 기준("`as_known_at` 미지원 Tool이 과거 Replay에서 호출되면 Fail-closed")이 이 ADR 9절 1번(백테스트 원리적 불가)과 **같은 문제를 가리킨다.**

---

## 1. 한 문장 결론

**새 메모리 저장소를 만들지 않는다. 기억은 `research` 스키마 원장(`packet_claims` → `claim_outcomes`)에서 `as_of`를 필수 인자로 받는 SQL 함수가 매번 재계산하는 파생값이며, 1단계에서는 그 기억을 아무도 읽지 않는다 — 읽을 표본이 아직 없기 때문이다. 1단계의 일은 표본 생산 Job을 만들고 계측을 복구해, 오염되지 않은 표본이 오늘부터 쌓이게 하는 것이다.**

## 2. 실측 — 지금 기억이 몇 건인가

**0건이다.** 컨테이너 안에서 센 값이다.

| 확인 대상 | 실측 (2026-08-03) |
|---|---|
| `/opt/data/memories` (설치본 전역) | 디렉터리 존재, **파일 0개** |
| `/opt/data/profiles/research-department/memories` | 디렉터리 존재, **파일 0개** (퀀트도 동일) |
| Hermes 설정 | `memory_enabled: true`, `flush_min_turns: 6` — 켜져 있는데 단발 호출이라 못 채운다 |
| 분석가 6인(LangGraph) | 메모리 코드 **없음**. 매 실행 무상태 |
| `analyst_calibration` 읽는 곳 | `api/mcp_server.py`의 MCP 도구 하나뿐. **분석가는 MCP를 호출하지 않는다** |
| **누적된 Packet** | `reports/`에 **16건, 전부 000660, 3일치** |
| **Packet 생성 Job** | `collector_scheduler.py` JOBS 25개 중 **0개** (채점기만 있다) |

마지막 두 줄이 이 ADR의 순서를 강제한다. **표본이 없는 상태에서 읽기면부터 만드는 것은 순서가 거꾸로다.**

## 3. 외부 프레임워크 판정 — 전량 도입 불가, 신규 의존성 0개

2026년 에이전트 메모리 생태계는 working / episodic / semantic / procedural 4분류로 수렴했고 mem0(약 48k stars)·Letta(MemGPT)·Zep·LangMem이 그 계층을 제공한다. 전부 탈락한다.

| 프레임워크 | 탈락 사유 | 우리 쪽 실측 근거 |
|---|---|---|
| mem0 / Letta / LangMem | **쓰기 주체가 LLM이다** — 무엇을 기억할지, 기존 기억을 어떻게 고칠지를 LLM이 판정 | 우리는 이 규율을 주석이 아니라 DB CHECK로 못박았다: `packet_claims.origin check (origin in ('code'))` |
| 전부 | **갱신이 파괴적이다** — 현재값이 과거값을 덮는다 | 백테스트 재현 불가. 우리 노선은 `financial_facts.revision` / `macro_observations.vintage_date`(원본 미갱신, 새 revision) |
| Zep / Graphiti | bi-temporal은 유일하게 진짜지만 **`valid_at`을 LLM이 텍스트에서 추출**한다 | 우리 PIT 축은 수집 파이프라인이 관측한 시각(`documents.observed_at`). 환각이 곧 lookahead가 된다. Neo4j 필수 = Core 스택 아님 |
| LangGraph `BaseStore` / `PostgresStore` | 백엔드는 통과하나 **`put()`이 값을 덮고 조회에 `as_of` 인자가 없다** | 클라이언트 필터는 `skills/agentic-rag/src/nodes.py:248`의 실패 패턴 재현 — top_k로 먼저 자르고 PIT로 걸러 결과가 조용히 줄어든다 |
| 전부 | **제거 기준을 쓸 수 없다** — 기억이 프레임워크 소유 스토어에 갇힌다 | 개발원칙 8. LDM은 함수를 drop하면 끝이고 원장은 남는다 = **제거 비용 0** |

`langgraph-checkpoint-postgres`도 채택하지 않는다. 실측된 진짜 실패는 총괄 `draft_packet` 스키마 이탈(19회 중 10회)이고, 그건 `out`을 JSON 덤프하고 `draft_packet`만 다시 부르면 의존성 0으로 해결된다. 더 싼 대안을 배제하지 않은 도입은 개발원칙 8을 못 채운다.

**대신 두 문헌의 규율만 가져온다.**

- **SSGM** (*Governing Evolving Memory in LLM Agents*, arXiv:2603.11768) — 승격 게이트 / 검증 / 감쇠 / 출처 추적
- **Nexus** (*An Agentic Framework for Time Series Forecasting*, `references/2605.14389v1.pdf`) — 보정 루프

### 3.1 Nexus에서 가져오는 것

Nexus는 예측을 다섯 역할로 쪼갠다(과거 맥락 / 거시 / 미시 / 합성 / **보정**). 앞의 넷은 우리 분석가 구조와 이미 겹친다. 없는 것이 다섯 번째다.

> 보정 에이전트는 모델 파라미터를 재학습하지 않는다. … **파인튜닝 기반 학습이 아니라 검증된 규칙의 누적**이다.
> — `references/NEXUS_FRAMEWORK_EXPLAINED.md` 4.5절

그 승격 절차(같은 문서 5절)가 우리 PIT 규율과 설계상 호환된다: 시간순 구간 분할 → **당시 알 수 있었던 데이터만** 사용 → 실제값 대조 → 반복 오차를 규칙화 → **한 구간에만 나타난 규칙 제거** → 홀드아웃에서 개선 확인 → 기준 이상일 때만 승격.

우리 조정:

| Nexus 원형 | 우리 조정 | 이유 |
|---|---|---|
| 5% 이상 개선 시 승격 | **shadow Brier < baseline Brier + 부트스트랩 95% CI가 0 미포함** | 표본이 작다. 고정 비율은 우연을 학습으로 착각하게 한다 |
| 단일 경로 예측 | **확률 예측**(`packet_claims.probability` 기존) | 논문 한계 명시: "확률 분포보다 단일 경로에 초점" |
| 보정 지침이 프롬프트 메모리 | **기억은 어떤 narrate 프롬프트에도 들어가지 않는다** | 우리 `verify()` 시그니처가 `(note, readout)` 둘뿐이라 "기억을 봤는지" 알 방법이 없고, 정확히 인용한 수치가 전부 환각으로 flag된다(실측 사고 있음) |
| 교집합으로 희귀 규칙 제거 | 제거하지 않고 격리 보관 | 논문 한계 명시: "새 국면에 필요한 희귀 규칙을 제거할 수 있다" |

## 4. 구조

### 4.1 기억의 층

| 층 | 실체 | 저장 위치 | 읽기 시점 | LLM 도달 |
|---|---|---|---|---|
| **L0 원자** | 채점된 주장 1건 | `packet_claims` ⋈ **`claim_outcomes`(신규)** | — | ✗ 절대 |
| **L1 사실** | as_of 컷오프로 집계한 1행. **DB에 저장되지 않는다** | 없음 — `api.method_prior()` 반환값 | `load_memory` 노드(2단계) | ✗ |
| **L2 표시** | 코드 템플릿이 만든 리포트 각주 | `packet["_memory_footnote"]` (Packet 본문 **밖**) | 리포트 렌더 | ✗ |
| **L3 절차** | "무슨 기법을 왜 쓰나" | `evidence/methods.py` 파일 자체 | import 시 (git 커밋 = PIT 게이트) | ✗ |

**semantic memory 층을 만들지 않는다.** "삼성전자는 반도체 기업" 같은 사실은 `reference.instruments`가 이미 갖고 있고 이미 시점 관리된다. 층을 위한 층은 만들지 않는다.

### 4.2 기억의 종류 — Grade 분리가 핵심

| 종류 | Grade | 키 (**symbol 금지**) | 허용 용도 |
|---|---|---|---|
| **M2 METHOD_PRIOR** | **A** (자기 주장 채점) | `(probability_method, kind, horizon_days)` | 3단계 shadow 재보정 |
| **M1 ANALYST_PRIOR** | **B** (판정 동시발생) | `(node, verdict, horizon_days)` | **리포트 각주·MCP 조회만. 숫자 보정 영구 금지** |

M1이 Grade B인 이유가 실측으로 확정됐다. `analyst_calibration`의 키 도메인은 `analyst_verdicts`의 6인 노드명인데, 확률이 붙은 주장의 `source_node`는 `price_context`다 — **교집합이 공집합이다.** 게다가 M1은 P(20일 수익률>0) 방향 통계이고 채점 대상은 P(배리어 터치) 변동성 통계다. 단위가 다르다.

**기억의 단위는 종목이 아니다.** 종목 단위 기억은 그 자체로 look-ahead다. 기억하는 것은 "이 방법론이 이 지평에서 확률을 과대평가한다" 같은 습관이다.

## 5. PIT 보장 기제

1. **읽기는 `as_of`를 필수 인자로 받는 `security definer` 함수로만.** 기본값을 주지 않는다. 선례는 `api.match_evidence_chunks`, 반례는 `/evidence/search`(as_of 선택 → 미지정 시 필터가 통째로 사라진다).
2. **`knowable_at` = 지평 마지막 거래일 15:30 KST + 1거래일.** `evaluated_at`을 쓰면 안 된다 — `packet_outcome_scorer.py:302`가 `on conflict do update set evaluated_at = now()`이고 PENDING 행이 매일 재선택돼 upsert가 반복된다. 그 열은 세계의 시각이 아니라 배치가 마지막으로 건드린 시각이다.
3. **쓰기 경로에 `as_of`를 넣지 않는다.** 세 설계안이 모두 제안했고 모두 틀렸다 — `evidence/bundle.py:142`에 `as_of` 인자가 없고 `market_api`에서 시간 컷오프가 있는 엔드포인트는 `/bars`의 `to` 하나뿐이다(`/breadth`·`/regime/daily`·`/microstructure`·`/snapshot` 전부 없음). 지금 `as_known_at = started`는 **참**이다(실행 시각 = 데이터 컷오프). as_of를 넣으면 그 항등식이 깨지면서 원장에 거짓 행이 append-only로 박힌다.
4. **`packet_hash` 재현 비교를 PIT 반증 장치로 쓰지 않는다.** `scripts.py:747`이 `uuid4()` trace_id를 packet에 넣고 `:676`이 그 packet 전체를 sha256한다 — 오탐률 100%. 대신 `deterministic_hash`(trace_id·벽시계 제외)를 따로 만든다.

## 6. 저장 금지 목록

| 저장하지 않는 것 | 이유 |
|---|---|
| 뉴스·공시 **원문 전문** | 라이선스(`UseScope`에 FULLTEXT_STORE 없는 소스) |
| LLM 서술 문장 그대로 | 앵커링. 기억은 **숫자와 조건**으로만 |
| 다른 부서의 판단 | 마스터플랜 572행. 전사 공유는 ImprovementCandidate 경로 |
| **개별 종목의 "지난번 결과"** | 종목 단위 기억은 곧 look-ahead |
| 게이트 미달 후보 | 보관은 하되 **읽기 함수가 반환하지 않는다**(fail-closed) |
| MCP 응답의 `pct_positive`·`avg_forward_return_pct` | 헤르메스가 정성 서술로 되뇌면 그것이 유출이다. `n` + `as_of` + 포인터만 |

## 7. 망각 정책

`document_revisions`와 같은 원칙 — **행을 삭제하지 않고 상태만 바꾼다.**

| 조건 | 조치 |
|---|---|
| 근거 데이터 마지막 관측이 180일 이상 지남 | 회수 대상에서 제외(국면이 바뀌었을 수 있다) |
| 같은 셀에 모순되는 결과 | 둘 다 후보로 강등 후 재평가. **조용히 최신 것을 이기게 하지 않는다** |
| 게이트 미달 | 삭제하지 않고 보관 — 새 국면에서 재평가 대상 |

## 8. 구현 순서

### 1단계 — 표본 생산 + 계측 복구 (읽기면 0개)

| # | 파일 | 변경 |
|---|---|---|
| **1.1** | `collectors/collector_scheduler.py` | **`Job("research-packet", …, daily_at=16:40)` 추가** + `scripts.py --run-universe`. **이게 없으면 2·3단계는 0행 위에서 정직하게 UNAVAILABLE을 반환하는 코드다** |
| 1.2 | `supabase/migrations/…_research_claim_outcomes.sql` | `research.claim_outcomes` + `run_mode` + `deterministic_hash` + 기존 캘리브레이션 뷰에 "백테스트 금지" comment |
| 1.3 | `collectors/packet_outcome_scorer.py` | `results` **전량 + unscored 라벨**을 3상태(HIT/MISS/**UNRESOLVED**)로 반환. 지금 `triggered` jsonb는 발동분만 담아 미발동/미채점을 원리적으로 구분 못 하고, 채점 보류가 예측 실패로 위조된다 |
| 1.4 | `collectors/label_snapshot_collector.py` | `do update set … computed_at = now()` → revision append. 16:30 단발 잡이 부분 데이터로 라벨을 굳히면 영구히 틀린다 |
| 1.5 | `scripts.py` | `deterministic_hash` 적재, `run_mode='LIVE'` |
| 1.6 | `scripts.py:596-611` | **코드 변경 없음.** `c["method_key"] = method`는 버그가 아니라는 주석 + 자체점검 — `by_node`에 `price_context`가 없어 가격 주장은 분석가 귀속을 **가진 적이 없고** 이 줄이 유일한 귀속 공급원이다. 지우면 `method_calibration`이 확률 있는 행을 전부 잃는다. **세 설계안이 여기서 같이 틀렸다** |
| 1.7 | `tests/schema/test_schema_contract.py` | 신규 마이그레이션 등록 + research 테이블 수 갱신 |

**1단계만 끝나도 나오는 값**
1. `method_calibration.brier_score`가 처음으로 **측정값**이 된다. 지금은 개별 주장 확률을 `(claims_triggered > 0)` OR 묶음과 대조해 체계적으로 부풀어 있다 — 잡음이지 측정이 아니다.
2. 표본이 오늘부터 하루 N종목씩 쌓인다. **소급 불가 자산이다.**
3. 채점 보류가 예측 실패로 위조되던 것이 멈춘다.

### 2단계 — 회수면 (0행에서도 정직하게 동작)

`api.method_prior` / `api.analyst_prior`(as_of 필수) → `GET /memory/*`(Tool Gateway scope 등록) → `memory/recall.py`(persona 필수 인자) → `scripts.py`의 `load_memory` 노드 → **Packet 본문 밖 각주**. MCP `analyst_calibration()`도 이 함수 경유로 바꾸고 성과 수치를 응답에서 뺀다. `as_of`는 서버가 주입하고 **도구 인자로 노출하지 않는다** — LLM이 PIT 컷오프를 고르면 제약 위반이다.

### 3단계 — shadow 재보정 (게이트 4개)

`probability`(운영값)를 건드리지 않고 `probability_shadow` 열에만 쓴다. 같은 행에 있으므로 Brier 비교가 같은 표본에서 성립한다. 승격 조건: ① `n_eff ≥ 100`, `distinct_symbols ≥ 20`, `max_symbol_share ≤ 0.15` ② shadow Brier < baseline Brier이고 부트스트랩 95% CI가 0 미포함 ③ `methods.py`에 인용과 함께 등재 ④ **QA(동규) 검증 경유** — 문장이 아니라 `governance.approvals`의 FK로. 마스터플랜 590-596: 자기 산출물의 Production 승인 단독 수행 금지.

## 9. 이 채택안이 해결하지 못한 것 (숨기지 않는다)

1. **백테스트가 원리적으로 불가능하다.** `market_api`의 `/breadth`·`/regime/daily`·`/microstructure`·`/snapshot`에 시간 컷오프가 없고 `market_bars`에 vintage가 없어 나중에 백필된 봉이 과거 조회에 섞인다. LDM은 **기억 상태만** 되돌릴 수 있고 **판단은 되돌릴 수 없다.** market-api as_of 관통은 별도 과제이며 그 비용은 이 안에 없다.
2. **표본 독립성 게이트는 편중을 완화할 뿐 제거하지 못한다.** PRICE_DRAWDOWN은 시장 베타로 같은 날 동시 발동한다 — 급락일 100종목이면 독립정보는 1에 가까운데 `n_eff`는 100이다. 날짜 클러스터 보정이 v1에 없다.
3. **M1은 분석가가 아니라 시장 베타를 잰다.** 6인이 같은 Packet을 공유하므로 "BULLISH일 때 60% 상승"이 "시장이 60% 상승"과 구분되지 않는다. 초과수익 전환이 v1에 없다.
4. **Hermes 전역 메모리(`/opt/data/memories`)는 리서치 권한 밖이다.** 마스터플랜 308행 "고유한 Memory Namespace"와 충돌하는 상태다. 값 유출은 막지만 정성 서술은 못 막는다.
5. **RLS는 실질 경계가 아니다.** 모든 접근이 `DATABASE_URL` psycopg2 직결이고 그 DSN은 `postgres` 롤이다. `enable row level security`는 아무것도 막지 않으면서 경계가 생겼다는 착시를 만든다. 실제 경계는 `DATABASE_URL` 배포 통제뿐이며, 그래서 이 문서는 "구조적으로 불가능하다"는 표현을 쓰지 않는다.
6. **`claim_evidence` 인용 축이 원리적으로 닫혀 있다.** 현재 발행되는 주장 4종 중 문서를 소비하는 것이 하나도 없고, 문서를 소비하는 분석가(RES-05·06)는 주장을 발행하지 않는다. "어떤 소스에 기댄 판단이 틀렸나" — 기억의 가장 값진 축 — 이 v1에서 열리지 않는다.
7. **`ImprovementCandidate`가 이 산출물을 못 받는다.** `target_type` CHECK가 `('SKILL','PROFILE','WORKFLOW','AGENT')`뿐이라 METHOD/MEMORY 후보 제출 수단이 없다. 3단계 게이트 ④가 성립하려면 영주님 소유 workforce 스키마를 넓혀야 하고, **리서치가 그 마이그레이션을 직접 쓰는 것 자체가 권한 침범이다.**
8. **pytest 스위트가 없다.** 자체점검이 각 모듈 `__main__`뿐이라 CI 게이트가 아니다.

## 10. 반증 조건

발행 시점에 고정한다. `falsification_note`가 주장에 하는 것과 같은 규율이다.

| # | 반증 조건 | 반증 시 조치 |
|---|---|---|
| **F1** | `api.method_prior(과거 as_of)`를 30·60·90일 간격으로 재호출해 결과 해시가 **1건이라도 불일치** | **즉시 회수 중단.** 미래 정보 유입은 협상 대상이 아니다 |
| **F2** | 원장 테이블 직접 SELECT가 허용 모듈 밖에 존재 | 경로 제거 또는 설계 폐기 |
| **F3** | `run_research_department` 시그니처에 `as_of`가 생김 | 해당 커밋 반려 |
| **F4** | 1단계 배포 +90일에 회수 함수 행 수가 **0** | Job 빈도·유니버스 오류. **지평은 줄이지 않는다** — 주장 난이도가 바뀌면 이전 표본과 못 섞인다. 유니버스만 늘린다 |
| **F5** | `n_eff ≥ 100`에서 shadow Brier가 baseline을 못 이김 | **기억을 켜지 않는다.** 각주로만 남긴다. 이것도 정당한 결론이다 |
| **F6** | `UNRESOLVED / total` > 0.3 유지 | 라벨·시세 커버리지 문제. 이 비율을 회수 응답에 **항상 함께 반환**한다 — 분모를 숨기면 통계가 아니다 |
| **F8** | 각주의 기억 수치가 `verify_narrative_numbers`의 `unmatched`에 등장 | 총괄이 각주를 산문에 옮겨 쓴 것. 각주를 본문 밖으로 완전 격리 |
| **F9** | 같은 종목·같은 날 2회 실행에서 `deterministic_hash` 불일치 | 어느 경로가 벽시계를 보는지 찾을 때까지 **2단계 착수 금지** |

## 11. 다음 행동

- [ ] 동규님(QA) 리뷰 — 3단계 승격 게이트가 QA 독립검증 권한을 침범하지 않는지
- [ ] 영주님(HR) 리뷰 — `improvement_candidates.target_type` 확장 필요(9절 7번). **리서치가 직접 쓰지 않는다**
- [ ] 도현님 — 9절 1번(market-api as_of 관통)이 공통 Platform 과제인지 판단
- [ ] 승인 시 `TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md`에 `RQF-MEM-01~03` 추가
- [ ] 반려 시 반려 사유만 추가하고 Status를 `Rejected`로 변경(삭제하지 않음)
