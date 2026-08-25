# HgFinance 문서 관리 기준

> **상태:** CURRENT GOVERNANCE
> **검토일:** 2026-08-25 UTC
> **범위:** 저장소의 사람이 관리하는 Markdown. 생성 데이터와 외부 API 참조는 별도 규칙을 따른다.

## 1. 문서 상태

새 문서와 현재 기준 문서는 제목 바로 아래에 다음 상태 중 하나를 적는다.

| 상태 | 의미 | 기본 위치 |
|---|---|---|
| `CANONICAL CURRENT` | 현재 구조나 상태를 대표하는 정본 | `docs/` |
| `CURRENT REFERENCE` | 특정 영역의 현재 세부 계약·설명 | 주제별 폴더 |
| `RUNBOOK` | 실제 실행·복구 절차 | `docs/02-engineering/` |
| `TARGET / PLAN` | 아직 구현되지 않은 목표·로드맵 | `docs/01-product/` 또는 engineering plan |
| `ADR` | 당시 의사결정과 근거. 승인 후 본문을 현재값에 맞춰 고쳐 쓰지 않음 | `docs/02-engineering/adr/` |
| `HISTORICAL SNAPSHOT` | 날짜·커밋에 고정된 감사·측정·완료 기록 | `docs/archive/<date>/` 또는 해당 산출물 폴더 |
| `GENERATED REFERENCE` | 스크립트로 다시 생성하는 지식·외부 API 참조 | 생성기와 manifest가 정한 경로 |

`IMPLEMENTED`, `TEST_VERIFIED`, `RUNTIME_VERIFIED`는 구현 증거의 수준이며 문서
종류가 아니다. 계획 문서에 코드 예시가 있다고 해서 `IMPLEMENTED`로 바꾸지 않는다.

## 2. 정본과 충돌 해결

제품 의도와 현재 구현 사실은 한 줄의 우선순위로 섞지 않는다.

- 제품 범위·조직·통제 원칙은 `HEDGE_FUND_MASTER_PLAN.md`와 승인된 ADR이 소유한다.
- 현재 구현 사실은 현재 checkout의 executable code, Compose, migration, registry,
  schema와 테스트가 소유한다.
- 현재 구조 요약은 `CURRENT_PROJECT_ARCHITECTURE.md`, 준비 상태는
  `PROJECT_IMPLEMENTATION_STATUS.md`, 상세 실행 계약은
  `02-engineering/FINAL_RUNTIME_ARCHITECTURE.md`가 소유한다.
- 날짜가 붙은 감사, benchmark 결과와 ADR은 현재값을 덮어쓰지 않는다.
- 외부 실행 상태는 실제 API·DB·process·GPU 관측 없이는 `RUNTIME_VERIFIED`로 쓰지 않는다.

현재 checkout과 원격 branch가 다르면 현재 문서는 checkout을 설명한다. 원격 비교가
필요한 감사에서는 branch·commit·ahead/behind를 날짜와 함께 별도 기록한다.

## 3. 구조 규칙

- 같은 사실표를 여러 문서에 복사하지 않고 정본 링크를 둔다.
- 파일명에 날짜나 기준 commit이 있으면 현재 engineering index가 아니라 archive에서 찾게 한다.
- 완료된 migration은 운영 절차가 계속 유효할 때만 runbook에 남기고, 일회성 실행 기록은 archive로 옮긴다.
- `docs/06-integrations/`의 생성 API 참조와
  `benchmarks/quantization/knowledge/bok800_2026/wiki/entities/`의 789개 entity 문서는
  일반 설계 문서 수, 수동 상태 표기와 일반 링크 감사에서 제외한다.
- benchmark 결과는 해당 결과 디렉터리의 README와 provenance가 소유한다. 현재
  아키텍처 문서에 실행별 점수표를 복사하지 않는다.
- 링크는 대상 파일 기준의 상대 경로를 사용하고, 이동 시 참조를 같은 변경에서 갱신한다.
- 문서에 서비스 수, 모델 context, commit 같은 변동값을 적으면 근거 파일과 검토일을 함께 적는다.

## 4. 현재 문서 지도

| 질문 | 먼저 볼 문서 |
|---|---|
| 지금 시스템 구조는 무엇인가 | [CURRENT_PROJECT_ARCHITECTURE.md](CURRENT_PROJECT_ARCHITECTURE.md) |
| 무엇이 구현·검증됐는가 | [PROJECT_IMPLEMENTATION_STATUS.md](PROJECT_IMPLEMENTATION_STATUS.md) |
| Runtime·gateway·gate 계약은 무엇인가 | [FINAL_RUNTIME_ARCHITECTURE.md](02-engineering/FINAL_RUNTIME_ARCHITECTURE.md) |
| Worker 권한과 모델 배치는 무엇인가 | [WORKER_ROLE_BOUNDARIES.md](02-engineering/WORKER_ROLE_BOUNDARIES.md), [WORKER_MODEL_MATRIX.md](02-engineering/WORKER_MODEL_MATRIX.md) |
| 제품 목표와 장기 범위는 무엇인가 | [HEDGE_FUND_MASTER_PLAN.md](HEDGE_FUND_MASTER_PLAN.md) |
| 과거 시점의 구조는 어땠는가 | [archive/](archive/README.md) |

## 5. 변경 점검표

1. 문서의 상태와 검토일이 목적에 맞는가.
2. 현재 사실은 코드·설정·테스트로 확인했는가.
3. 계획, 구현, runtime 관측을 구분했는가.
4. 같은 표를 복사하지 않고 정본에 연결했는가.
5. 이동·삭제 후 상대 링크가 남아 있지 않은가.
6. generated/vendor 문서를 사람이 관리하는 문서처럼 일괄 수정하지 않았는가.
