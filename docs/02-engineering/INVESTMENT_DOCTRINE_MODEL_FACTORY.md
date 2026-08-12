# Investment Doctrine Model Factory

> 문서 상태: 조건부 도입 설계 기준
> 적용 조직: 퀀트/백테스트본부 `QNT-08 Investment Doctrine & Model Engineer`
> 상위 기준: [Research-Quant Evidence-to-Strategy Framework](RESEARCH_QUANT_AGENTIC_FRAMEWORK.md)
> 권한 원칙: 투자철학 모델은 `Strategy Reviewer`와 `Research Lens`이며 주문·승격 권한이 없다.

## 1. 한 문장 정의

Investment Doctrine Model Factory는 공개·허가된 투자 자료에서 인물의 말투가 아닌 **검증 가능한 투자 원칙**을 추출하고, 이를 구조화된 Doctrine, 학습 Dataset, Fine-tuned Adapter와 독립 평가 결과로 만드는 퀀트 연구 Pipeline이다.

```text
투자자·운용사 자료
  -> 출처·권리·시점 검증
  -> 투자 원칙 추출
  -> InvestmentDoctrineV1
  -> Prompt/RAG Baseline 평가
  -> 필요할 때만 SFT/LoRA Fine-tuning
  -> Frozen Case 독립 평가
  -> DoctrineModelCandidateV1
  -> QA/Model Risk Gate
  -> Shadow Reviewer
```

이 Pipeline의 목적은 `버핏처럼 말하는 봇`을 만드는 것이 아니다. 예를 들면 `장기 경쟁우위`, `현금흐름의 질`, `안전마진`, `재무 레버리지 제한`을 각각 측정 가능한 평가 기준과 반증 조건으로 바꾸는 것이다.

## 2. 왜 별도 역할이 필요한가

현재 `QNT-06 ML Quant Researcher`는 가격·수익률 예측, Feature 기반 ML 모델과 Calibration을 담당한다. 투자철학 모델은 다음 업무가 추가로 필요해 성격이 다르다.

- 원문 사용권한과 인물 오인 위험 확인
- 문체와 정체성을 제거하고 투자 원칙만 추출
- 원칙을 평가 기준, 금지 조건, 시간 지평과 무효화 조건으로 변환
- 철학 충실도와 일반 금융 추론 능력을 동시에 평가
- 여러 Doctrine의 독립 의견과 Dissent 보존
- Persona 이름의 유명세가 아니라 사후 성과와 Calibration으로 비활성화

따라서 조건부 직원 `QNT-08 Investment Doctrine & Model Engineer`를 추가한다. QNT-08은 Training Job을 설계하고 제출하지만 GPU Shell, Model Registry 승격과 Production 배포를 직접 수행하지 않는다.

## 3. 역할과 책임 경계

| 역할 | 책임 | 금지 |
|---|---|---|
| `RES-08` Evidence Curator | 원문, Citation, Published/Observed Time, License와 Retraction 확인 | 철학 모델 학습 승인 |
| `QNT-08` Doctrine Model Engineer | Doctrine 추출, Dataset Spec, Training Plan, Candidate와 Model Card 생성 | 자기 Candidate 최종 검증·승격 |
| `QNT-02` PIT Dataset Engineer | 시점 분할, 중복 제거, Dataset Hash와 Leakage 검사 | Holdout을 Training에 재사용 |
| `QNT-04` Independent Validator | Frozen Case, Counterfactual, Robustness와 회귀 평가 | 작성자 설명만으로 실패 제외 |
| `QNT-06` ML Quant Researcher | Base Model·Adapter 기술 검토와 일반 ML 회귀 검사 | 투자철학의 정책 Owner 역할 |
| `QNT-07` Release Manager | Registry, Shadow Alias, Rollback과 Artifact Signing | QA·Risk·CEO Gate 우회 |
| `AI QA/감사본부` | Citation, 저작권·인물 오인, 환각, Model Risk와 승인 | PnL만으로 승인 |
| `Agent Workforce` | 역할·Skill Version, 비용·SLO와 채용 필요성 평가 | 성과 검증 없는 자동 조직 변경 |

