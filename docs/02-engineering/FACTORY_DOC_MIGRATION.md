# 문서 전수 조사 - 공장 프레임워크 정합

담당: 재일 (리서치본부 RES + 퀀트·백테스트본부 QNT)
작성: 2026-08-10
대상: 저장소 전체 마크다운 243개

코드는 이미 공장으로 바뀌었는데 문서는 전략 중심 서술이 남아 있다. 문서가 코드와
다르면 **문서가 아니라 오해의 원천**이 된다. 이 문서는 무엇을 고치고 무엇을 그대로
두는지, 그리고 왜 그런지를 정한다.

---

## 0. 무엇이 바뀌었나 - 고칠 때 적용할 기준

옛 서술과 공장 서술의 차이는 문장 몇 개가 아니라 **모델**이다. 아래 대조표가
모든 수정의 기준이다.

| | 옛 서술 | 공장 서술 |
|---|---|---|
| 프레임워크의 역할 | 프레임워크가 투자를 판단한다 | **프레임워크는 전략을 생산한다.** 판단은 승격된 전략이 한다 |
| 리서치 산출물 | 리포트·종목 분석·투자의견 | `MethodologyLeadV1` -> `ExperimentProposalV1` (**가설 공급**) |
| 퀀트 산출물 | 백테스트 성적 | `ExperimentCardV1` + `ExperimentOutcomeV1` (**성공도 실패도 환류**) |
| 루프 | 리서치 -> 퀀트 일방통행 | 가설 -> 실험 -> 결과 -> 폐기 or 승격 -> **다음 가설에 반영** |
| 역할 분리 | 에이전트가 판단 | [코드]=결정론 판정 / [에이전트]=제안·서술 / [인간]=자본 걸린 긍정 판정의 서명 |
| 과적합 방어 | 사후 점검 | trial_family(분모) -> DSR(문턱) -> PBO/CSCV(선택 절차) 3층 |
| 누수 방지 | 사후 검사 | `PITView` - 기준일 초과 데이터를 **꺼낼 접근자가 없다** |

부수 원칙 (문서 전반에 일관되게 적용):

- **미측정은 0 이 아니다.** 안 잰 값은 `None`, 행 0건은 PASS 아님, `NOT_RUN` 은 통과 아님
- **막는 것은 기계가 즉시, 여는 것은 사람이 서명 후** (비대칭)
- 자유 서술 교훈은 대조가 안 되므로 **통제 어휘**만 쓴다

---

## 1. 손대지 않는 것 (150개)

고치는 것이 오히려 사고인 부류다.

| 분류 | 개수 | 그대로 두는 이유 |
|---|---|---|
| `*/reports/` | 51 | **과거 기록**이다. 그때 그렇게 판단했다는 사실이 남아야 한다. 지금 프레임워크로 고쳐 쓰면 기록 위조다 |
| API 레퍼런스 (LS·KRX·SerpAPI·OpenDART) | 65 | 남의 스펙이다. 공장과 무관하고 우리가 고칠 권한도 없다 |
| `03-risk/experiments/llm_wiki/**` | 28 | 리스크본부 실험 코퍼스 - 문서가 아니라 데이터다 |
| `docs/02-engineering/adr/` | 6 | ADR 은 **결정 시점의 판단**을 남기는 형식이다. 뒤집혔으면 새 ADR 을 쓰지 기존 것을 고치지 않는다 |
| `FINAL_RUNTIME_ARCHITECTURE.md` | 1 | 재일님 지시로 제외 |

---

## 2. 1층 - 공통 프레임워크 문서

회사 전체가 읽는다. **여기가 안 맞으면 나머지가 다 어긋나므로 먼저 고친다.**

정합성 점검에서 옛 서술 흔적이 검출된 순서:

