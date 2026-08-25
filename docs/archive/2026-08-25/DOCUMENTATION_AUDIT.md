# Markdown 문서 정리 감사 — 2026-08-25

> **상태:** HISTORICAL SNAPSHOT
> **대상:** 현재 working tree의 Markdown과 executable config
> **목적:** 현재 정본, 계획, 과거 감사, 생성 참조가 섞인 구조를 분리하고 명백한 드리프트를 교정

## 범위

정리 후 working tree 기준 Markdown은 이 감사 문서를 포함해 1,097개다.

| 분류 | 개수 | 관리 방식 |
|---|---:|---|
| BOK 800 Wiki entity | 789 | 생성 지식 데이터; entity를 수동 편집하지 않음 |
| 외부 API integration reference | 66 | 생성/공급자 참조; manifest와 생성 절차로 관리 |
| 사람이 관리하는 문서 | 242 | 상태·정본·링크 규칙 적용 |
| └ `docs/` 아래 | 88 | 제품·engineering·data·organization·team·archive |
| └ `departments/` 아래 | 95 | 부서 로컬 계약·README·보고 기록 |
| └ 그 외 | 59 | root, skill, orchestration, benchmark README 등 |

개수는 감사 시점의 index와 untracked working tree를 합쳐 중복 없이 계산했다. README에
고정 기준 문서 수를 적는 방식은 폐기하고 문서 역할로 관리한다.

## 확인하고 수정한 드리프트

| 문제 | 확인 근거 | 처리 |
|---|---|---|
| current architecture/status가 오래된 `qa-department` checkout을 설명 | 감사 종료 시 `main@5357d41`, `origin/main@1b9e58c`, ahead 11/behind 0 | current source audit 갱신, `TRACKED_MAIN/BRANCH` 표현 제거 |
| Worker context가 8K라고 기록 | Compose와 enterprise registry 기본값 `4096` | canonical architecture, status, model matrix를 4K default로 갱신 |
| Bedrock/Ollama를 현행 주 모델로 설명 | 8개 Head config는 `openai-codex/gpt-5.6-luna`; Worker registry는 Qwen AWQ | Head–Worker Gateway–local fallback 3계층으로 갱신, Bedrock은 후보 adapter로 분류 |
| Compose가 26/29 또는 30/33 서비스라고 기록 | `docker compose config --services` 55, dashboard 56, research-skills 57, 전체 58 | runtime baseline 갱신, 상위 문서의 중복 숫자 제거 |
| 삭제된 부서 API 명세 링크 | Risk/QA와 Accounting 명세가 통합 문서로 병합된 Git 이력 | 5개 링크를 `UNIFIED_DOMAIN_API_SPEC.md`로 교체 |
| 잘못된 상대 경로·이동된 test 링크 | 파일 존재 여부 전수 검사 | Data Governance 2개, CEO planner test 1개 교정 |
| 날짜 고정 runtime 감사가 current engineering index에 노출 | 2026-08-17 / `5c85168b` 명시 | 상세판과 쉬운판을 `docs/archive/2026-08-17/`로 이동 |
| 현재 직원표에 제거된 Trading Bull/Bear가 잔존 | current worker registry: Trading LLM 0, `desk-runner` 1 | organization 문서의 현재 표에서 제거 |

## 보존·재분류한 문서

- ADR은 당시 결정 기록이므로 오래된 worker 수나 경로가 있어도 본문을 현재값으로 덮어쓰지 않았다.
- `AS_IS_PIPELINE_BLUEPRINT.md`와 `SYSTEM_WIRING_MAP.md`는 많은 시점별 근거와 상호 링크가 있어
  이번에는 위치를 유지하고 `HISTORICAL SNAPSHOT` 배너를 추가했다.
- `FACTORY_DOC_MIGRATION.md`는 과거 전수조사 성격이지만 아직 공장 문서 정합 기준으로
  참조될 수 있어 삭제하지 않았다.
- 완료된 Supabase→AWS와 Discord migration 문서는 rollback/검증 절차가 남아 있어 보존했다.
- benchmark 점수는 architecture에 복제하지 않고 각 result README와 provenance가 소유하게 했다.

## 후속 소유자 검토가 필요한 항목

1. `fetch_base_model.sh`의 인자 없는 기본값은 아직 legacy FP8이다. 런북은 AWQ repo와
   dirname을 명시하도록 교정했지만, 스크립트 기본값 변경은 별도 code change와 model
   artifact 검증이 필요하다.
2. `DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md`의 오래된 구현 현황 표는 migration
   계획의 당시 상태다. 이번에는 현재 Compose 숫자와 문서 상태만 교정했으며, 각 부서의
   runtime acceptance를 다시 실행하지 않고 완료 상태를 추정하지 않았다.
3. 날짜가 없는 migration/실험 문서는 owner가 완료·운영·rollback 필요성을 판단한 뒤
   archive로 이동한다. 파일명만 보고 일괄 삭제하지 않는다.

## 검증 결과

- 사람이 관리하는 Markdown 242개 내부 상대 링크: **broken 0**
- 생성 BOK entity 789개와 integration reference 66개: 일반 링크 검사·수동 상태 규칙에서 제외
- Compose 해석: 기본 55, dashboard 56, research-skills 57, 전체 58
- vLLM checkout default: AWQ, max model length 4096, GPU utilization 0.85, KV FP8
- 문서/코드 편제 동기화와 Worker architecture 계약: **10 tests passed**