## 4. Persona를 Doctrine으로 바꾸는 규칙

### 4.1 허용하는 것

- 투자자의 공개 서한과 직접 발언에서 반복되는 평가 원칙 추출
- 원칙을 이름 없는 내부 Doctrine ID로 정규화
- 같은 Case를 서로 다른 철학으로 검토하고 Dissent 비교
- 원문 Citation과 사용권한을 가진 짧은 교육 예제
- 팀이 직접 작성하고 QA가 검토한 합성 Training Pair

### 4.2 금지하는 것

- 살아 있는 인물이나 운용사의 말투, 목소리와 정체성 모방
- `OOO가 추천합니다`와 같은 제휴·보증 오인 표현
- 권리 확인 없이 책, 유료 보고서와 인터뷰 전문을 학습 Corpus로 저장
- 미래 결과를 알고 작성된 회고 자료를 과거 Case의 정답으로 사용
- 유명인의 이름을 Model 성과 대신 신뢰 근거로 사용
- Persona 다수결을 Risk 승인 또는 매매 Signal로 사용

내부 Model ID는 `buffett-model` 같은 인명 대신 `quality_compounder_v1`, `deep_value_balance_sheet_v1`, `trend_following_v1`처럼 철학과 Version을 표현한다.

## 5. `InvestmentDoctrineV1`

```yaml
doctrine_id: quality_compounder
version: 1
display_name: Quality Compounder
objective: 장기간 재투자 가능한 높은 자본수익률 기업을 찾는다.
applicable_asset_classes: [equity]
applicable_strategy_families: [long_short, relative_value, event_driven]
holding_horizons: [60d, 1y_plus]
evaluation_criteria:
  - criterion_id: durable_roic
    question: 자본수익률이 경기와 회계 조정 이후에도 지속되는가?
    required_metrics: [roic, operating_cash_flow, reinvestment_rate]
    minimum_evidence_tier: primary
prohibited_evidence:
  - uncited_social_post
  - current_constituents_used_in_historical_case
valuation_or_signal_rules: []
risk_constraints:
  - leverage_requires_explicit_stress
invalidation_conditions:
  - competitive_advantage_evidence_retracted
source_citations: []
usage_rights: internal_research_only
calibration_history_ref: null
```

Doctrine은 Prompt가 아니라 Versioned Policy Artifact다. Model, RAG와 Agent를 바꿔도 Doctrine ID와 평가 기준은 유지되어야 한다.

## 6. QNT-08 공식 산출물

| Artifact | 목적 |
|---|---|
| `DoctrineSourceManifestV1` | 출처, 저작권, 시점, Hash와 허용 용도 기록 |
| `InvestmentDoctrineV1` | 투자철학을 평가 가능한 정책으로 고정 |
| `DoctrineDatasetManifestV1` | Training/Validation/Frozen Test와 중복·PIT 검사 |
| `DoctrineTrainingPlanV1` | Base Model, SFT/LoRA 설정, 예산과 중단 조건 사전 등록 |
| `DoctrineModelCandidateV1` | Model·Adapter Hash와 Eval Artifact를 가진 후보 |
| `DoctrineModelCardV1` | 적용 범위, 제한, 데이터, 평가, 권리와 위험 공개 |
| `DoctrineReviewV1` | 특정 Research Case를 Doctrine으로 검토한 구조화 결과 |
| `DoctrineRetractionImpactV1` | 원문 정정·삭제가 Dataset과 Model에 미친 영향 |

### 6.1 `DoctrineReviewV1`

```yaml
review_id: dr_...
doctrine_id: quality_compounder
doctrine_version: 1
model_candidate_id: dmc_...
research_packet_id: rp_...
as_known_at: 2026-08-03T01:30:00Z
criterion_results:
  - criterion_id: durable_roic
    status: insufficient_evidence
    supporting_claim_ids: []
    opposing_claim_ids: []
    unanswered_questions: []
stance: abstain                 # supportive | opposing | mixed | abstain
horizon: 60d
hypothesis_seeds: []
invalidation_claim_ids: []
confidence_status: uncalibrated
limitations: []
```

