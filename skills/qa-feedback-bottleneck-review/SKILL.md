---
name: qa-feedback-bottleneck-review
description: "QA Hermes가 redacted QA feedback 또는 Evolution skill proposal을 검토할 때 사용하는 advisory skill. 한 개의 finding을 사실·원인·재현성·병목 귀속으로 검토하고 승인 의견만 제시하며 승인·거부·설정 변경은 수행하지 않는다."
metadata:
  hermes:
    owner_profile: qa-department
    tags: [qa, observability, bottleneck, review]
---

# QA Feedback Bottleneck Review

QA Hermes에서 다음 marker가 있는 요청에만 사용한다.

- `[hgfinance-qa-feedback-request-v1]`: 하나의 redacted feedback artifact를 검토한다.
- `[hgfinance-skill-proposal-review-v1]`: 지정된 Evolution skill proposal의 hash와 계보를 검토한다.

일반 QA 대화, 원본 프롬프트/응답 분석, 주문 실행에는 사용하지 않는다. 요청 카드의
artifact 또는 proposal ID 하나만 처리하고 다른 pending 항목을 합치지 않는다.

## Feedback artifact 검토

1. 카드에 포함된 metadata, finding code, source/root/task lineage만 사실로 취급한다.
   원본 prompt, answer, provider payload, credential, 주문 데이터는 요청하거나 출력하지 않는다.
2. 관측 사실과 추론을 분리한다. `OBSERVED_PASS`는 검토 대상으로 취급하지 않는다.
3. `REVIEW_REQUIRED`와 `REVIEW_WORTHY`는 검토 상태일 뿐 승인된 수정이 아니다.
4. `CORRELATION_METADATA_MISSING`은 관측성 문제로 분류한다. request/root lineage가
   없으면 원인 부서를 단정하지 말고 source·role·시간창 집계와 추가 계측을 제안한다.
5. `LATENCY_ABOVE_THRESHOLD`는 성능 사건으로 분류한다.
   `latency_attribution_status=MEASURED`이고 `primary_bottleneck_department`가
   있을 때만 주요 병목과 담당 부서를 쓴다. `ceo-ingress`는 타이머 시작점일 수
   있지만 직접 증거 없이는 원인 부서가 아니다.
6. `D5_*`는 전용 regression 검토 대상으로 남긴다. 이 skill 또는 Evolution skill
   생성 경로로 우회하지 않는다.

검토 의견은 다음 중 하나만 선택한다.

- `승인 검토 권고`: actionable deviation과 측정 가능한 검증 방법이 모두 있다.
- `보류 권고`: 실패 단계, 영향, 재현성, 또는 소유자 근거가 부족하다.
- `거부 검토 권고`: finding이 모순되거나 중복되거나 actionable하지 않다.

응답은 다음 형식을 지킨다. 내부 enum은 먼저 자연어로 설명하고 필요한 경우에만
괄호 안에 표시한다.

```text
## ② QA Hermes 검토 결과
- 피드백 기록 ID: `<exact artifact id>`
**검토 의견:** 승인 검토 권고 | 보류 권고 | 거부 검토 권고
**근거 충족도:** 충분 | 부분 | 부족

### 확인된 사실
- 중복 없는 관측 사실 한두 개

### 아직 확인되지 않은 점
- 확인되지 않은 원인, 단계, 영향 또는 재현성

### 실행 제안
- 주요 병목: <측정된 담당 부서 또는 미확정>
- 공동 개선 대상: <workflow/observability owner 또는 없음>
- 관측 시작 지점: <timer origin; 원인 아님>
- 조치: <하나의 제한된 corrective action>
- 검증: <하나의 측정 가능한 verification>

### 관리자 판단 가이드
- 승인 다음 gate 또는 승인 전에 필요한 정확한 추가 증거
```

`승인 완료`, `거부 완료`, `적용 완료`라고 쓰지 않는다. QA Hermes는 review opinion만
작성하며, 실제 결정은 authorized gateway와 offline benchmark가 담당한다.

## Evolution skill proposal 검토

proposal ID가 포함된 경우 `/var/lib/evolution-skills/proposals/<proposal-id>/`의
`SKILL.md`, `diff.patch`, `provenance.json`, `state.json`만 읽고 다음을 대조한다.

- Discord 카드와 content/provenance/diff hash가 일치하는가
- 서로 다른 source artifact와 source run이 세 개 이상인가
- benchmark ID와 status가 모두 통과인가
- owner, version, parent, 경계와 deterministic validation이 일치하는가
- skill이 관측된 절차만 다루고 권한·설정·주문 실행을 재정의하지 않는가

하나라도 불일치하거나 proposal이 일반론·미검증 주장·위험한 실행 지시를 포함하면
`거부 검토 권고` 또는 `보류 권고`를 낸다. 파일을 수정하거나 promote하지 않는다.

```text
## ⑨-검토 QA Skill 제안 검증
skill_proposal_id=<exact id>
**검토 의견:** 승인 검토 권고 | 보류 권고 | 거부 검토 권고
**Hash 일치:** PASS | FAIL
**계보·소유자:** PASS | FAIL
**결정론 검증:** PASS | FAIL
### 변경 요약
- 재사용되는 검토 절차
### 해결하려는 QA 문제
- source artifact와 finding의 관계
### 남은 위험과 회귀 검증
- 제한된 위험과 정확한 검증 방법
### 관리자 판단 가이드
- 승인 또는 거부 전에 필요한 판단
```
