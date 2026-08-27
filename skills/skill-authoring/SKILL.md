---
name: skill-authoring
description: "반복 실행에서 관측된 병목을 검증 가능한 Hermes 스킬로 제안하고, QA·소유자 승인 뒤 정본에 승격하거나 기존 스킬을 진화·퇴역시킬 때 사용한다. 일회성 사고, 미실행 아이디어, 코드 복제, 사용 횟수만으로 한 삭제에는 사용하지 않는다."
metadata:
  hermes:
    tags: [meta, autonomy, knowledge, self-improvement]
    related_skills: [wiring-audit, dataset-engineering, autonomous-quant-research]
---

# 스킬 작성과 진화

## 사용 조건

다음 중 하나를 만족할 때만 스킬 후보를 만든다.

- 같은 종류의 문제가 서로 다른 source run ID에서 3회 이상 관측됐다.
- 기존 절차가 있는데 발견 경로가 없어 반복해서 “없음”으로 오진됐다.
- 활성 스킬의 낮은 성과가 서로 다른 실행에서 반복되어 새 버전이 필요하다.

다음은 스킬 후보가 아니다.

- 한 번 발생한 사고 또는 아직 실행하지 않은 아이디어
- 코드나 정본 문서를 읽으면 바로 알 수 있는 구현 상세
- 다른 부서의 프로필·권한·승인 절차를 바꾸는 지시
- 세션 호출 횟수만을 근거로 한 삭제 또는 비활성화

## 작성 원칙

1. 증상과 실제 오류 문구로 시작한다.
2. 관측된 사실, 정본 경로, 복사해 실행할 수 있는 검증 명령만 기록한다.
3. 코드 전체를 복사하지 않는다. 구현과 문서가 서로 다른 정본이 되면 안 된다.
4. 실행 가능한 리소스가 있으면 실제 환경에서 성공·실패 경로를 모두 검증한다.
5. 하지 않을 것과 안전한 대체 경로를 함께 적는다.
6. 진입 문서는 짧게 유지하고 상세 자료는 `references/`로 분리한다.

## 운영 흐름

QA finding은 QA Hermes 검토와 관리자 1차 승인, baseline reproduction benchmark
PASS를 거친 뒤에만 운영 occurrence 원장으로 들어간다. `SKILL_CREATE`와
`SKILL_EVOLVE`가 아닌 유형은 Skill 파이프라인으로 보내지 않는다. 모델이나 제안
Worker가 저장소 `skills/`를 직접 수정하지 않게 한다.

```bash
python3 scripts/evolution_skills.py ingest \
  --department 01-research --input /path/to/occurrences.jsonl
python3 scripts/evolution_skills.py propose --department 01-research --dry-run
python3 scripts/evolution_skills.py status
```

후보 생성은 운영 정본 `qwen2.5-14b-instruct-awq`만 사용한다. 결정론적 구조,
경계, provenance 검증을 통과하면 Discord에 본문·provenance hash와 원인 artifact를
담은 2차 승인 카드를 게시한다. 이 승인 전에는 자동 활성화하지 않는다.

```bash
python3 scripts/evolution_skills.py approve <proposal-id> \
  --approved-by <reviewer> --qa-verdict PASS
python3 scripts/evolution_skills.py promote <proposal-id>
python3 scripts/evolution_skills.py validate
```

운영 승격은 모델 자격증명이 없는 `control-daemon`만 수행한다. 승격기는
`skills/evolved/<slug>/`와 `skills/evolution-registry.json`을 함께 갱신하고
회귀 검증 실패 시 파일을 이전 snapshot으로 복구한다. 제안 Worker의 공유 스킬
마운트는 읽기 전용이어야 한다.

```bash
python3 scripts/evolution_skills.py report <proposal-id>
```

활성화 직후 상태는 `ACTIVE_PENDING_FEEDBACK`이다. 독립 운영 실행 3건의 성과가
검증되기 전에는 문제를 해결했다고 표현하지 않는다.

## 변경과 퇴역

- 활성 스킬을 덮어쓰지 않고 새 버전 제안으로 진화시킨다.
- 구버전은 `SUPERSEDED`, 사용 중단은 `RETIRED`로 기록하고 계보와 소스를
  보존한다.
- 퇴역에는 등록된 소유자의 승인과 활성 대체 스킬이 필요하다. 대체가 없으면
  소유자의 명시적 무대체 승인을 기록한다.
- bundled, project-owned, evolved, generated-cache, legacy-custom을 먼저
  분류한다. provenance가 없다는 이유만으로 기존 스킬을 삭제하지 않는다.

상태 전이, provenance 필드, 발생원, 성과 되먹임, 검증 명령은
[Evolution Skills 운영 계약](references/evolution-lifecycle.md)을 따른다.

## 금지 사항

- 관측하지 않은 결과, 열지 않은 경로, 실행하지 않은 명령을 쓰지 않는다.
- LLM 생성물을 QA·소유자 승인 없이 정본에 승격하지 않는다.
- 다른 부서의 SOUL, config, persona 또는 권한 경계를 재정의하지 않는다.
- 호출 횟수 0, 세션 기록 부재, provenance 부재만으로 삭제하지 않는다.
- 실패한 검증을 성공으로 바꾸거나 이전 버전을 지워 이력을 숨기지 않는다.