`DoctrineReviewV1`에는 주문 Side, 수량, 목표 비중과 승인 상태를 넣지 않는다. QNT-01이 Review와 Research Claim을 받아 `HypothesisSpecV2` 후보로 변환하고, 이후 기존 Quant 검증 Pipeline이 처리한다.

## 7. Agent Workflow

```text
1. QNT-08이 Doctrine Build Case 접수
2. RES-08에 Source Manifest와 권리 검증 요청
3. 원문에서 Principle Candidate 추출
4. 중복·충돌·문체·정체성 표현 제거
5. InvestmentDoctrineV1 초안 생성
6. QA가 Citation과 Impersonation Risk 검토
7. Prompt + RAG Baseline으로 Frozen Case 평가
8. Fine-tuning 필요성 Gate
9. QNT-02가 Dataset Manifest와 PIT Split 고정
10. QNT-08이 TrainingPlan을 사전 등록
11. 격리 Doctrine Trainer가 SFT/LoRA Job 실행
12. QNT-04와 QA가 Candidate를 독립 평가
13. QNT-07이 통과 후보를 Shadow Reviewer로 등록
14. 결과·Drift·Retraction을 추적하고 Rollback
```

Hermes는 Case, 예산, Job 상태, 재시도와 승인을 관리한다. LangGraph는 Source Review, Doctrine Build, Dataset, Training, Evaluation과 Release Handoff 상태를 관리한다. 실제 Training과 Metric 계산은 Agent가 아닌 격리 Worker가 수행한다.

## 8. Fine-tuning 필요성 Gate

다음 순서로 가장 단순한 방법부터 비교한다.

1. `InvestmentDoctrineV1 + Prompt`
2. `InvestmentDoctrineV1 + RAG + Structured Output`
3. Few-shot Example 추가
4. SFT + LoRA/QLoRA Adapter
5. 충분한 Preference Pair가 있을 때만 DPO
6. 강화학습은 별도 ADR과 Reward 검증 전까지 보류

Fine-tuning은 다음 조건을 모두 만족할 때만 시작한다.

- Prompt/RAG Baseline이 고정 Eval에서 반복 실패한다.
- 최소 Dataset 규모와 독립 Frozen Test가 존재한다.
- Source License와 인물 오인 검토가 완료됐다.
- 학습으로 개선하려는 행동이 명확하고 측정 가능하다.
- Training Budget, Rollback과 Model Registry가 준비됐다.
- QNT-04와 AI QA가 평가 기준을 학습 전에 고정했다.

## 9. Dataset 설계

### 9.1 Training Record

```yaml
record_id: dtr_...
doctrine_id: quality_compounder
task_type: criterion_review
as_known_at: 2024-03-31T00:00:00Z
source_ids: [src_...]
source_available_at: 2024-03-20T09:00:00Z
rights_status: approved_internal_training
input:
  research_claim_ids: [claim_...]
  evidence_ids: [evidence_...]
expected_output:
  schema: DoctrineReviewV1
  criterion_results: []
quality_review_ids: [qa_...]
generator: human_or_approved_teacher
```

### 9.2 분할 규칙

- 동일 원문, 같은 사건과 의미상 중복 Case는 한 Split에만 둔다.
- Validation과 Test는 시간 순서로 Training보다 뒤에 둔다.
- 미래 성과를 언급한 회고 자료는 과거 시점 Input에서 제외한다.
- 공개된 이후 수정된 원문은 Version과 `available_at`을 분리한다.
- Frozen Test는 QNT-08과 Training Worker가 열람하지 못하게 권한을 분리한다.
- 같은 투자자 자료만 평가하지 않고 반대 철학, 불완전 Evidence와 `abstain` Case를 포함한다.
- 좋은 응답뿐 아니라 근거 부족, 철학 비적용과 충돌을 올바르게 거절하는 예제를 포함한다.