| 파일 | 흔적 | 무엇을 고쳐야 하나 |
|---|---|---|
| `02-engineering/RESEARCH_QUANT_AGENTIC_FRAMEWORK.md` | 5 | 공장의 정본 문서다. 7.1절 단계 정의와 계약 이름이 코드(`factory_contracts.py`)와 일치하는지 전수 대조 |
| `02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md` | 3 | 부서별 산출물 서술이 리포트 기준이다 -> 계약 기준으로 |
| `01-product/MULTI_AGENT_TRADING_COMPETITIVE_ANALYSIS.md` | 3 | 선행 연구를 "프레임워크가 판단" 기준으로 비교했다. **우리와 그들의 차이가 바로 이 지점**이므로 대조축을 공장으로 바꾸면 분석의 결론이 더 선명해진다 |
| `02-engineering/RESEARCH_OUTPUT_ADVANCEMENT_STRATEGY.md` | 2 | 제목부터 "리서치 아웃풋"이다. 가설 공급 관점으로 재작성 |
| `02-engineering/DEPARTMENT_WORKER_GRAPH_ARCHITECTURE.md` | 2 | 워커 편제가 옛 편제다. 리서치 8명(스카우트 4 렌즈 포함)·퀀트 5명 반영 |
| `02-engineering/WORKER_ROLE_BOUNDARIES.md` | 1 | 역할 3분류(코드/에이전트/인간) 명시 |
| `02-engineering/REPOSITORY_DEPARTMENT_STRUCTURE.md` | 1 | 신설 모듈 경로 반영 |
| `02-engineering/HEDGE_FUND_IMPLEMENTATION_BACKLOG.md` | 1 | 완료·폐기 항목 정리 |
| `01-product/MINIMUM_SERVICE_UNIT_SPEC.md` | 1 | 최소 서비스 단위가 "리포트"면 공장과 어긋난다 |
| `02-engineering/OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md` | 1 | 모델 서술만 - 경미 |

흔적이 안 잡혔어도 **모델 수준에서 봐야 하는 것**:
`01-product/HEDGE_FUND_CORE_PLAN.md`(회사 정본), `02-engineering/MAS_PIPELINE_CONTRACTS.md`
(계약 정본), `02-engineering/INVESTMENT_DOCTRINE_MODEL_FACTORY.md`,
`02-engineering/WORKER_SKILL_REGISTRY.md`, 루트 `README.md` / `AGENTS.md` / `CLAUDE.md`.

---

## 3. 2층 - 리서치·퀀트 본부 문서

내 소관이고 **코드가 이미 바뀌었으므로 문서만 따라가면 된다.** 대조 대상 코드:

```
departments/01-research/contracts/factory_contracts.py   계약 3종
departments/01-research/factory/publish_gate.py          발행 게이트
departments/01-research/factory/lead_intake.py           리드 적재
departments/01-research/factory/proposal_intake.py       기획안 조립 + 독립 회의론자
departments/04-quant-backtest/pipeline/factory_bridge.py Gate 0 + 환류
departments/04-quant-backtest/pipeline/data_resolution.py 원천->데이터셋 사상
skills/{methodology-scout,experiment-factory}/SKILL.md   에이전트 절차
```

---

## 4. 3층 - 타 부서 문서

**부서 내부 절차는 그 부서 소관이다.** 여기서 바꾸는 것은 딱 하나 -
"공장에서 당신 부서의 입력과 출력이 무엇인가".

| 부서 | 입력 | 출력 |
|---|---|---|
| 트레이딩 | `SUPPORTED` 전략 후보 | PAPER 승격 여부 (Bull/Bear 토론) |
| 리스크 | 승격 후보 | 수용력·한도 판정 |
| QA·감사 | 실험 카드 | 재현 검증 |
| CEO 오케스트레이션 | 각 관문 판정 | 예산·시도 증액 결정 |

---

## 5. 진행 상태

- [x] 전수 조사 (243개 분류)
- [ ] 1층 공통 문서
- [ ] 2층 리서치·퀀트
- [ ] 3층 타 부서 (입출력 절만)

한 번에 다 고치면 중간에 끊겼을 때 문서가 반은 옛 프레임워크, 반은 공장이 되어
지금보다 나빠진다. **층 단위로 끊어서** 진행하고 이 표를 갱신한다.
