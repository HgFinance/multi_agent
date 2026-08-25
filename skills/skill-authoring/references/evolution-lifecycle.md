# Evolution Skills 운영 계약

## 상태 전이

```text
Occurrence
  -> Candidate (서로 다른 source run ID 3개 이상)
  -> PROPOSED
  -> VALIDATED (결정론적 구조·경계·provenance 검사)
  -> APPROVED (QA PASS + 이름이 기록된 검토자)
  -> ACTIVE (정본 소스·registry 동시 등록)
  -> SUPERSEDED | RETIRED
```

QA FAIL은 `REJECTED`다. 모델 생성 실패와 검증 실패는 정본을 변경하지 않는다.
구버전을 덮어 지우지 않고 제안 원장에 `SUPERSEDED` 계보를 남긴다.

## 저장 경계

- 운영 상태: `$EVOLUTION_SKILLS_HOME` 또는
  `~/.hermes/evolution-skills/`
- 사건: `occurrences.jsonl`
- 전이 감사 원장: `events.jsonl`
- 성과 신호: `feedback.jsonl`
- 제안: `proposals/<proposal-id>/{SKILL.md,provenance.json,state.json}`
- 정본: `skills/evolved/<slug>/{SKILL.md,provenance.json}`
- 정본 등록부: `skills/evolution-registry.json`

제안 워커에는 운영 상태 경로만 쓰기 권한을 준다. `skills/`는 읽기 전용으로
마운트하고, 승인 후 호스트 control-plane 명령만 정본을 바꾼다.

## 사건 발생원

- 현재 운영 발생원은 LangSmith feedback bridge다. Research·Quant trace의
  결정론적 finding만 소유 부서 occurrence로 변환한다.
- 새 발생원은 `append_occurrences_to_path()` 또는 `EvolutionSkillStore`를 통해
  같은 원장·중복 제거 계약을 사용해야 한다.
- 같은 `department + kind + source run ID`는 동시 기록돼도 한 번만 센다.
- `scripts/evolution_skills.py daemon`이 후보와 제안만 만들며 승인·승격은 하지
  않는다. 구형 `agents/skill_forge.py` 실행 경로는 사용하지 않는다.

## provenance 필수값

evolved 스킬은 다음을 반드시 가진다.

- `classification: evolved`
- owner profile과 authoring department
- 정수 버전과 부모 버전
- 원인이 된 서로 다른 source run ID 3개 이상
- 생성 모델 `qwen2.5-14b-instruct-awq`
- 생성·승인·활성 시각, 승인자, QA 판정
- 검증된 콘텐츠 SHA-256

기존 bundled 또는 project-owned 스킬에 가짜 생성 이력을 소급해 쓰지 않는다.
`inventory`에서 별도 분류하고 `legacy-custom`은 provenance 미확인 상태로
보존한다.

## 운영 명령

```bash
# 자동 수집 상태와 제안 확인
python3 scripts/evolution_skills.py status
python3 scripts/evolution_skills.py propose --department 01-research --dry-run

# 현재 소스 분류 감사(삭제하지 않음)
python3 scripts/evolution_skills.py inventory

# 검토와 활성화
python3 scripts/evolution_skills.py approve <proposal-id> \
  --approved-by <name> --qa-verdict PASS
python3 scripts/evolution_skills.py promote <proposal-id>
python3 scripts/evolution_skills.py validate

# 성과 기록
python3 scripts/evolution_skills.py feedback --slug <slug> --version <n> \
  --run-id <run-id> --score <0..1> --detail <요약>

# 퇴역: 둘 중 하나가 필수이며 파일은 삭제하지 않는다
python3 scripts/evolution_skills.py retire --slug <slug> \
  --approved-by <owner> --owner-profile <owner-profile> --replacement <active-slug>
python3 scripts/evolution_skills.py retire --slug <slug> \
  --approved-by <owner> --owner-profile <owner-profile> \
  --owner-approved-no-replacement
```

`inventory`의 사용·세션 정보는 품질 신호일 뿐 생존 여부를 결정하지 않는다.
자동 삭제 명령은 제공하지 않는다.