## 10. 기술 구현 경로

### 10.1 권장 기본 경로

| 계층 | 기술 | 역할 |
|---|---|---|
| Control Plane | Hermes Agent | QNT-08 Case, Queue, 예산, Retry와 Handoff |
| Workflow | LangGraph | Build·Train·Eval 상태, Checkpoint와 승인 대기 |
| Contract | Pydantic v2 + JSON Schema | Doctrine, Dataset, Review, Candidate 검증 |
| Teacher/Reviewer | Amazon Bedrock Claude | 원칙 추출, 합성 예제 초안과 평가 설명 보조 |
| Local Training | Transformers + PEFT + TRL + Datasets + Accelerate | SFT, LoRA/QLoRA와 선택적 DPO |
| Training Runtime | Docker Linux GPU Worker | 네트워크·Secret·운영 DB Write가 제한된 실행 |
| Local Serving | vLLM Multi-LoRA 또는 검증된 Ollama Adapter | Doctrine별 Adapter Routing |
| Registry | MLflow Model Registry + S3-compatible Object Storage | Version, Hash, Alias, Model Card와 Rollback |
| Evaluation | pytest + Hugging Face Evaluate + 자체 금융 Eval | Schema, Doctrine, Citation, 회귀와 성능 평가 |
| Observability | OpenTelemetry + MLflow Run | Dataset, Config, Cost, Metric과 Trace 연결 |

### 10.2 AWS Bedrock 경로

2026-08-03 확인 기준 Amazon Bedrock 공식 문서는 Fine-tuning 가능 모델 목록에 `Anthropic Claude 3 Haiku`를 포함하고 있으며 지원 Region은 `us-west-2`로 안내한다. 새로운 Claude 모델도 자동으로 Fine-tuning 가능하다고 가정하지 않고 Job 생성 전에 공식 지원 목록과 Quota를 다시 확인한다.

Bedrock 경로는 다음에 적합하다.

- 학습 데이터와 Model Artifact를 AWS 안에서 관리해야 한다.
- 운영 팀이 GPU Driver와 Training Cluster를 직접 관리하기 어렵다.
- 지원 Base Model과 Region, 비용이 요구사항을 만족한다.

Training Data는 S3에 Versioned JSONL로 저장하고, IAM Role, KMS와 선택적 VPC를 사용한다. Claude 3 Haiku Fine-tuning Dataset은 공식 Bedrock 대화 형식과 제한을 Contract Test로 검사한다.

### 10.3 Open-weight Adapter 경로

권장 시작점은 Base Model 하나에 Doctrine별 LoRA Adapter를 분리하는 방식이다. PEFT의 LoRA는 Base Weight를 고정하고 작은 Adapter만 학습하므로 철학별 Version과 Rollback을 관리하기 쉽다. TRL은 SFT와 DPO Trainer를 제공한다.

- 현재 `qwen3:14b`는 Quant 부서의 일반 Local Assistant로 유지한다.
- Qwen 계열 Adapter는 vLLM 호환성 Spike 후 Serving한다.
- Ollama `ADAPTER`를 사용할 때는 공식 지원 Architecture와 Base Model 일치를 반드시 검사한다.
- Ollama 공식 문서의 Safetensors Adapter 지원 목록에는 Llama, Mistral과 Gemma 계열이 명시되어 있으므로 현재 Qwen Adapter를 그대로 지원한다고 가정하지 않는다.
- Base Model, Tokenizer, Quantization과 Adapter Hash가 하나라도 다르면 Candidate를 로드하지 않는다.

### 10.4 권장 선택

초기에는 `Prompt/RAG Baseline -> Open-weight LoRA Adapter` 순서가 비용과 비교 실험에 유리하다. AWS 운영 표준이 확정되고 Claude 3 Haiku의 품질·Region·비용이 요구사항을 만족하면 Bedrock Custom Model을 Challenger로 비교한다.

