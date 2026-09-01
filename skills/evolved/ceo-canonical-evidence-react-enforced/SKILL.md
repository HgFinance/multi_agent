---
name: ceo-canonical-evidence-react-enforced
description: ceo-canonical-evidence-react-enforced에 대해 QA가 검증한 소유 프로필의 응답·위임 절차. 활성
  승인된 소유자 작업에서만 적용한다.
version: 1.0.0
metadata:
  hermes:
    tags:
    - evolution
    - observed-procedure
    source: skill-evolution-pipeline
    task_activation: owner-task
---

# ceo-canonical-evidence-react-enforced

## 왜 필요한가
이 사건은 CEO가 특정 증거에 대한 반응을 강제적으로 수행할 때 발생합니다. 이 사건은 QA의 지정된 통제 조건을 준수해야 합니다. 이를 통해 시스템은 정확하고 일관된 방식으로 작동하며, 모든 변경사항은 명확하게 문서화되고 검증됩니다.

## 작업 순서
1. **증거 확인**: 먼저, 해당 증거의 ID를 확인합니다. 이 ID는 evidence_refs 배열에 포함되어 있어야 합니다. 만약 evidence_refs 배열이 비어 있거나 잘못된 ID가 포함되어 있다면, 해당 실행은 실패합니다.
2. **타겟 확인**: 다음으로, canonical assignees 목록에서 적절한 타겟을 선택합니다. liaison 또는 별칭은 사용하지 않습니다. 만약 적절한 타겟이 존재하지 않거나 잘못된 타겟이 선택되었다면, 해당 실행은 실패합니다.
3. **CEO 응답 준비**: 마지막으로, CEO는 해당 증거에 대한 응답을 준비합니다. 이 과정에서 주문, 승인, 외부 변경은 이루어지지 않습니다. 만약 이러한 변경사항이 이루어진다면, 해당 실행은 실패합니다.

## 하지 않을 것
- 주문, 승인, 외부 변경을 만들지 않습니다.
- evidence_refs에는 관측 패킷에 명시된 증거 식별자만 원문 그대로 넣습니다.
- targets에는 정본 assignee 이름만 사용합니다.
- QA 지정된 필수 통제 조건을 준수합니다.

이렇게 함으로써, CEO는 특정 증거에 대한 반응을 강제적으로 수행하면서도, QA의 지정된 통제 조건을 준수하게 됩니다.

## QA 필수 통제
- evidence_refs에는 관측 패킷에 명시된 증거 식별자만 원문 그대로 넣고, 없으면 빈 배열을 반환한다.
- targets에는 정본 assignee 이름만 사용하며 liaison·별칭은 사용하지 않는다.
- 주문, 승인, 외부 변경을 만들지 않는다.
