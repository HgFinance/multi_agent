# 문헌 기반 돌파 지도 — 서비스 질 개선 우선순위 분석

> **작성일**: 2026-08-13 · **기준**: [AS_IS_PIPELINE_BLUEPRINT.md](AS_IS_PIPELINE_BLUEPRINT.md) (HEAD `892973c`)
> **방법론**: 전문 문헌 4개 영역(백테스트 타당성 / 멀티에이전트 LLM 신뢰성 / 트레이딩 안전·규제 / 이벤트 인프라·관측성)을 병렬 조사. 검증 소스 60+ 편 — 학술 논문(피어리뷰·고인용 워킹페이퍼), 규제 원문(규칙 번호까지), 업계 표준, 공식 문서, 사후 분석. 모든 소스는 웹 검색·원문 대조로 실재를 확인했고 핵심 문서는 직접 fetch로 주장을 검증했다. 날조된 인용 없음.
> **판단 기준**: 순위 = (문헌 지지 강도 × 서비스 질 영향) ÷ 노력. 단계 태그는 서비스 목표 로드맵(전략 1호 → PAPER 전 부서 검증 → 사용자 질의, 로컬 달성 후 AWS) 기준.

---

## 목차

1. [한 장 요약과 우선순위 보드](#1-한-장-요약과-우선순위-보드)
2. [돌파 지점 12개 상세 — 현재 상태 · 문헌 근거 · 처방](#2-돌파-지점-12개-상세)
3. [영역 1 조사 전문 — 백테스트 타당성·전략 공장](#3-영역-1--백테스트-타당성전략-공장)
4. [영역 2 조사 전문 — 멀티에이전트 LLM 신뢰성](#4-영역-2--멀티에이전트-llm-신뢰성)
5. [영역 3 조사 전문 — 트레이딩 안전장치·규제](#5-영역-3--트레이딩-안전장치규제)
6. [영역 4 조사 전문 — 이벤트 인프라·관측성](#6-영역-4--이벤트-인프라관측성)
7. [영역 교차 합의 지점](#7-영역-교차-합의-지점)
8. [기존 references.md와의 차분 + 추가 독서 목록](#8-기존-referencesmd와의-차분)
9. [로드맵 단계 매핑](#9-로드맵-단계-매핑)

---

## 1. 한 장 요약과 우선순위 보드

**결론.** 조사한 문헌 전체가 이 시스템의 결정론 골격 — supervisor 상태기계, 결정론 게이트(Gate 0 / RiskEngine / release gate / publish gate), MCP 도구 표면 강제, 복식부기 원장, UNKNOWN 주문 차단 — 을 **정답으로 판정**한다. 재설계할 것이 없다. 격차는 전부 **신뢰 경계를 검증 없이 건너는 4곳**에 집중된다:

| # | 신뢰 경계 | 무엇이 검증 없이 통과하는가 | 해당 돌파 지점 |
|---|---|---|---|
| ① | LLM 출력 → 실행 | CEO 팬아웃 계획, QA 판정 정규화, 인용 없는 합성 | 3, 5 |
| ② | 안전 상태 → 기본값 | 킬스위치 키 부재=ENABLED, QA 미인식=WARN, 프리필터 예외=통과 | 2, 3 |
| ③ | 연구 입력 → 판정 | 미구속 사전등록 지문, 고정 비용 모델, LLM 훈련 오염 가설 | 1, 4, 6, 7 |
| ④ | 계측 → 아무도 안 봄 | 수집기 없는 Prometheus, SENT 마킹 후 소실, 풀 고갈 | 8, 9, 11 |

처방의 공통 문법은 **"경계에 울타리 치기"**이고, 상당수는 신규 개발이 아니라 잠자는 부품(eval_runner, Prometheus 계측, opt-in 플래너, 원장)의 배선이다.

### 우선순위 보드

| 순위 | 돌파 지점 | 단계 | 노력 | 핵심 근거 |
|---:|---|---|---|---|
| **1** | 사전등록 지문 완전 구속 | 공장 | 하 | Arnott-Harvey-Markowitz 2019 · López de Prado 프로토콜 |
| **2** | 킬스위치 fail-safe 반전 | 실주문 전 | 하 | MiFID II RTS 6 Art.12 · PRA SS5/18 · CME/Nasdaq/KRX |
| **3** | QA 판정 fail-closed | 질의 | 하 | MT-Bench · τ-bench · AgentRewardBench |
| **4** | 비용 모델 현실화 (세율 스케줄·오목 임팩트·밴드) | 공장 | 중 | FSC/PwC 실측 · Almgren-Chriss · Novy-Marx-Velikov |
| **5** | CEO 팬아웃 가드 (generate-then-verify) | 질의 | 중 | LLM-Modulo(ICML 2024) · MAST · Anthropic 2025 |
| **6** | 다중검정 회계 강화 (공장 전체 N·테마 패밀리·PBO≤0.2) | 공장 | 중 | Bailey-LdP 2014/2017 · Harvey-Liu-Zhu · JKP 2023 |
| **7** | LLM 훈련 오염 방어 | 공장 | 중 | Sarkar-Vafa(ICML 2025) · ChronoBERT · AlphaAgent(KDD 2025) |
| **8** | outbox 내구성 복원 (멱등 분개·재전송 스윕·AOF) | 기반 | 중 | Richardson · Kleppmann DDIA · Redis 공식 문서 |
| **9** | 관측성 최소 스택 (Prometheus + 알림 4개) | 기반 | 중 | Google SRE Ch.6 · Hidalgo SLO · Monte Carlo |
| **10** | EW/VW 로버스트니스 레그 + 신호 블렌딩 | 공장 | 상 | Hou-Xue-Zhang · Grinold-Kahn · Clarke et al. |
| **11** | 구성 드리프트 제거 (.env·스위치보드·환경 패리티) | 기반 | 하 | 12-Factor · SEC 34-70694(Knight) · OWASP |
| **12** | 자동 트립 + 실시간 사후 대사 | 실주문 전 | 상 | Nasdaq NNRE · RTS 6 Art.15–17 · Knight 사후 분석 |

**순위 논리**: 1–3위는 노력이 몇 줄~며칠 수준인데 문헌 지지가 만장일치인 지점. 4–7위는 지금 유일하게 가치가 흐르는 공장 루프의 통계적 신뢰성 — 전략 1호가 진짜인지 가짜인지를 가르는 문제. 8–9·11위는 기반 배관. 10위는 노력이 크지만 IR 벽 이후의 성과 상한을 결정. 12위는 D1–D5(주문 경로) 배선 착수의 **선행 관문** — 규제 문헌의 일치 판정은 "사전 게이트만으로는 통제 시스템의 절반"이다.

---

## 2. 돌파 지점 12개 상세

### 2-1. 사전등록 지문 완전 구속 — 공장 · 노력 하

**현재 상태** (AS-IS §6.9, §12.3): `experiment_orchestrator.py:519-520`이 `universe_version='krx-basket-daily/v2'`를 하드코딩한 채 실제 실행은 v3 데이터셋(2016–2026, 3,924종목)으로 돈다. 비용 모델 태그도 러너·PBO는 `krx-cost-v2`, 워크포워드·IC insert는 `krx-cost-v1`로 갈라져 있다. **지문이 실데이터셋과 비용 전제를 구속하지 않는다.**

**문헌이 말하는 것**: Arnott-Harvey-Markowitz(2019)의 7범주 프로토콜에서 "표본·유니버스·제외 규칙·비용을 사전에 동결하고 결과를 본 후 변경 금지"는 협상 불가능한 핵심 항목이다. López de Prado의 인과 팩터 프로토콜(2025)은 여기에 인과 그래프와 반증 검정 선언을 추가한다. 지문의 존재 이유는 9단계 검증(`LEAKAGE_SUSPECT`)인데, **해시 밖에 있는 것은 전부 조용히 가변**이므로 현재의 지문은 프로토콜 요건을 형식만 충족한다 — 데이터셋을 바꿔치기해도 fingerprint 검증을 통과한다.

**처방** — 지문 해시에 다음 필드를 전부 포함:

```
material_fingerprint = sha256(
    dataset_snapshot_id,        # 현재 누락 — v2 하드코딩을 실제 해석 결과로 교체
    universe_construction_rule, # "요청 범위에 bar가 존재하는 전부" 선언 포함
    cost_model_version,         # v1/v2 혼재 해소가 선행 조건 (2-4와 연결)
    template_code_version,      # 이미 있음 (러너 파일 해시)
    trial_family_id,            # 이미 있음
    scout_model_id + training_cutoff,  # 신규 — 2-7과 연결
    config, seed                # 이미 있음
)
```

기대 효과: 하류의 DSR·PBO·부트스트랩 장치 전체가 "연극"에서 실제 통제로 복원. 수리 범위는 `experiment_orchestrator.py`의 지문 조립부 한 곳.

### 2-2. 킬스위치 fail-safe 반전 — 실주문 전 · 노력 하

**현재 상태** (AS-IS §8.2 + §11.1 조합): 킬스위치 상태는 Redis 저장(TTL 없음), **키 부재 = ENABLED**, Redis는 AOF 없음. 두 사실을 합치면 **Redis 재시작 한 번에 HALTED가 조용히 ENABLED로 부활**한다. 읽기 실패는 fail-closed(HALTED)로 옳게 설계했으나 키 소실은 fail-open이다. 게다가 `.env` last-wins 이중 정의로 실제 Redis가 로컬인지 Redis Cloud인지도 모호하다. 쓰기 경로는 HS256 토큰이 있으나 자동 트립 코드는 없다.

**문헌이 말하는 것**: 조사한 **모든** 레퍼런스 구현과 감독 문서가 "killed가 끈적한(sticky) 기본 상태, 재가동이 명시적 인증 행위"로 설계한다:

- **MiFID II RTS 6 Art.12**: 긴급 조치로서 미체결 주문 전량 즉시 취소 — 알고리즘·트레이더·데스크 단위 선택 가능해야 하며, 모든 주문의 책임 알고리즘·트레이더 식별 의무.
- **PRA SS5/18**: "거래를 정지하거나 접근을 차단하는 수동·자동 통제 + **재가동에는 수동 개입 필요**" + 활성화 시 미체결·기제출 주문 처리 방침 정의 + 주기적 작동 시험.
- **CME Kill Switch**: Legal Clearing Entity / Execution Firm / SenderComp 계층에서 차단+취소를 원자적으로, 상위 킬은 하위로 캐스케이드. 재가동은 회원사의 명시적 조작.
- **Nasdaq Equity Kill Switch**(SEC 34-71555 승인): 사전 설정 Net Notional Risk Exposure 캡 위반 시 **자동으로** 포트 차단+미체결 취소.
- **KRX**: 2013 HanMag 사고 후 호가 일괄취소 제도(회원 요청 일괄취소+추가 호가 차단, 2016 증권시장 확대) + 접속 단절 시 취소(cancel-on-disconnect).

NAV 엔진은 이미 fail-closed(신선한 마크 없으면 `ValuationError`)인데 킬스위치만 fail-open인 것은 같은 시스템 안의 원칙 비일관이기도 하다.

**처방**: ① 권위 저장소를 Postgres(`risk.trading_states` 또는 `kill_switch_events` 확장)로 이동, Redis는 캐시로 강등. ② 키 부재/판독 불능/모호 = **HALTED**로 해소. ③ 상태 전환 전부 이벤트 로깅(누가·언제·왜). ④ 재가동은 인증된 수동 조작만(기존 HS256 스코프 토큰 재활용). 수리 규모는 `risk_engine.py`의 읽기 경로 수 줄 + 저장 계층 이동.

### 2-3. QA 판정 fail-closed — 질의 · 노력 하

**현재 상태** (AS-IS §9.3): `qa_audit_projection.py`의 판정 정규화 — `CONDITIONAL PASS|WARN|ESCALATE → WARN`, `FAIL|REJECT|BLOCK → FAIL`, **미인식 → WARN**. 미인식 판정이 절대 PASS는 아니지만 소프트패스(WARN)로 흘러간다.

**문헌이 말하는 것**: LLM 판정자 문헌의 일치된 전제는 **"판독 불능 판정 = 판정자의 실패"**이며 절대 유효 판정으로 취급하지 않는다.

- Zheng et al., MT-Bench(NeurIPS 2023): 강한 판정자도 위치 편향(먼저 제시된 답 선호), 장황 편향(긴 답 선호), **자기선호 편향**(자기 계열 출력 선호) 실증. 완화책은 위치 스왑 후 일치 요구, 참조 기반 루브릭.
- τ-bench(2024): ground truth가 있는 곳은 LLM 판정이 아니라 **결정론적 최종 상태 비교**(DB 상태 vs 목표 상태)로 평가. GPT-4o급 에이전트도 태스크 해결 <50%, **pass^8 < 25%** — 단일 실행 성공률은 프로덕션 신뢰성을 크게 과대평가.
- AgentRewardBench(2025): 1,302개 전문가 주석 궤적 × 12 판정자 — 어떤 LLM 판정자도 전 영역에서 신뢰 불가, 그러나 순수 규칙 평가는 성공을 과소 보고. **규칙+판정자 층위 구조**가 결론.

**처방**: ① 미인식 판정 → 위치 스왑 재판정 1회 → 실패 시 **격상(escalate), 절대 WARN 아님**. ② LLM 판정 전에 결정론 검사 층 선행: 스키마 유효성, 인용 해소(참조 문서 실재), 산출물 존재. ③ 판정 모델이 부서장과 같은 GPT 계열이면 자기선호 편향이 실측돼 있으므로 참조 기반 루브릭 필수. 수리 지점: `qa_audit_projection.py`의 정규화 표 + QA 카드 프롬프트.

### 2-4. 비용 모델 현실화 — 공장 · 노력 중

**현재 상태** (AS-IS §6.6): `krx-cost-v2` — 수수료 1.5bps, **매도세 15bps 고정**, 슬리피지 7.2bps(자사 호가 2백만 표본 하프스프레드 중앙값). 유동성 티어는 있으나 임팩트는 주문 크기와 무관.

**문헌이 말하는 것**:

한국 증권거래세 실측 스케줄 (KOSPI+KOSDAQ 유효 매도세율, PwC Tax Summaries·FSC 검증):

| 기간 | 유효 세율 | 백테스트 영향 |
|---|---:|---|
| ~2019 상반기 | **0.30%** | 현재 모델은 이 구간 비용을 절반으로 과소 계상 |
| 2019 하반기~2020 | ≈0.25% (전환기, 정확 시점 재확인 필요) | 과소 계상 |
| 2021–2022 | 0.23% | 과소 계상 |
| 2023 | 0.20% | 과소 계상 |
| 2024 | 0.18% | 근사 |
| 2025 | **0.15%** | 현행 모델과 일치 |
| **2026-01-01~** | **0.20%** (회귀) | **실전 비용을 25% 과소 청구** |

즉 15bps 고정은 **양방향으로 틀렸다**: 2016–2023 백테스트 순수익률은 상방 편향, 2026 실전 전망은 과소 청구.

- **Almgren & Chriss(2000)** + 후속 실증 합의: 시장 임팩트는 주문 크기에 **오목(√ 법칙)** — 비용은 참여율(주문/ADV)의 함수이지 상수가 아니다. top-300 동일가중의 꼬리 종목은 소형주라 참여율이 비용을 지배한다.
- **Novy-Marx & Velikov(RFS 2016)**: 단면 월회전 ~50% 초과 전략은 비용 후 생존 희박. **최고 완화책은 buy/hold 밴드**(보유 유지 랭크 문턱을 매수 문턱보다 느슨하게). 일별 리밸런스 리버설이 정확히 위험 지대.
- **Frazzini-Israel-Moskowitz(2018)**: ~$1T 실집행 데이터 — 인내심 있는 집행의 실비용은 학술 추정의 ~1/10이나, **단기 리버설이 가장 비용 제약적**(9개 템플릿 중 REV·LIQREV 직격).
- 공매도 전면 금지기(2023-11 ~ 2025-03-30, FSC): 350종목 부분 허용 → 전 종목(~2,700) 재개. long-only에도 별개 미시구조 레짐(과대평가 지속, 리버설 수익 왜곡) — **가장 싼 첫 레짐 분석 대상**.

**처방**: ① `krx-cost-v3` = 날짜별 세율 스케줄(위 표) — 스케줄 자체를 버전·사전등록. ② 슬리피지를 `spread/2 + k·√(주문/ADV)` 오목 곡선으로 — k는 자사 `market_quotes` 실측으로 적합. ③ 전략별 **용량(capacity)** 추정을 판정 산출물에 추가. ④ buy/hold 랭크 밴드를 템플릿 표준 노브로. ⑤ 선행 조건: cost 태그 v1/v2 혼재 수리(§12.3 드리프트). 주의: 레거시 2종 base_config의 바이트 동일성 규칙(과거 input_hash 보존)과의 충돌은 신규 버전 태그로 회피.

### 2-5. CEO 팬아웃 가드 — 질의 · 노력 중

**현재 상태** (AS-IS §5.5): 루트 카드를 부서 카드 N개로 쪼개는 것은 **무방비 LLM 턴**이다. SOUL.md의 계약(부서 allowlist, workflow_role 기입)을 프롬프트로만 요구하고, 읽기 측이 사후 재구성한다. supervisor가 비-canonical assignee를 발견하면 루트를 block하는 사후 방어는 있으나, 계획 자체의 사전 검증은 없다. 단 하나의 예외가 이미 존재: 포트폴리오 파이프라인 전용 opt-in 플래너(`ceo_task_planner.py`)는 allowlist 상한·`{qa, ceo}` 하한 강제 + 실패 시 결정론 폴백.

**문헌이 말하는 것** — 4갈래가 같은 처방으로 수렴:

1. **Kambhampati et al., LLM-Modulo(ICML 2024)**: 자기회귀 LLM은 계획을 신뢰성 있게 생성도 자기검증도 못 한다. 생산적 구조는 LLM이 후보 계획 생성 → 외부 건전 검증기가 검사 → 위반 사유와 함께 재프롬프트 루프.
2. **Cemri et al., MAST(2025)**: 1,600+ 실행 트레이스, 7개 프레임워크, κ=0.88 — 14개 실패 모드가 3범주(사양·시스템 설계 / 에이전트 간 불일치 / 태스크 검증)로 묶이며 **실패는 구조적**(프롬프트 수리는 미미한 개선). 잘못된 태스크 분해가 1범주의 핵심.
3. **Anthropic 멀티에이전트 리서치 시스템(2025)**: 멀티에이전트가 단일 대비 +90.2%(내부 리서치 eval) — 단 오케스트레이터가 서브에이전트별 **명시적 목표·출력 형식·노력 예산**을 주지 않으면 중복·표류. 인용은 전용 검증 패스.
4. **Beurer-Kellner et al., 주입 방어 6패턴(2025)** 중 plan-then-execute: 비신뢰 데이터 섭취 **전에** 계획 토폴로지를 고정 — 검색된 콘텐츠가 카드 내용은 바꿔도 카드 **구조**는 못 바꾸게.

**처방**: CEO 턴 출력을 "타입된 팬아웃 제안"으로 정의(자유 추론 후 마지막 JSON 블록) → supervisor 또는 신규 검증기가 결정론 검사: 부서 allowlist(`canonical_profiles.py` 재사용), 카드 수 상·하한, workflow_role 필수, 원질의 원문 포함, 예산 필드 → 위반 시 구체적 오류로 재프롬프트(최대 2회) → 폴백은 결정론 기본 플랜. **`ceo_task_planner.py`의 검증 패턴을 실전 경로로 승격하는 작업**이지 신규 발명이 아니다.

### 2-6. 다중검정 회계 강화 — 공장 · 노력 중

**현재 상태** (AS-IS §6.5–6.6): DSR ≥ 0.95 게이트, PBO(CSCV) ≤ 0.5, trial family 단위 디플레이션, IC Spearman 비중첩 t ≥ 3.0, 워크포워드 비중첩 반기 윈도.

**문헌이 말하는 것** — 세 가지 수리:

1. **N의 정직성** (Bailey & López de Prado 2014): DSR은 N회 시도 하 기대 최대 SR(~√(2 log N) 스케일 + 시도 간 SR 분산)을 넘을 확률이다. 0.95 문턱은 원전 권장 그대로지만, **같은 2016–2026 KRX 표본을 때린 모든 백테스트가 N에 들어가야** 한다 — 다른 family 포함, 포기·실패 트라이얼 포함(AHM 프로토콜 항목 2와 동일 요구). family 스코프 N은 공장 전체가 하나의 거대 다중검정이라는 사실을 과소 반영해 디플레이션을 과소 적용한다. Harvey-Liu-Zhu의 상관 인지 보정이 올바른 모델.
2. **PBO 문턱** (Bailey et al. 2017): PBO=0.5는 "IS 최적 구성이 OOS 중앙값 아래일 확률이 동전 던지기"인 **무차별점**이다 — 0.49로 통과한 전략은 거의 아무것도 입증하지 못했다. 원전은 홀드아웃을 "신뢰 불가·부정확"으로 판정하고 CSCV의 **랭크 분포 전체**를 보라고 한다. 실무 관행은 ≤ 0.2. 처방: 문턱 ≤ 0.2 + CSCV 분포·성과 열화 기울기를 판정에 기록.
3. **family의 단위** (Jensen-Kelly-Pedersen, JF 2023): 153개 팩터는 **13개 테마로 군집**하며 증거는 테마 수준에서 합산(베이지언 수축)해야 한다. 9개 템플릿(MOM/REV/LOWVOL/RAMOM/LIQREV/BRK/TREND/ILLIQ/LOWMAX)은 실질 4–5개 테마 — family를 테마 클러스터로 재정의하면 DSR의 올바른 N도 함께 바뀐다. `trial_family.THEMES` 렌즈가 이미 있으므로 배선 작업이다.

추가: 워크포워드 반기 비중첩은 10년에 ~20윈도 = **단일 경로**. CPCV(López de Prado, AFML)는 조합 경로 분포를 만들어 PBO 입력을 강화한다 — 저장소 references.md의 KBS 2024 비교 논문이 이미 같은 결론("워크포워드를 만능 게이트로 보지 않는다")을 채택 근거로 기록하고 있으므로 설계 의도는 있고 실행만 남았다. 정합 확인: IC t ≥ 3.0은 Harvey-Liu-Zhu 허들과 정확히 일치 — 유지.

### 2-7. LLM 훈련 오염 방어 — 공장 · 노력 중

**현재 상태**: 헤드 에이전트(웹 스카우트)가 gpt-5.6-luna 등 프런티어 모델로 방법론 리드를 발굴한다. 스카우트 모델의 훈련 데이터에 2016–2025 KRX 역사·팩터 문헌이 포함돼 있다.

**문헌이 말하는 것** — 이것이 공장의 가장 깊은 위협인 이유:

- **Sarkar & Vafa(ICML 2025)**: 사전학습 룩어헤드 편향 직접 실증 — Llama 2에 2019년 9–11월 어닝콜만 주고 물어도 **25%+ 확률로 Covid-19 리스크를 "예측"**. LLM-루프 과거 평가는 전부 이 편향을 상속한다.
- **Li-Wang-Ma(2026), "Summoning the Oracle to Slay It"**: 추론 시점 암기 억제 실험 — 암기된 날짜 구간에서 순진한 LLM 백테스트 수익의 **최대 67%가 암기**였다.
- **He et al., ChronoBERT/ChronoGPT(2025)**: 연도별 데이터 컷오프로 훈련한 모델만이 신뢰 가능한 LLM 시대 백테스트를 만든다는 실용 프레임.
- **AlphaAgent(KDD 2025)**: 무제약 LLM 팩터 마이닝은 p-해킹·군집·급감쇠 팩터를 양산 — 완화책은 팩터 zoo 대비 **독창성 강제**, 가설-팩터 일관성 검사, 복잡도 페널티(생성기 자체를 정규화, 출력 필터만으로는 부족).
- **McLean & Pontiff(JF 2016)**: 97개 발표 예측자 — OOS 26% 감쇠, **발표 후 58% 감쇠**. 웹 스카우트가 수확하는 것은 정의상 발표 후 신호다.

핵심 논리: 오염은 **검정이 아니라 가설 선택에** 있으므로 어떤 트라이얼 카운트 디플레이션도 이를 수리하지 못한다. 스카우트가 "이게 한국에서 먹혔다"를 이미 알고 제안하면, 그 제안의 백테스트 성공은 정보가 아니다.

**처방**: ① **스카우트 모델 훈련 컷오프 이후 구간 + PAPER 실계측 구간을 유일한 결정적 OOS 증거로 승격** — 로드맵의 PAPER 단계를 형식 절차가 아니라 *그* 검정으로 재정의. ② 모델 ID·컷오프를 사전등록 지문에 포함(2-1과 연결). ③ lesson-code 중복 거부를 "자사 트라이얼 중복"에서 "공개 팩터 zoo 대비 유사도 점수"로 확장(AlphaAgent 독창성 강제) — `DUPLICATE_UNADDRESSED` 장치의 자연 확장. ④ Gate 0 prior에 발표 후 감쇠(–50%+)를 인코딩 — 문헌 SR을 달성 가능치로 취급 금지.

### 2-8. outbox 내구성 복원 — 기반 · 노력 중

**현재 상태** (AS-IS §8.4 + §11.1 조합): `apply_fill` → 동일 트랜잭션 outbox → relay가 Redis XADD 후 `SENT` 마킹 → consumer가 분개 후 ack. Redis는 AOF 없음. **XADD 성공(SENT) 후 소비 전에 Redis가 죽으면 그 체결은 재전송 경로 없이 영원히 미분개**된다.

**문헌이 말하는 것**:

- **Richardson(microservices.io, Transactional Outbox 정본)**: relay는 "메시지를 발행하고 그 사실을 기록하기 전에 크래시할 수 있다 — 따라서 1회 이상 발행되고, **소비자는 반드시 멱등이어야 한다**(처리한 메시지 ID 추적 등)". 패턴의 보증은 at-least-once이고, 이는 **브로커가 수락한 메시지를 내구 보존할 때만** 성립한다. outbox의 존재 이유가 이중 쓰기 창 제거이지, 잊어버리는 브로커의 용인이 아니다.
- **Kleppmann(DDIA Ch.11–12)**: "exactly-once"의 실체 = 내구·재생 가능 로그 위의 at-least-once 전달 + 멱등(또는 트랜잭션) 처리. Kafka EOS도 복제·fsync된 로그 위에 같은 방식으로 구축 — 브로커 내구성은 하중을 받는 전제이지 옵션이 아니다.
- **Redis 공식 지속성 문서**: 지속성 없으면 프로세스 사망 시 전부 소실. `appendfsync everysec` = 최대 1초 유실, `always` = 커맨드 배치별 fsync. "PostgreSQL급 안전을 원하면 RDB+AOF 병용". Streams의 PEL(미확인 목록)도 메모리 상태라 AOF 없이는 메시지와 ack 상태가 함께 증발.
- **Jepsen(Redis-Raft 분석, 2020)**: **복제된** Redis(Sentinel/Cluster)조차 "확인된 쓰기의 유실 창"을 허용 — 비동기 복제의 구조적 한계. `WAIT`도 강한 일관성을 만들지 못함.

**처방** 2층: ① **정확성 백스톱**(브로커 무관): 분개 테이블에 outbox `event_id` unique 제약(멱등 소비) + "SENT이지만 분개에 반영 안 된" outbox 행을 재-XADD하는 주기 대사 스윕. 이것만으로 종단 간 at-least-once 복원. ② **방어 심층**: Redis `appendonly yes` + `appendfsync everysec`(단일 호스트 체결량이면 `always`도 처리량 여유), `stop-writes-on-bgsave-error yes`, 명명 볼륨 마운트. 대안 검토: NATS JetStream(단일 바이너리, 파일 기반 스트림, 명시적 ack) — 이 규모에서 근사 드롭인.

### 2-9. 관측성 최소 스택 — 기반 · 노력 중

**현재 상태** (AS-IS §9.5): Prometheus 3지표 + OTel 계측이 risk/audit API와 파이프라인에 있으나 **compose에 수집기·Grafana·콜렉터가 없다**. `/metrics`는 아무도 긁지 않는다. 02/05 부서는 계측 자체가 없다. 이 부재가 시스템의 침묵 열화 지대(모델 조용한 강등, geopolitical/macro 노화, 스위치보드 리셋)를 영구화한다.

**문헌이 말하는 것**:

- **Google SRE Ch.6**(Ewaschuk): 4 골든 시그널(지연·트래픽·오류·포화), **원인이 아니라 증상에 알림**("증상 포착에 훨씬 더 많은 노력을"), 규칙은 최대한 단순·예측 가능·신뢰 가능하게 — 작은 스택이 복잡한 스택을 이긴다.
- **Hidalgo, *Implementing SLOs***(데이터 신뢰성 장): 파이프라인·배치의 SLI는 **신선도·완전성·정확성** — "데이터가 X보다 오래되지 않음" 신선도 SLO가 파이프라인 시스템의 표준 첫 SLI.
- **OpenTelemetry 공식**: 소규모 배포에는 no-collector/단일 에이전트 패턴 인정 — 콜렉터 함대는 소규모에서 명시적 안티패턴.
- **Monte Carlo 5기둥**: 신선도·볼륨·스키마·리니지·분포 — 신선도·볼륨이 자동 검사 수익률 최고. 일봉 350/전체 유니버스 어긋남 사건은 정확히 이 문헌의 예제다: 심볼·일별 행수 vs 선언 유니버스 비교 모니터가 있었다면 **첫날 발화**했다.

**처방**: Prometheus 1대 + Grafana + Alertmanager(또는 Prometheus 알림 규칙만) — 컨테이너 3개, OTel 게이트웨이 불요. 첫 알림 4개(전부 증상 기반, 사용자 가시 피해 직결):

| 알림 | 정의 | 잡는 결함 |
|---|---|---|
| outbox 최고령 미전송 나이 | `min(created_at) where status=PENDING` 초과 | relay 사망·적체 |
| 스트림 소비 지연 | XPENDING 최고령 엔트리 나이 | consumer 사망·적체 |
| 테이블별 신선도 랙 | `now() - max(ingest_ts)` per 핵심 테이블 | 수집기 침묵 사망, macro류 노화 |
| 체결-분개 대사 드리프트 | fills 수 vs 분개 수 괴리 | 2-8의 유실 창 실측 |

여기에 볼륨 모니터(심볼·일별 행수 vs 선언 유니버스)를 5번째로. 실주문 단계에서 이 스택이 2-12(자동 트립)의 센서가 된다.

### 2-10. EW/VW 로버스트니스 레그 + 신호 블렌딩 — 공장 · 노력 상

**현재 상태** (AS-IS §6.6): long-only 동일가중 top-N(한계 5–300), 유니버스는 "bar가 존재하는 전부"(~3,900종목, 소형주 포함, `SURVIVORSHIP_BIAS_DECLARED`). SUPPORTED 가설은 개별 전략으로 승격 요청. IR 벽(top-20 TC 0.114)은 top-300 개방으로 1차 해소.

**문헌이 말하는 것**:

- **Hou-Xue-Zhang(RFS 2020)**: 452개 앤어멀리 재현 — 마이크로캡 통제(NYSE 브레이크포인트·가치가중) 시 **65%가 |t|>1.96 실패, 82%가 다중검정 허들(2.78) 실패**. 생존 앤어멀리도 원 발표 대비 크게 축소. **동일가중 소형주가 단일 최대 인플레이터** — 현 설계가 정확히 가장 취약한 사양이다.
- **Grinold & Kahn(기본법칙)**: IR ≈ IC × √Breadth — 약한 IC로는 **독립 베팅 수**가 성과를 결정. 신호를 하나씩 배포하면 breadth를 버리는 것.
- **Clarke-de Silva-Thorley(FAJ 2002)**: IR = **TC** × IC × √BR — long-only·집중도·회전 제약의 전이계수는 0.3–0.8, 제약 포트폴리오는 예측 가치의 10–60%만 포착. top-20→300 개방이 TC 수리였다는 실측과 정확히 부합하며, 다음 병목도 이 프레임으로 예측 가능.
- **JKP(JF 2023)**: 개별 팩터는 노이즈, **테마 수준 결합이 탄젠시 포트폴리오의 유의한 구성** — 경제 가치는 블렌드에 있다.

**처방**: ① SUPPORTED 판정 전 **가치가중(또는 KOSPI200 브레이크포인트·사이즈 필터) 확인 레그**를 릴리스 게이트에 추가 — EW/VW 결과 괴리는 적신호로 기록. ② SUPPORTED 신호들을 개별 배포하지 말고 **합성 점수 단계**(z-score 또는 IC 가중 블렌드 → 단일 포트폴리오) 신설 — 상관 ~0.3인 신호들의 블렌드는 최고 단일 신호 대비 IR ~2배. 부재한 포트폴리오 계층(전략 결합·크로스 신호 리스크 통제)의 자연스러운 자리이며, D4(strategy.versions 승격 파이프라인)와 함께 설계할 것.

### 2-11. 구성 드리프트 제거 — 기반 · 노력 하

**현재 상태** (AS-IS §11.1, D5, §3.3): `.env` 48키 이중 정의(dotenv last-wins로 Redis Cloud가 로컬을 이김), 평문 크리덴셜 백업 사본(`.env.bak-20260812`), 전략 스위치보드는 프로세스 메모리 싱글턴(재시작 시 전 전략 조용히 OFF), 환경 3종(로컬/EC2/EB)의 도구 표면 분기.

**문헌이 말하는 것**:

- **SEC, In re Knight Capital(34-70694, 2013)** — 구성 드리프트의 정본 사후 분석: 8대 중 1대에 신규 코드 미배포(2인 검증 없음) → 2003년부터 방치된 죽은 코드(Power Peg)가 재활용된 플래그로 재활성 → 체결 추적이 수년 전 이동돼 무한 자식 주문 → 사전 개장 경고 메일 **97통 무대응** → 집계 노출 한도 부재 → 정지 절차 부재로 45분·420만 체결·$460M. SEC가 적시한 위반: 15c3-5(b), (c)(1)(i)(ii), (c)(2), (e). 판결 요지는 코드가 아니라 **배포 검증·상태 검증·대응 절차의 부재**.
- **12-Factor III·X**: 배포당 모호하지 않은 단일 구성값, "tools gap"(환경 간 다른 백킹 서비스/바인딩) 경고 — 환경 3분기는 이 위반의 정의 그 자체.
- **OWASP Secrets Management**: env 변수는 전 프로세스 접근·로그 유출 가능 — 시크릿은 관리 저장소로, 중복·커밋 금지, `.env`는 로컬 인터페이스로만.

**처방**: ① 환경별 파일 분리(`.env.local`/`.env.ec2`, compose `env_file` 선택) + 키당 정의 1개. ② **부팅 시 fail-fast 검증기**: 유효 Redis/DB 엔드포인트를 로그에 출력, 중복 키·모호 조합이면 기동 거부. ③ 스위치보드 영속화(`strategy_switch.py` → DB) + 재시작 후 상태 양성 확인 요구(조용한 OFF도 조용한 ON도 금지 — Knight의 플래그 드리프트 계급). ④ `.env.bak` 제거·시크릿 회전. ⑤ 도구 표면을 카드 계약의 일부로(2-5 스키마에 표면 버전 필드) — 같은 카드가 다른 표면에서 실행되는 것 자체를 검출 가능하게.

### 2-12. 자동 트립 + 실시간 사후 대사 — 실주문 전 · 노력 상

**현재 상태** (AS-IS §8.2, §8.5): 킬스위치 트립은 수동 HTTP PUT뿐. 사후(post-trade) 리스크 모니터 프로세스 없음(`projection_worker.py` compose 미등록). 10 사전 게이트는 정교하나 가격 칼라·메시지율 한도는 없음. trading-api 무인증.

**문헌이 말하는 것** — 규제가 사전 게이트와 **동급으로** 요구하는 나머지 절반:

- **SEC 15c3-5(c)(1)**: 자동(체계적) 차단 — 신용/자본 문턱 초과 주문 차단, "부적절한 가격·수량 파라미터 초과 또는 **중복 주문** 징후 거부". (c)(2): 사전 승인된 인원으로 접근 제한. (d): 직접·배타 통제. (e): 연차 유효성 검토 + CEO 인증.
- **MiFID II RTS 6 Art.15**: 주문마다 4종 명명 통제 — **가격 칼라**(파라미터 밖 자동 차단/취소), 최대 주문 금액, 최대 주문 수량, **최대 메시지 한도**(제출+정정+취소 합산). + 반복 자동 체결 스로틀(반복 트리거 시 전략 일시정지, **사람이 재가동할 때까지**). Art.16: 담당 트레이더 + 독립 리스크 기능의 실시간 감시, 이상 행동 실시간 알림. Art.17: 사후 통제 — 자사 전자 로그 vs 거래소/브로커 기록·드롭 카피 대사, 포지션·노출 재계산. Art.5–9: 적합성 시험, 거래소 컨포먼스, 비운영 시험 환경, **통제 배포**(축소 한도로 실전 개시), 연차 자가평가.
- **Nasdaq Equity Kill Switch(NNRE)** — 자동 트립의 정본: 사전 설정 순명목 노출 캡, 50/75/85/90/95%에서 격상 통보, 위반 시 **사람 개입 없이** 포트 차단+미체결 취소.
- **FINRA 15-09**: "최소 단계로 알고리즘 정지" + 비의도 행동 탐지 알림 + 트레이더의 통제 우회 방지.
- **FIA 모범규준(2024, 14년 가이드 통합)** 트리거 수렴 세트: 누적 명목/손실 문턱, 메시지율, 반복/중복 주문 패턴, reject storm, 드롭 카피 괴리, 접속 단절(cancel-on-disconnect 기본).

**처방**: ① **자동 트립 배선** — 트리거: 누적 손실/드로다운 캡(NNRE 모델: 조기 경고 단계 포함), 브로커 reject storm(N회/M초), 메시지율 위반, 중복 주문 패턴, **UNKNOWN 상태 타임아웃 초과**(기존 차단 규칙의 완성). 트립 대상은 2-2의 내구 킬스위치. ② **실시간 사후 대사** — 브로커 체결/잔고 vs 복식부기 원장, 계산 포지션 vs 한도의 지속 대사 + 이름 있는 담당자에게 가는 알림(Knight의 97통 메일의 반대). **원장-브로커 괴리 자체를 트립 트리거로** — 기존 원장이 부기에서 통제 장치로 승격되는 지점. ③ 10 게이트에 가격 칼라·메시지율 2개 추가. ④ 멱등 주문 제출: intent당 고유 client order ID를 상태와 같은 트랜잭션에 저장, 재시도는 ID 재사용, UNKNOWN은 재제출이 아니라 **LS 조회 대사로만** 해소. ⑤ trading API 인증(15c3-5(c)(2)) + 첫 실전은 의도적 축소 한도(RTS 6 controlled deployment). D1–D5 배선은 이 관문 뒤에.

---

## 3. 영역 1 — 백테스트 타당성·전략 공장

조사 질문 6개, 검증 소스 21편(학술 18 + 규제 3).

### RQ1. 백테스트 과적합·다중검정 — DSR 0.95 + PBO 0.5는 문헌 정합인가?

| 소스 | 핵심 주장 | 적용 |
|---|---|---|
| [Bailey & López de Prado, "The Deflated Sharpe Ratio" (JPM 2014)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551) | DSR = N회 시도 하 기대 최대 SR(~√(2 log N) + 시도 간 분산 의존)을 관측 SR이 넘을 확률. 왜도·첨도·표본 길이 보정. **DSR > 0.95가 5% 유의 표준** | 0.95 문턱은 정합. 단 N은 같은 표본을 때린 전체 시도여야 — family 스코프는 과소 |
| [Bailey, Borwein, LdP & Zhu, "The Probability of Backtest Overfitting" (JCF 2017)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2326253) | PBO(CSCV) = IS 최적 구성이 OOS 중앙값 미만일 확률. 홀드아웃은 "신뢰 불가·부정확". 점추정이 아니라 랭크 분포 | **0.5는 무차별점** — ≤0.2로 조이고 분포·열화 기울기 기록 |
| [Harvey, Liu & Zhu, "…and the Cross-Section of Expected Returns" (RFS 2016)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2249314) + [Harvey & Liu, "Backtesting" (JPM 2015)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2345489) | 수십 년 집단 데이터 마이닝 후 신규 팩터는 **t ≥ 3.0**. 헤어컷 샤프는 비선형 — 균일 50% 헤어컷은 "심각한 오류" | IC t ≥ 3.0 게이트는 정확히 이 허들 — 유지 |
| White, "A Reality Check" (Econometrica 2000) + [Romano & Wolf (Econometrica 2005)](http://www-stat.wharton.upenn.edu/~steele/Courses/956/Resource/MultipleComparision/RomanoWolf05.pdf) | 검토한 **전략 전체 우주**를 부트스트랩해 최고 성과자의 벤치마크 초과를 검정. 스텝와이즈 FWER 통제 | 9개 템플릿을 개별이 아니라 공동 검정하는 Romano-Wolf식 벤치마크 테스트 추가 |

### RQ2. 사전등록·연구 프로토콜

| 소스 | 핵심 주장 | 적용 |
|---|---|---|
| [Arnott, Harvey & Markowitz, "A Backtesting Protocol in the Era of ML" (JFDS 2019)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3275654) | 7범주 체크리스트: 사전 경제 논리 / **포기분 포함 전체 시도 수 추적** / 데이터 무결성(결과 후 표본·제외규칙 변경 금지, PIT) / 교차검증 규율 / 모델 동학·크라우딩 / 복잡도 통제 / 연구 문화(백테스트 SR이 아니라 과정에 보상) | 지문 미구속 = 항목 3 정면 위반. Pydantic 계약(경제논리·경쟁설명·반증검정)은 이례적으로 프로토콜 근접 — 업계 관행보다 우수 |
| [López de Prado & Zoonekynd, "A Protocol for Causal Factor Investing" (ADIA Lab 2025)](https://www.adialab.ae/research-series/a-protocol-for-causal-factor-investing) | 백테스트는 인과 기제의 증거가 아님. 인과 그래프(교란·충돌 변수), 기제를 기각할 반증 검정, 적합이 아니라 인과로 정당화된 사양 선택 — "팩터 신기루" | falsification_tests ≥ 1 요구는 방향 정합. 인과 그래프 선언은 다음 단계 후보 |
| López de Prado, *Advances in Financial ML* (Wiley 2018) | 퍼징+엠바고 체계화, **CPCV** — 워크포워드는 단일 경로라 재실행으로 과적합 가능, CPCV는 경로 분포 생성 | 반기 비중첩 ~20윈도 = 단일 경로. CPCV로 PBO 입력 강화 |

### RQ3. 팩터 복제 위기 — 웹 마이닝 발굴의 함의

| 소스 | 핵심 주장 | 적용 |
|---|---|---|
| [Hou, Xue & Zhang, "Replicating Anomalies" (RFS 2020)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3275496) | 마이크로캡 통제 시 452개 중 **65% |t|>1.96 실패, 82% 다중검정(2.78) 실패**. 동일가중 소형주가 단일 최대 인플레이터 | EW top-N over ~3,900종목이 가장 취약한 사양. VW/사이즈 필터 레그 필수 |
| [McLean & Pontiff, "Does Academic Research Destroy Stock Return Predictability?" (JF 2016)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2156623) | 97개 발표 예측자: OOS 26% 감쇠, **발표 후 58% 감쇠**. 고 IS 수익·비유동·고 고유변동 종목에서 최악 | 웹 스카우트 수확물은 정의상 발표 후 신호 — Gate 0 prior에 감쇠 인코딩 |
| [Jensen, Kelly & Pedersen, "Is There a Replication Crisis in Finance?" (JF 2023)](https://onlinelibrary.wiley.com/doi/full/10.1111/jofi.13249) | 낙관적 반론: 베이지언 수축 시 대부분 재현, **13개 테마 군집**, 93개국(한국 포함) OOS 유효. 증거는 테마 수준에서 합산 | trial family를 테마 클러스터로 재정의 — 9템플릿 ≈ 실질 4–5테마 |

### RQ4. 거래비용·용량 — 한국 특수성

| 소스 | 핵심 주장 | 적용 |
|---|---|---|
| Almgren & Chriss, "Optimal Execution" (J. Risk 2000) | 임시/영구 임팩트 + 타이밍 리스크 분해. 후속 실증 합의: 임팩트는 참여율에 **오목(√ 법칙)** | 슬리피지 7.2bps 상수 모순 — 오목 곡선 적합 + 용량 보고 |
| [Novy-Marx & Velikov, "A Taxonomy of Anomalies and Their Trading Costs" (RFS 2016)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2535173) | 단면 월회전 50%+ 전략은 비용 후 생존 희박. **buy/hold 밴드가 최고 완화책**. 사이즈/밸류 최대 용량, 리버설류 최소 | 일별 리밸런스 REV/LIQREV가 위험 지대 — 밴드를 템플릿 표준으로 |
| [Frazzini, Israel & Moskowitz, "Trading Costs" (2018)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2294498) | ~$1T 실집행 19개 시장: 인내 집행 실비용은 학술 추정 ~1/10, 용량은 스타일 의존, **단기 리버설이 최대 비용 제약** | 리버설 템플릿의 실전 용량 추정 필수 |
| [PwC Tax Summaries](https://taxsummaries.pwc.com/republic-of-korea/corporate/other-taxes) · [FSC 공매도 재개(2025-03-31)](https://www.fsc.go.kr/eng/pr010101/82465) | 거래세: ~2019 0.30% → 2021–22 0.23% → 2023 0.20% → 2024 0.18% → 2025 0.15% → **2026 0.20% 회귀**. 공매도: 2023-11~2025-03 전면 금지 후 전 종목 재개(이전엔 350종목만) | 15bps 고정은 양방향 오류. 금지기는 가장 싼 첫 레짐 분석 |

### RQ5. LLM/AI 자동 알파 발굴 (2023–2026)

| 소스 | 핵심 주장 | 적용 |
|---|---|---|
| [AlphaAgent (KDD 2025)](https://arxiv.org/abs/2502.16789) | 무제약 LLM 팩터 마이닝 = p-해킹·군집·급감쇠. 완화: 팩터 zoo 대비 독창성 강제, 가설-팩터 일관성, 복잡도 페널티 — **생성기 정규화** | lesson-code 중복 거부를 공개 팩터 유사도로 확장 |
| [Sarkar & Vafa, "Lookahead Bias in Pretrained LMs" (ICML 2025)](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=4754678) | Llama 2가 2019-09~11 어닝콜에서 25%+ 확률로 Covid 리스크 "예측" — 사전학습 누출 직접 실증 | LLM-루프 과거 평가 전부 오염 상속 — 가설 선택 오염은 디플레이션 불가 |
| [He et al., "Chronologically Consistent LLMs" (2025)](https://arxiv.org/abs/2502.21206) + [Li-Wang-Ma, "Summoning the Oracle to Slay It" (2026)](https://arxiv.org/abs/2605.24564) | 연도별 컷오프 훈련(ChronoBERT/GPT)이 신뢰 프레임. 암기 억제 시 IS 수익 최대 **67% 소멸** | 컷오프 이후+PAPER를 유일 결정 증거로. 모델 컷오프를 지문에 |
| [Microsoft R&D-Agent(Q) (NeurIPS 2025)](https://www.microsoft.com/en-us/research/publication/rd-agent-quant-a-multi-agent-framework-for-data-centric-factors-and-model-joint-optimization/) | 언어 상호작용에서 직접 신호를 내는 에이전트는 환각 — 가설→코드→평가 엄격 분해 + 밴딧 스케줄러 | 공장의 제안→게이트→백테스트 사슬 구조를 검증하는 실증 |

### RQ6. 신호 결합 vs 단일 전략 배포

| 소스 | 핵심 주장 | 적용 |
|---|---|---|
| Grinold & Kahn, *Active Portfolio Management* (2e, 1999) | IR ≈ IC × √Breadth — 약한 IC는 독립 베팅 수로 보상. 약상관 신호의 합성이 유효 IC 상승 | 단일 전략 배포는 breadth 포기 |
| [Clarke, de Silva & Thorley (FAJ 2002)](https://www.tandfonline.com/doi/abs/10.2469/faj.v58.n5.2468) | IR = **TC** × IC × √BR — long-only·집중·회전 제약의 TC는 0.3–0.8, 예측 가치의 10–60%만 포착 | IR 벽·top-300 개방의 이론적 설명. 다음 병목 예측 프레임 |
| JKP (JF 2023, 상동) | 개별 팩터는 노이즈, 테마 결합이 탄젠시 포트폴리오 구성 | SUPPORTED → 합성 점수 단계 신설 |

---

## 4. 영역 2 — 멀티에이전트 LLM 신뢰성

조사 질문 6개, 검증 소스 18편.

### RQ1. 실패 분류학

| 소스 | 핵심 주장 | 적용 |
|---|---|---|
| [Cemri et al., "Why Do Multi-Agent LLM Systems Fail?" — MAST (2025)](https://arxiv.org/abs/2503.13657) | 1,600+ 주석 트레이스, 7 프레임워크, κ=0.88. 14개 실패 모드 3범주: **사양·시스템 설계**(태스크/역할 사양 불량, 단계 반복, 이력 상실) / **에이전트 간 불일치**(정보 은닉, 입력 무시, 표류, 추론-행동 불일치) / **태스크 검증**(조기 종료, 불완전·오검증). 실패는 구조적 — 프롬프트 수리는 미미, 검증 기제·표준 통신 프로토콜이 처방 | 무방비 팬아웃=1범주, 자유 텍스트 카드=2범주, WARN 소프트패스·무인용 합성=3범주. eval 하네스 구축 시 MAST 14모드를 주석 스키마로(LLM 주석 파이프라인 동봉) |
| [Microsoft AI Red Team, "Taxonomy of Failure Modes in Agentic AI" (2025, v2 2026)](https://www.microsoft.com/en-us/security/blog/2025/04/24/new-whitepaper-outlines-the-taxonomy-of-failure-modes-in-ai-agents/) | 프로덕션 레드팀 기반: 신규(에이전트 주입, **플로 조작**, 사칭, 침해) vs 증폭(메모리 오염, 교차 도메인 주입, HITL 우회) | 칸반 카드 본문 = 다음 에이전트의 비신뢰 입력(주입 표면). watchdog 강제 완료 = HITL 우회 감사 대상 |

### RQ2. 오케스트레이션 — 워크플로 vs 에이전트 논쟁

| 소스 | 핵심 주장 | 적용 |
|---|---|---|
| [Anthropic, "Building Effective Agents" (2024-12)](https://www.anthropic.com/engineering/building-effective-agents) | 워크플로(코드가 LLM을 지휘) vs 에이전트(LLM이 자율 지휘) 구분. 5패턴(체이닝·라우팅·병렬·오케스트레이터-워커·평가-최적화). **가장 단순한 구조를 찾고, 정확성이 중요한 모든 단계에 결정론 코드를** | supervisor 측은 이미 준수. 최대 레버리지 지점(팬아웃)에서만 위반 |
| [Anthropic, "How We Built Our Multi-Agent Research System" (2025-06)](https://www.anthropic.com/engineering/built-multi-agent-research-system) | 멀티에이전트가 단일 대비 **+90.2%**(병렬 읽기 태스크). 신뢰성 교훈: 명시적 목표·형식·예산 없으면 중복·표류, 전체 트레이싱 필수, 장기 실행은 내구 재개("레인보 배포"), **인용은 전용 검증 패스**, 대표 질의 ~20개 회귀가 대부분의 퇴행 포착 | 8부서 팬아웃 구조 검증 + 부재한 3요소(카드별 계약, 트레이싱, 인용 패스) 명시 |
| [Cognition, "Don't Build Multi-Agents" (2025-06)](https://cognition.com/blog/dont-build-multi-agents) | 반론: 병렬 서브에이전트는 컨텍스트 분절로 취약 — "전체 트레이스를 공유하라", "행동은 암묵 결정을 담는다". 2026 완화: **쓰기는 단일 스레드, 추가 에이전트는 읽기·리서치 기여**일 때 유효 | 읽기 팬아웃+단일 합성은 양 진영 합의 구성 — 단 카드에 충분한 공유 컨텍스트(원질의 원문+선행 결정) 필요 |
| [Kambhampati et al., "LLMs Can't Plan, But Can Help Planning in LLM-Modulo Frameworks" (ICML 2024)](https://proceedings.mlr.press/v235/kambhampati24a.html) | LLM은 계획 생성·자기검증 불가 — LLM 생성 + 외부 건전 검증기 + 반복 루프가 생산적 구조 | 팬아웃 가드(2-5)의 직접 근거 |

### RQ3. LLM 판정자·에이전트 평가

| 소스 | 핵심 주장 | 적용 |
|---|---|---|
| [Zheng et al., MT-Bench (NeurIPS 2023)](https://arxiv.org/abs/2306.05685) + [Wataoka et al., 자기선호 정량화 (2024)](https://arxiv.org/pdf/2410.21819) | 판정자 ~80% 인간 일치하나 위치·장황·**자기선호** 편향, 수학·추론 채점 한계. 완화: 위치 스왑 일치, 참조 기반 루브릭 | QA 레인 판정이 전부 상속. 같은 GPT 계열 판정이면 자기선호 직격 |
| [Yao et al., τ-bench (2024)](https://arxiv.org/abs/2406.12045) | DB 최종 상태 vs 목표 상태의 **결정론 비교** + **pass^k**(k연속 성공 확률). GPT-4o급도 <50% 해결, pass^8 < 25% | ground truth 있는 곳은 결정론 검사 우선. 부서별 pass^k 보고 — retry≤2 하에서 watchdog 발화 빈도를 결정하는 바로 그 양 |
| [Lù et al., AgentRewardBench (2025)](https://arxiv.org/abs/2504.08942) | 1,302 전문가 주석 궤적 × 12 판정자: 어떤 판정자도 전 영역 우수 불가, 부작용·반복 루프 못 봄, 순수 규칙은 과소 보고 — **궤적 수준 검토 필요** | 최종 답만 평가하면 부서 에이전트의 궤적 병리(루프·부작용) 누락 |
| [Liu et al., AgentBench (ICLR 2024)](https://arxiv.org/abs/2308.03688) + [Yehudai et al., 에이전트 평가 서베이 (2025)](https://arxiv.org/abs/2503.16416) | 프런티어 API 모델 vs ≤70B 오픈 모델의 에이전트 능력 격차가 **가장 큼**(장기 추론·지시 준수). 서베이: 능력별/응용별 × 오프라인/온라인 평가 체계, 프로덕션은 온라인 연속 평가로 이동 중 | **Qwen 1.7B/14B 워커 계층이 최약 고리** — 1.7B 개발 결과는 14B 프로덕션 행동을 예측 못함(환경 분기와 복합). 워커 카드에 최엄격 스키마 |

### RQ4. 도구 사용 보안·최소 권한

| 소스 | 핵심 주장 | 적용 |
|---|---|---|
| [Beurer-Kellner et al., "Design Patterns for Securing LLM Agents against Prompt Injections" (2025)](https://arxiv.org/abs/2506.08837) | 6패턴(action-selector, **plan-then-execute**, map-reduce, dual LLM, code-then-execute, context minimization). 통일 원리: 비신뢰 입력 섭취 후에는 능력 집합 제한 — **보안은 구조(할 수 있는 것)이지 프롬프트(하라고 한 것)가 아니다** | MCP 도구 표면 축소 = 문헌의 권장 기제 그 자체. plan-then-execute → 팬아웃 토폴로지는 비신뢰 데이터 섭취 전 고정 |
| [Debenedetti et al., CaMeL (DeepMind 2025)](https://arxiv.org/abs/2503.18813) | 신뢰 질의에서 제어·데이터 흐름 추출 — 비신뢰 데이터가 프로그램 흐름을 못 바꿈. 데이터 값에 **capability 부착**, 도구 호출 시점 결정론 정책 강제. AgentDojo 77% 해결 + **증명 가능** 보안(무방비 84% 대비 소폭 비용) | "도구 표면+결정론 게이트 > 프롬프트 제한"의 최강 인용. 카드 페이로드에 출처 스탬프 → supervisor 게이트가 고결과 전이 전 출처 검사(기존 pit_provenance 규율과 동형) |
| [Hou et al., MCP 위협 지형 (2025)](https://arxiv.org/abs/2503.23278) + [MCP 서버 1,899개 실증 (2025)](https://arxiv.org/abs/2506.13538) | 서버 수명주기 위협(설치자 스푸핑, **tool poisoning**, 이름 충돌, 구성 드리프트). 실측: 7.2% 일반 취약, **5.5% tool poisoning**. 방어는 정적·결정론: 메타데이터 핀·검증, 표면 최소화, 기동 감사 | liaison 쓰기 도구 물리 제거 = 권장 방어 그대로. 단 읽기 도구의 **설명 메타데이터도 공격면** — 기동 시 매니페스트 핀·diff. 환경별 도구 표면 분기 = 이 문헌의 "구성 드리프트" 실패 계급 |

### RQ5. 장기 실행 열화·복구

| 소스 | 핵심 주장 | 적용 |
|---|---|---|
| [Kwa et al. (METR), "Measuring AI Ability to Complete Long Tasks" (2025)](https://arxiv.org/abs/2503.14499) | 50% 성공 시간 지평(~7개월마다 2배)— 단 80% 신뢰 지평은 50% 지평의 수분의 1. 긴 자율 사슬은 프런티어 모델도 비신뢰 | 카드를 워커 신뢰 지평 안쪽으로 — 14B 로컬 모델은 분 단위. 짧고 개별 검증 가능한 카드 다수 > 긴 카드 소수 |
| [Ord, "Is there a half-life for the success rates of AI agents?" (2025)](https://arxiv.org/abs/2505.05115) | METR 재분석: 성공률은 **분당 일정 위험률의 지수 감쇠**(반감기 모델). 한 번의 미복구 오류가 전체를 죽임 — 에이전트는 인간의 자기 교정 부재 | 카드 완료 = 위험 시계 리셋(체크포인트) — 칸반 그래프 구조의 이론 근거. **retry≤2가 p→p³이 되려면 재시도가 독립이어야** — 같은 오염 카드 텍스트 재생은 독립이 아님, 신선한 컨텍스트 필요 |
| [Chroma, "Context Rot" (2025)](https://www.trychroma.com/research/context-rot) + [Anthropic, "Effective Context Engineering" (2025)](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | 18개 프런티어 모델 — 한계 훨씬 전(200K 중 50K에서도) 비균일 성능 저하, 방해 요소·낮은 유사도에서 악화. 처방: 압축, **구조화 노트(외부 상태)**, 서브에이전트 컨텍스트 격리 | 1.7B/14B는 훨씬 심함 — 부서 브리핑 최소·고신호로. 칸반 보드 = 구조화 노트 외부 상태(이미 문헌 정합). 위로 올리는 요약은 결론이 아니라 **결정**을 보존 |

### RQ6. 에이전트 간 통신 계약

| 소스 | 핵심 주장 | 적용 |
|---|---|---|
| [Tam et al., "Let Me Speak Freely?" (EMNLP 2024)](https://arxiv.org/abs/2408.02442) + [Constraint Tax (2026)](https://arxiv.org/pdf/2606.25605) | 엄격 형식 제약(JSON 모드·제약 디코딩)은 **추론 성능을 깎으며** 소형 모델일수록 심함 — 오픈 소형 모델에선 도구 호출 억제까지. 해법: 자유 추론 → 스키마 변환·검증 2단계 | 카드 계약은 경계에서 타입: 자유 추론 → 마지막 JSON 블록 → 결정론 파서(실패 시 재시도). 14B 워커에 전체 출력 제약 디코딩 금지 |
| MAST 2범주 + Cognition 단일 작성자 원칙 (상동) | 정보 은닉·입력 무시·표류는 미명세 자유 텍스트 핸드오프에서 발생 — 표준 통신 프로토콜이 구조적 완화. 핸드오프는 결론 뒤의 결정·트레이스를 동반해야 | 버전드 카드 스키마 필수 필드: 목표, 원질의 원문, 산출물 형식, 예산, 출처, **"내린 결정들"** |
| [Google + Linux Foundation, A2A Protocol (2025)](https://developers.googleblog.com/en/a2a-a-new-era-of-agent-interoperability/) | 150+ 조직 수렴: 에이전트 간 핸드오프 = **타입된 태스크 객체 + 명시적 수명주기 상태기계**(submitted/working/input-required/completed/failed/canceled) + 능력 카드 + 구조화 아티팩트 | A2A 채택 불요 — 단 그 수명주기 설계(명시적 failed·input-required)는 미인식 판정이 WARN·강제완료로 붕괴하는 현 패턴의 검증된 대안 템플릿 |

보조: [Dong et al., "A Taxonomy of AgentOps" (2024)](https://arxiv.org/abs/2411.05285) — 무엇을 트레이싱해야 하는가(목표·계획·도구 호출·아티팩트·판정, 세션/트레이스/스팬 수준)의 체계적 지도 — 부재한 수집기의 참조 템플릿.

---

## 5. 영역 3 — 트레이딩 안전장치·규제

조사 질문 6개, 검증 소스 15편. AS-IS 결함 매핑: G1 킬스위치 휘발·fail-open / G2 자동 트립 부재 / G3 사후 감시 부재 / G4 주문 경로 단절·UNKNOWN / G5 무인증 API / G6 메모리 스위치보드.

### RQ1. SEC Rule 15c3-5 (Market Access Rule)

| 소스 | 요구사항 | 격차 매핑 |
|---|---|---|
| [17 CFR § 240.15c3-5](https://www.law.cornell.edu/cfr/text/17/240.15c3-5) + [최종 규칙 34-63241 (2010)](https://www.sec.gov/files/rules/final/2010/34-63241.pdf) | (b) 재무·규제·운영 리스크 통제 체계의 수립·문서화·유지. (c)(1) 신용/자본 문턱 초과·오류 주문("부적절한 가격·수량 파라미터 초과 **또는 중복 주문 징후**")의 **체계적(자동) 차단**. (c)(2) 무권한 거래 방지, 사전 승인 인원으로 기술 접근 제한. (d) 직접·배타 통제. (e) 연차 유효성 검토 + CEO 인증 | 중복 주문 조항 → G4 직격(UNKNOWN·재시도가 중복을 만들 수 있는 경로). (c)(2)·(d) → G5(루프백 뒤라도 무인증은 접근 통제 실패). (e) → 주기적 통제 검토의 템플릿 |

### RQ2. MiFID II RTS 6 (EU 2017/589)

| 소스 | 요구사항 | 격차 매핑 |
|---|---|---|
| [RTS 6 원문](https://eur-lex.europa.eu/eli/reg_del/2017/589/oj/eng) + [ESMA 감독 브리핑 (2026)](https://www.esma.europa.eu/sites/default/files/2026-02/ESMA74-1505669079-10311_Supervisory_Briefing_on_Algorithmic_Trading_in_the_EU.pdf) | **Art.15**: 주문마다 가격 칼라 / 최대 주문 금액 / 최대 수량 / **최대 메시지 한도**(제출+정정+취소) + 반복 자동 체결 스로틀(사람이 재가동할 때까지 정지). **Art.12**: 킬 기능 — 알고리즘·트레이더·데스크·클라이언트 단위 미체결 전량 즉시 취소 + 전 주문의 책임 식별. **Art.16**: 담당 트레이더+독립 리스크의 실시간 감시·알림. **Art.17**: 자사 로그 vs 거래소/브로커·드롭 카피 대사, 포지션·노출 재계산. **Art.5–9**: 시험 방법론, 컨포먼스, 비운영 환경, **축소 한도 실전 개시**, 연차 자가평가 | Art.12+식별 의무 → G1/G2(상태가 증발하고 기본 ENABLED인 킬 기능은 "긴급 조치" 목적 불충족). Art.16–17 → G3 전면(사전 엔진만으로는 RTS 6의 절반). Art.15 메시지 한도 → 10게이트에 부재(회전율 게이트는 사업 회전이지 메시지율 방어 아님). Art.8 → G4 배선 시 준수 표준 |

### RQ3. 킬스위치 설계 — FIA·거래소

| 소스 | 요구사항/교훈 | 격차 매핑 |
|---|---|---|
| [FIA 모범규준 (2024, 14년 통합)](https://www.fia.org/sites/default/files/2024-07/FIA_WP_AUTOMATED%20TRADING%20RISK%20CONTROLS_FINAL_0.pdf) | 트리거 수렴 세트: 누적 명목/손실, 메시지율, 반복/중복 주문, reject storm, 드롭 카피 괴리, 접속 단절(COD 기본) | G2의 트리거 사양 그 자체 |
| [CME Kill Switch](https://www.cmegroup.com/tools-information/webhelp/globex-credit-controls/Content/Kill-Switch.html) | 1회 활성화 = 신규 차단 + 미체결 취소 **원자적**, LCE/EF/SenderComp 계층 캐스케이드, 재가동은 명시적 조작 | 킬 상태 = 끈적(sticky), 안전 상태가 지속되고 위험 상태가 양성 행위를 요구 |
| [Nasdaq Equity Kill Switch (2014)](https://www.nasdaqtrader.com/content/EquityKillSwitch.pdf) + [SEC 승인 34-71555](https://www.sec.gov/files/rules/sro/nasdaq/2014/34-71555.pdf) | **자동 트립 정본**: 사전 설정 NNRE 캡, 50/75/85/90/95% 격상 통보, 위반 시 사람 없이 포트 차단+취소 | G2 — 펌 레벨 등가물(손실 캡·reject storm·메시지율·중복 자동 트립)은 업계 확립 관행 |
| [FINRA Notice 15-09 (2015)](https://www.finra.org/rules-guidance/notices/15-09) | "최소 단계로 알고리즘/플랫폼 정지", 비의도 행동 알림, 트레이더의 통제 우회 방지 | G1(무인증 PUT은 인가 인원 제한 규범 위반)·G2 |
| [CFTC Electronic Trading Risk Principles (2020)](https://www.federalregister.gov/documents/2020/07/15/2020-14381/electronic-trading-risk-principles) | 시장 교란 방지 원칙 기반 규제 — 사전 통제+운영 안전장치 | 전반 배경 |

### RQ4. Knight Capital 2012 — 정본 사후 분석

| 소스 | 발견 | 교훈 → 격차 |
|---|---|---|
| [SEC, In re Knight Capital (34-70694, 2013)](https://www.sec.gov/files/litigation/admin/2013/34-70694.pdf) + [PRMIA 케이스 스터디](https://prmia.org/common/Uploaded%20files/eAI/PRMIA%20Case%20study%20-%20Knight%20Trading.pdf) | ① 8대 중 1대 미배포, 2인 검증 없음 ② 2003년 죽은 코드(Power Peg)가 **재활용 플래그**로 재활성, 체결 추적은 수년 전 이동 → 무한 자식 주문 ③ 개장 전 경고 메일 **97통 무대응** ④ 집계 노출 한도 부재($2M 계좌 한도는 주문을 거부하지 않음) ⑤ 정지 절차 부재 — 45분, 420만 체결, $460M, 초기 진단이 정상 서버 7대의 올바른 코드를 제거해 악화 | 배포는 전 노드 실행분의 양성 검증(체크섬·2인) → G6·§11. 죽은 코드 제거·플래그 재활용 금지 → 스위치보드/12.1 사어 목록. 집계 노출이 주문 흐름을 하드 거부로 게이트 → 10게이트 유지+확장. 소유자 없는 알림 = 알림 없음 → G3. 리허설된 1-액션 정지 절차가 go-live 전 존재 → G2. **45분의 대부분은 탐지가 아니라 결정 절차 부재** |

### RQ5. 한국 규제 맥락

| 소스 | 내용 | 결론 |
|---|---|---|
| KRX 파생시장 알고리즘 관리 방안(2013) · [The TRADE 보도](https://www.thetradenews.com/krx-suggests-refinements-for-algorithmic-trading-in-derivatives/) | 알고 계좌 **등록**, **누적 호가 한도**, 거래소 **킬스위치**, **접속 단절 일괄취소(COD)**, 과다 호가 부담금 | 국내 규범의 4종 세트 |
| HanMag 사고(2013-12) · [비즈한국 회고](https://www.bizhankook.com/bk/article/30640) | 이자율 변수 days/0 코딩 → 143초에 37,900 오류 체결, 462억 손실, **회사 파산**. 이후 호가 일괄취소 제도 증권시장 확대(2016) | 한국판 Knight — 국내에서도 자동 정지·오류 주문 통제가 실존 리스크 |
| KLRI, DMA 규제 국제 동향 | 한국 규제는 거래소 주도, 미국/EU 대비 얇음. KRX는 스폰서드 액세스 없음 — DMA는 회원사 주문 시스템·회원사 검증 경유 | LS OpenAPI는 리테일/API 중개 — 거래소 대면 의무는 LS 부담. **글로벌 표준(15c3-5·RTS 6·SS5/18)을 설계 기준선으로, KRX 세트를 국내 확인으로** |

### RQ6. 운영 모범 규준

| 소스 | 요구사항 | 격차 매핑 |
|---|---|---|
| [FCA, "Algorithmic Trading Compliance in Wholesale Markets" (2018)](https://www.fca.org.uk/publication/multi-firm-reviews/algorithmic-trading-compliance-wholesale-markets.pdf) | 5영역: 알고 정의(**완전한 알고 인벤토리** — 거버넌스 밖 알고 금지), 개발·시험, 리스크 통제, 거버넌스·감독, 시장 행위. 킬스위치 절차는 알고별 문서화 + 주기 재검증 | G6 — 전략 on/off 상태는 내구·인벤토리·재시작 후 검증 대상 |
| [PRA SS5/18 (2018)](https://www.bankofengland.co.uk/prudential-regulation/publication/2018/algorithmic-trading-ss) | **킬스위치 최상세 감독 문서**: "거래를 정지하거나 접근을 차단하는 수동·자동 통제 + 재가동에는 수동 개입", 활성화 시 기존 주문 처리 방침, 소유권 지정, 주기적 작동 시험 | G1+G2를 쌍으로 직격 — 현 설계(수동 정지·자동 재가동)는 정반대 |
| [IOSCO FR332 (2010)](https://www.iosco.org/library/pubdocs/pdf/ioscopd332.pdf) + [FR31/2015](https://www.iosco.org/library/pubdocs/pdf/IOSCOPD522.pdf) | 사전 통제는 **실행 경로 안에**(자문이 아니라 강제), 거래소 변동성 통제·킬스위치·BCP | 10게이트 배치는 정합 — 경로 밖 자문 금지 원칙 확인 |
| 멱등성 엔지니어링 관행 (microservices/시스템 설계 문헌) | at-least-once가 기본 현실 — 주문 제출은 고유 멱등키(client order ID)를 비즈니스 상태와 **같은 트랜잭션에**, 타임아웃 ≠ 실패, UNKNOWN은 권위 측 조회 대사로만 해소(맹목 재시도 금지) | G4의 5개 단절 지점을 닫는 설계 사양 |

---

## 6. 영역 4 — 이벤트 인프라·관측성

조사 질문 6개, 검증 소스 15편. (핵심 주장은 §2-8, 2-9, 2-11에 통합 — 여기는 소스 명세와 보충)

### RQ1–2. Outbox·exactly-once·Redis 내구성

| 소스 | 핵심 주장 |
|---|---|
| [Richardson, "Pattern: Transactional Outbox"](https://microservices.io/patterns/data/transactional-outbox.html) | at-least-once + 멱등 소비자. relay는 "발행 후 기록 전 크래시 가능" — 정본 정의 |
| [Kleppmann, *DDIA* (2판 2025) Ch.11–12](https://www.oreilly.com/library/view/designing-data-intensive-applications/9781098119058/) | exactly-once = 내구·재생 가능 로그 + 멱등 처리. 내구 로그 없는 이중 쓰기는 유실 재도입 |
| [Confluent, "Transactions in Apache Kafka" (2017)](https://www.confluent.io/blog/transactions-apache-kafka/) | Kafka EOS = 멱등 프로듀서 + 복제·fsync 로그 위 트랜잭션 — 브로커 내구성이 하중 전제 |
| [Redis 지속성 공식 문서](https://redis.io/docs/latest/operate/oss_and_stack/management/persistence/) | 무지속 = 전부 소실. everysec = 최대 1초 유실. "PostgreSQL급 안전은 RDB+AOF 병용" |
| [Redis Streams 공식 문서](https://redis.io/docs/latest/develop/data-types/streams/) | 컨슈머 그룹 at-least-once는 **살아있는 프로세스 내에서만** — PEL도 메모리 상태 |
| [Jepsen, Redis-Raft (2020)](https://jepsen.io/analyses/redis-raft-1b3fbf6) | 복제 Redis(Sentinel/Cluster)도 확인된 쓰기 유실 허용. WAIT는 창을 좁힐 뿐 |

### RQ3–4. 관측성·데이터 품질

| 소스 | 핵심 주장 |
|---|---|
| [Google SRE Ch.6](https://sre.google/sre-book/monitoring-distributed-systems/) | 골든 시그널 4, 증상 우선 알림, 단순성 — 소규모는 작은 스택이 정답 |
| Hidalgo, *Implementing SLOs* (O'Reilly 2020) | 파이프라인 SLI = 신선도·완전성·정확성. 신선도 SLO("X보다 오래되지 않음")가 표준 첫 SLI |
| [OTel Collector 배포 패턴 + 안티패턴 (2024)](https://opentelemetry.io/docs/collector/deploy/gateway/) | 소규모에 no-collector/단일 에이전트 인정 — 콜렉터 함대는 안티패턴 |
| [Monte Carlo, 데이터 관측성 5기둥](https://www.montecarlodata.com/blog-introducing-the-5-pillars-of-data-observability/) + [Great Expectations](https://docs.greatexpectations.io/) | 신선도·볼륨·스키마·리니지·분포 — 신선도·볼륨이 최고 수익. 선언적 단언을 파이프라인 체크포인트로 |
| [Fowler, "Bitemporal History" (2021)](https://martinfowler.com/articles/bitemporal-history.html) + Snodgrass (1999) | 유효 시간/기록 시간 2축 — "실제 이력은 append-only가 아니나 **기록 이력은 append-only**" = PIT 정확성의 데이터 모델. `pit_provenance`가 기록 시간 축 — 백필·정정은 제자리 갱신이 아니라 새 기록 타임스탬프로 |

### RQ5–6. 구성·단일 호스트 복원력

| 소스 | 핵심 주장 |
|---|---|
| [12-Factor III·X](https://12factor.net/dev-prod-parity) | 구성-코드 분리, 배포당 단일 값, tools gap 경고 |
| [OWASP Secrets Management](https://cheatsheetseries.owasp.org/cheatsheets/Secrets_Management_Cheat_Sheet.html) | env는 전 프로세스 노출·로그 유출 — 시크릿은 관리 저장소, 중복 금지 |
| [Docker Compose healthcheck/depends_on/restart](https://docs.docker.com/reference/compose-file/services/) | `condition: service_healthy` + `restart: true` — 준비 안 된 의존 대상에의 크래시 루프 방지 표준 |
| [PgBouncer 공식 문서](https://www.pgbouncer.org/features.html) | transaction 풀링 = 트랜잭션 동안만 서버 연결 점유("연결당 2kB") — N 클라이언트 × M 슬롯 > 서버 한도의 정본 해법. 주의: 세션 기능(구버전 prepared statements, advisory lock, LISTEN/NOTIFY) 파손 |

보충: 23컨테이너 × ThreadedConnectionPool(0,4) = 잠재 92 vs Supabase 세션풀 15 — "네트워크 오류처럼 보이는 조회 실패"의 원인. 해법: PgBouncer transaction 모드(`default_pool_size ≤ 15`) 일원화 또는 Supabase 관리형 등가물인 **Supavisor transaction 포트(6543)** 활용 — 현재 `.env`의 6543 pooler가 이미 트랜잭션 모드이므로 저트래픽 20개 서비스의 per-컨테이너 maxconn 0/1 캡과 병행하면 재작성 없이 해소.

---

## 7. 영역 교차 합의 지점

1. **fail-closed 원칙의 비일관 적용이 시스템 최대 취약점.** NAV(마크 없으면 ValuationError)·리서치 MCP(도구 제거 실패 시 기동 거부)·supervisor(scope 위반 시 루트 block)는 fail-closed인데, 정확히 가장 중요한 세 곳 — 킬스위치(키 부재=ENABLED), QA 판정(미인식=WARN), 공장 프리필터(`_structurally_blocked` 예외=`{}` 통과) — 이 fail-open이다. **규제 영역과 인프라 영역이 독립 조사에서 같은 지점(킬스위치)을 1위로 지목**했다.

2. **Knight Capital이 두 영역에서 독립 소환.** 규제 영역(자동 정지 부재·경고 무대응·배포 검증)과 인프라 영역(구성 드리프트). `.env` last-wins, 스위치보드 메모리 리셋, 환경 3분기, §12.1의 사어 코드 목록 — 전부 같은 실패 계급. Knight의 $460M은 코드 버그가 아니라 **배포·상태·절차** 실패였다.

3. **"검증되지 않은 신뢰 경계 통과"가 4개 영역의 공통 문법.** LLM 계획(팬아웃), LLM 판정(WARN), LLM 가설(훈련 오염), 브로커 전달(SENT 마킹) — 각 영역 1순위 결함이 전부 같은 모양: 생산자의 산출물을 다음 단계가 검증 없이 신뢰. 처방도 같은 모양: 경계에 결정론 검증기.

4. **처방의 절반은 잠자는 부품의 배선.** eval_runner(플래그 OFF → 켜고 pass^k), Prometheus 계측(수집기만 추가), `ceo_task_planner.py`의 검증 패턴(실전 경로 승격), 복식부기 원장(대사 드리프트를 트립 트리거로 — 부기→통제 승격), `trial_family.THEMES`(테마 패밀리 재정의의 재료), platform_iam(G5 인증의 재료). 신규 발명보다 기존 자산의 활성화가 지배적이다.

5. **워커 계층(소형 모델)이 에이전트 신뢰성의 최약 고리라는 실측**(AgentBench)과 **환경 3분기**(dev 1.7B vs prod 14B, 도구 표면 상이)가 복합되면, 로컬에서 검증한 카드 행동이 AWS에서 재현된다는 보장이 이중으로 없다. AS-IS §13-3의 구조 결론을 문헌이 재확인.

---

## 8. 기존 references.md와의 차분

`references/references.md` 18편의 분포: 멀티에이전트 LLM 금융 프레임워크 13편(TradingAgents, FinMem, FinRobot, FinCon, Nexus, AlphaCast…), Agentic RAG·환각 서베이 3편, 통계 검증 2편(DSR SSRN, KBS 2024 CSCV 비교), 리스크 중심 감사 1편(2502.15865).

**공백 영역** (0편): 규제·안전 규범 전부, 인프라 신뢰성 전부, 거래비용·용량, 팩터 복제 위기, LLM 훈련 오염, LLM 판정자 편향, 백테스트 프로토콜(DSR 원전 제외), 포트폴리오 구성 이론.

이 공백은 우연이 아니라 증상이다 — 기존 목록은 "에이전트를 어떻게 만들까"에 답하고, 이번 조사는 "만든 에이전트 시스템을 어떻게 믿을 수 있게 하고 무엇으로 돈을 지킬까"에 답한다. 서비스 질의 다음 단계는 후자다.

**추가 권장 목록** — §3~§6의 소스 표가 그 자체로 추가 목록이다. 최소 코어 15편만 추리면: AHM 2019, Bailey et al. 2017(PBO), HXZ 2020, McLean-Pontiff 2016, Sarkar-Vafa 2025, AlphaAgent 2025, MAST 2025, LLM-Modulo 2024, τ-bench 2024, CaMeL 2025, SEC 15c3-5, RTS 6, SS5/18, SEC 34-70694(Knight), Richardson outbox + Redis 지속성 문서.

---

## 9. 로드맵 단계 매핑

서비스 목표 3단계와 돌파 지점의 대응:

| 로드맵 단계 | 선행돼야 할 돌파 지점 | 이유 |
|---|---|---|
| **전략 1호** (공장에서 신뢰 가능한 SUPPORTED 산출) | 1(지문), 4(비용), 6(다중검정), 7(오염), 10(로버스트니스 레그) | 이것들 없이 나온 SUPPORTED는 통계적으로 "진짜"라는 주장을 방어할 수 없다 — 특히 7(오염)은 사후 수리 불가 |
| **PAPER 전 부서 검증** | 2(킬스위치), 8(outbox), 9(관측성), 11(구성), 12(자동 트립·대사) | PAPER의 존재 이유가 2-7의 "유일한 결정적 증거"이므로 PAPER 인프라 자체가 신뢰 가능해야 함. 12는 실주문 전 완성 관문 |
| **사용자 질의 서비스** | 3(QA fail-closed), 5(팬아웃 가드), + eval 하네스(pass^k)와 카드 계약 타입화 | 질의 품질의 병목은 모델이 아니라 검증 경계 — MAST 3범주 전부가 여기 |
| **AWS 이전** | 11(환경 패리티·도구 표면 계약), 9(관측성 선행 배치) | "로컬에서 되는 카드"의 AWS 재현 보장이 현재 구조적으로 없음(AS-IS §13-3) |

---

*이 문서는 2026-08-13 시점의 문헌 조사와 AS-IS 스냅샷(HEAD `892973c`) 대조 결과다. 소스 URL은 조사 시점에 실재 검증됐다. 시스템 측 사실이 이후 커밋으로 바뀌면 해당 처방의 "현재 상태" 절만 무효화되고 문헌 근거는 유효하다. 우선순위는 종합 판단이며, 각 항목의 출처를 따라 독립 검증 가능하다.*