## 11. 평가 표준

### 11.1 P0 품질 Gate

| 지표 | 기준 |
|---|---|
| Output Schema Validity | 100% |
| Material Claim Citation | 100% |
| PIT Violation | 0건 |
| Order/Position 권한 위반 | 0건 |
| Identity·Affiliation 오인 표현 | 0건 |
| Unsupported Fact를 `abstain`하지 않은 비율 | Baseline 이하 금지 |
| Frozen Test Contamination | 0건 |
| Dataset·Model·Adapter Hash Lineage | 100% |

### 11.2 모델 비교

- Doctrine Criterion별 Precision, Recall과 Abstention
- Counter-evidence가 추가됐을 때 결론 변경의 적절성
- 같은 의미의 표현 변화에 대한 일관성
- 일반 금융 지식·추론 능력 회귀
- Citation Correctness와 Evidence Coverage
- Forecast를 출력하는 경우 Brier Score와 Calibration Error
- ResearchPacket-to-Hypothesis 채택률과 중복 가설 비율
- Token, GPU 시간, 응답 지연과 비용
- Shadow 사용자 수정률과 Retraction Rate

PnL은 최종 참고 지표일 뿐 Model 승인 단독 기준이 아니다. 동일 철학이 우연히 유리했던 시장 구간과 실제 일반화 능력을 구분해야 한다.

## 12. 다중 Doctrine 운용

초기 후보는 인물이 아니라 서로 다른 투자 가설 계열로 구성한다.

| Doctrine 후보 | 주로 보는 것 | 대표 반론 |
|---|---|---|
| `quality_compounder` | 자본수익률, 재투자, 현금흐름의 질 | 높은 가격과 경쟁우위 약화 |
| `deep_value_balance_sheet` | 자산가치, 재무 안전성, 할인율 | Value Trap과 Catalyst 부재 |
| `event_driven_catalyst` | 공시·실적·기업행동과 일정 | 조건 불성립과 Deal Break |
| `trend_following` | 다중 기간 추세와 변동성 | 횡보장 Whipsaw |
| `relative_value` | Pair·Basket Spread와 공통 Factor | 관계 붕괴와 Borrow Cost |
| `macro_regime` | 금리·환율·유동성과 Cross-asset 전달 | Regime 오분류와 정책 반전 |
| `volatility_risk_premium` | IV/RV, Skew, Term Structure | Tail Risk와 유동성 붕괴 |

동일 Case에서 각 Doctrine은 다른 Doctrine의 결과를 보지 않고 `DoctrineReviewV1`을 제출한다. Supervisor는 다수결로 결론을 만들지 않고 공통 Claim, 충돌, 적용 불가와 Evidence Gap을 보존한다.

## 13. 상태 머신

```text
SOURCE_INTAKE
  -> RIGHTS_VERIFIED
  -> DOCTRINE_DRAFTED
  -> DOCTRINE_APPROVED
  -> BASELINE_EVALUATED
  -> NO_TUNING_NEEDED | DATASET_CERTIFIED
  -> TRAINING
  -> INDEPENDENT_EVALUATION
  -> REJECTED | SHADOW_CANDIDATE
  -> QA_REVIEW
  -> SHADOW_ACTIVE
  -> SUSPENDED | RETRACTED | ROLLED_BACK
```

`NO_TUNING_NEEDED`는 실패가 아니다. Prompt/RAG가 같은 품질을 더 싸고 투명하게 제공하면 Fine-tuning을 하지 않는 것이 올바른 결과다.

## 14. 구현 단계

### Phase IDM-0: Doctrine Contract

- `QNT-08` Hermes Profile과 Tool Allowlist 등록
- `InvestmentDoctrineV1`, `DoctrineReviewV1` Pydantic Schema 작성
- `quality_compounder`, `event_driven_catalyst` Fixture 작성
- Source Rights와 Citation Gate 연결
- Prompt/RAG Baseline 구현

