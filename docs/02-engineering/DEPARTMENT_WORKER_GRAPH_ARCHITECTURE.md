# 부서 실행 계층: Hermes 부서장과 Worker Graph

> 상태: 현행 구조 참조 · 최종 대조 2026-08-26

이 문서는 부서 실행 계층의 공통 구조와 권한 경계만 설명한다. 실행 Worker의 이름·개수·상태는 각 부서 `hermes/config.yaml`과 `employee_workers.py`, 통합 편제는 [Current Project Architecture](../CURRENT_PROJECT_ARCHITECTURE.md), 역할 경계는 [Worker Role Boundaries](WORKER_ROLE_BOUNDARIES.md), 모델·serving 값은 [Worker Model Matrix](WORKER_MODEL_MATRIX.md)가 소유한다.

이 구조는 Agent 자문·자동 전략 파이프라인에 적용한다. 로컬 고정 fixture 사용자의 명시적 PAPER 지시는 별도 `USER_DIRECTIVE` 경로이며, 권한과 admission 계약은 [ADR-0007](adr/0007-authenticated-user-paper-directive-authority.md)과 [Local Paper Runtime](LOCAL_PAPER_RUNTIME.md)을 따른다. 이 문서의 자동 전략 주문 제한을 해당 경로까지 확대 해석하지 않는다.

![0–7번 부서 전체 파이프라인 아키텍처](assets/whole_pipeline_0_7.png)

편집 원본은 [`whole_pipeline_0_7.svg`](assets/whole_pipeline_0_7.svg), PNG 재생성기는 [`render_whole_pipeline.py`](assets/render_whole_pipeline.py)다. 다이어그램의 고정 인원·모델 표기가 문서 정본과 다르면 정본을 우선한다.

## 실행 흐름

```text
요청 또는 task
  → CEO/오케스트레이터가 필요한 부서와 계약 선택
  → Department Head가 구조화 입력 전달
  → 결정론 runner·허용 tool 실행
  → 조건을 만족한 Worker Graph 실행
  → schema·근거·권한 검증
  → non-binding worker context 반환
  → Department Head가 종합·에스컬레이션 서술
```

모든 요청이 모든 부서를 고정 순서로 통과하지 않는다. CEO plan과 계약이 필요한 부서만 선택하며, 부서 간 전달은 `case_id`·`trace_id`와 버전이 있는 구조화 계약을 보존한다. 구체적인 handoff 계약은 [Unified Domain API Spec](UNIFIED_DOMAIN_API_SPEC.md)과 `orchestration/contracts/mas.py`를 따른다.

## 계층별 책임

| 계층 | 책임 | 금지 |
|---|---|---|
| Department Head | 입력 분해, 하위 결과 종합, 누락·충돌·에스컬레이션 서술 | 주문 제출, Risk 판정 변경, 원장 수정 |
| LLM Worker | 허용된 도구와 근거로 역할별 비바인딩 context 생성 | 바인딩 승인, 임의 도구 호출, 결정론 수치 재작성 |
| 임시 전략 Worker | 승인된 immutable 전략 Bundle을 요청 단위로 실행 | 전략 수정·자기 승격·주문 권한 획득 |
| Deterministic Runner/Gate | 계약 전이, 계산, Risk·QA·권한·상태 검증 | LLM 서술로 판정 변경 |
| HR Registry | Worker 활성화·비활성화·교체와 성과 검토 | Worker의 자기 승인, 통제 우회 |
| Model Gateway | 환경별 endpoint·모델 선택과 호출 관측 | 문서에 복사된 모델값을 런타임 정본으로 사용 |

Trading처럼 고정 LLM Registry 없이 요청 단위의 결정론 전략 Worker와 `desk-runner`만 사용하는 부서도 있다. 반대로 조건부 LLM Worker가 필요한 부서도 있다. 따라서 고정된 전사 인원표나 `모든 부서 = LangGraph + LLM` 공식을 이 문서에 두지 않는다.

## Worker Graph 계약

- 입력은 역할·허용 tool·output contract·근거 참조가 포함된 구조화 task다.
- 그래프는 허용된 tool 결과를 state에 넣고 Worker가 context를 만들게 한 뒤 schema와 근거를 검증한다.
- 재시도 횟수와 fallback은 실행 코드·환경 계약이 소유한다. 문서의 과거 기본값을 현재 상수로 사용하지 않는다.
- 실패, schema 불일치, 모델 또는 tool 장애는 성공으로 축소하지 않고 `DEGRADED`, `HOLD`, `REJECT` 또는 `ESCALATE` 중 계약이 정한 안전 상태로 남긴다.
- Worker 결과는 비바인딩이다. Risk 결정, Evidence QA 판정, 주문 admission, 원장 posting의 소유권은 각각의 결정론 서비스에 있다.

## 생명주기와 실행 기록

Registry 상태는 다음 의미로 사용한다.

- `active`: 기본 입력을 만족하면 실행 대상
- `conditional`: 명시된 사건·근거·운영 신호가 있을 때만 실행
- `paused`: 검토 중 신규 실행 제외
- `retired`: 신규 실행에서 제외하고 Replay·감사 이력만 보존
- 임시 Worker: 요청 계약으로 생성하고 해당 실행 수명 안에서만 사용

Profile의 역할명이나 호환 Alias는 실행 인원으로 세지 않는다. 실제 실행 여부는 Registry 상태·trigger와 실행 매니페스트로 확인한다.

Worker 실행 기록은 최소한 다음 필드를 보존한다.

`worker_id`, `role`, `tools`, `executor`, `provider`, `model`, `output_contract`, `attempts`, `status`, `input_hash`, `error`, `evidence_refs`.

## 권한과 PAPER 범위

- Agent·alpha·자동 전략 경로는 Worker나 Department Head가 Broker submit을 직접 수행하지 않는다. Risk·OMS·회계·QA의 결정론 경계를 통과해야 한다.
- 명시적 로컬 `USER_DIRECTIVE`는 로그인 계정이 아니라 고정 fixture 사용자와 명시적 Fund/Book grant를 사용한다. 활성화된 환경에서만 LS PAPER adapter로 갈 수 있으며 LIVE 주문은 없다.
- Replay는 외부 Broker 주문과 운영 원장 쓰기를 수행하지 않는다.
- 어느 경로든 불확실한 실패를 자동 승인으로 바꾸지 않는다.

## 구현 기준

- 편제·상태: 각 부서 `hermes/config.yaml`
- Worker/runner: 각 부서 `employee_workers.py` 및 결정론 service
- 부서 간 계약: `orchestration/contracts/mas.py`
- 모델 routing: `departments/worker_model_gateway.py`와 [Worker Model Matrix](WORKER_MODEL_MATRIX.md)
- 전체 현행 구조: [Current Project Architecture](../CURRENT_PROJECT_ARCHITECTURE.md)
