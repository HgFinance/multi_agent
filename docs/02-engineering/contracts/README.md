# Runtime Contract Layers

`FINAL_RUNTIME_ARCHITECTURE.md`가 전체 Runtime의 상위 기준이고, 이 디렉터리의 JSON Schema는 경계별 실행 계약이다. 세 계약을 하나의 만능 패킷으로 합치지 않는다.

## 계약 경계

| 계약 | 송신자 → 수신자 | 목적 |
|---|---|---|
| `agent-task-context.v1` | CEO/Department Head/Kanban → Department Head/Runner | 어떤 Case의 어떤 Task를 누구에게 실행시킬지 전달 |
| `worker-context.v1` | Department Head ↔ Runner/Worker | 개별 Worker 실행 상태·advisory·재현 메타데이터 전달 |
| `agent-task-result.v1` | Worker/Department Head → Department Head/CEO/Kanban | 근거 기반 결과·결정·confidence·escalation 반환 |
| `department-handoff.v1` | Department Head/Profile → 다른 Department Head/Profile | 상황별 부서 연결, target Profile과 부서별 입력 계약 지정 |

## 공통 규칙

- `case_id`, `task_id`, `trace_id`는 사람이 읽는 opaque ID를 허용한다. 내부 DB primary key가 UUID인 경우에도 외부 표시 ID와 분리할 수 있다.
- Artifact는 원문 Prompt나 Secret을 담지 않고 `type`, `id`, `content_hash`와 필요한 `as_of`, `provenance_ref`, `acl_scope`만 참조한다.
- `model_version`은 Base Model, `adapter_version`은 Department LoRA 또는 `none`, `profile_version`은 실제 Worker Profile 버전이다. 계약 버전은 `schema_version`이다.
- Wire status는 `ESCALATED`를 사용한다. Risk/QA 내부 함수의 기존 `ESCALATE` 값은 경계 변환 시 `ESCALATED`로 매핑한다.
- Worker Context는 비구속적이다. Risk Engine, OMS, Ledger, Approval Service만 금융 상태를 확정하거나 변경할 수 있다.
- `decision`, `confidence`, `evidence_refs`, `escalate`는 `agent-task-result.v1`에 둔다. Worker Context의 `advisory.suggested_verdict`는 참고용이며 최종 결정이 아니다.
- 부서 간 연결이 필요할 때만 `department-handoff.v1`을 만들고, 그 `handoff_id`를 downstream Task와 Worker Context에 연결한다. 단순히 같은 부서 안에서 Worker를 호출할 때는 Handoff를 만들지 않는다.
- `department_input_contract`는 target Profile이 받을 입력 계약을 명시한다. 현재 지원 대상은 CEO, Research, Trading, Risk, Quant, Accounting, QA, HR이다.

## ID와 저장

ID는 패킷에 직접 넣고, 원본 내용·ACL·Provenance·대용량 결과는 Artifact Store/DB에서 조회한다. `content_hash`, `input_hash`, `output_hash`와 `trace_id`를 함께 저장해야 Replay와 중복 실행 검사가 가능하다.

이 계약은 Production 제출·Risk 승인·Ledger Posting 권한을 부여하지 않는다.