### Phase IDM-1: Dataset과 Eval

- `DoctrineDatasetManifestV1`, Temporal Split과 Decontamination 구현
- Frozen Case Suite와 `abstain`, Counter-evidence Case 구축
- Baseline Eval과 비용 기록
- Fine-tuning 필요성 Gate 자동 판정

### Phase IDM-2: Adapter Training Spike

- PEFT + TRL 기반 SFT/LoRA 격리 Worker
- Dataset, Config, Seed, Dependency와 Artifact Hash 고정
- MLflow Run과 Model Registry 등록
- vLLM 또는 검증된 Ollama Adapter Serving 비교
- Bedrock Claude 3 Haiku Customization을 선택적 Challenger로 비교

### Phase IDM-3: Shadow Reviewer

- ResearchPacketV3를 입력으로 `DoctrineReviewV1` 생성
- QNT-01에 Evidence-linked `hypothesis_seeds` 전달
- QNT-04·QA 독립 평가와 Drift Monitor 연결
- AI Office에 Doctrine 비교, Dissent와 Model Card 표시
- Retraction과 Rollback Drill 수행

## 15. 채용·도입 Trigger

QNT-08은 P2 조건부 역할이다. 다음 조건 중 하나 이상이 확인될 때 Runtime 구현과 GPU 예산을 승인한다.

- 서로 다른 Doctrine 3개 이상을 반복 평가해야 한다.
- Prompt/RAG Baseline이 고정 Eval 기준을 반복 실패한다.
- QNT-06의 Alpha ML 연구가 Doctrine Dataset·권리 검토 때문에 지연된다.
- 사용자에게 지속적인 철학 기반 Review와 비교 설명이 필요하다.
- 충분한 권리 검증 Dataset과 Frozen Test가 확보됐다.

Trigger 전에는 QNT-06의 단기 Spike로 Dataset 규모, 품질 개선 폭과 비용을 측정한다. QNT-08을 채용한다고 해서 모든 Doctrine을 Fine-tuning하는 것은 아니다.

## 16. 완료 정의

- 인물 자료가 Citation과 Usage Rights를 가진 `DoctrineSourceManifestV1`로 관리된다.
- 인물 이름과 말투 없이 동일 원칙을 `InvestmentDoctrineV1`로 재현할 수 있다.
- Prompt/RAG Baseline보다 Fine-tuned Candidate가 Frozen Test에서 유의미하게 개선된다.
- Dataset, Base Model, Adapter, Eval과 Model Card가 Hash로 연결된다.
- QNT-08이 자신의 Candidate를 승인하거나 Registry Alias를 변경할 수 없다.
- Doctrine Review에서 Research Claim과 Evidence까지 역추적된다.
- Review는 Strategy Hypothesis 후보만 만들며 주문·비중·승인을 생성하지 않는다.
- QA 실패, Drift 또는 Source Retraction 시 Shadow 중단과 Rollback이 가능하다.
- 여러 Doctrine의 충돌이 평균이나 다수결로 삭제되지 않는다.

## 17. 공식 기술 참고

- [Amazon Bedrock Fine-tuning 지원 모델](https://docs.aws.amazon.com/bedrock/latest/userguide/custom-model-fine-tuning.html)
- [Amazon Bedrock Fine-tuning Data 준비](https://docs.aws.amazon.com/bedrock/latest/userguide/model-customization-prepare.html)
- [Hugging Face PEFT LoRA](https://huggingface.co/docs/peft/main/conceptual_guides/lora)
- [Hugging Face TRL SFT Trainer](https://huggingface.co/docs/trl/en/sft_trainer)
- [Hugging Face TRL DPO Trainer](https://huggingface.co/docs/trl/dpo_trainer)
- [vLLM LoRA Adapter Serving](https://docs.vllm.ai/en/stable/features/lora/)
- [Ollama Modelfile Adapter](https://docs.ollama.com/modelfile)
- [MLflow Model Registry](https://mlflow.org/docs/latest/ml/model-registry/)
