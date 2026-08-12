---
target: ai-office/app/page.tsx
total_score: 34
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 1
timestamp: 2026-08-05T08-43-05Z
slug: ai-office-app-page-tsx
---
## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|---|---:|---|
| 1 | Visibility of System Status | 4 | 요약·Gate는 먼저 보이고 단계·직원·이벤트는 접혀 있어 읽기 순서가 좋아졌습니다. |
| 2 | Match System / Real World | 3 | CEO·부서·직원 모델은 명확하지만 PIT·BFF·Registry는 여전히 전문 용어입니다. |
| 3 | User Control and Freedom | 3 | 상세 접기와 PIT 복구 버튼은 좋지만 Operations에서 Mandate로 이동한 뒤 돌아오는 경로가 약합니다. |
| 4 | Consistency and Standards | 4 | 상태 pill과 승인 상태가 공통 한글 라벨 체계로 통일됐습니다. |
| 5 | Error Prevention | 4 | 재실행은 저장된 Mandate와 기존 BFF 경로를 사용하고, 자동 통과·가짜 데이터 생성이 없습니다. |
| 6 | Recognition Rather Than Recall | 3 | 오류 요약·반복 횟수는 좋아졌지만 raw event identity와 일부 전문 용어는 남습니다. |
| 7 | Flexibility and Efficiency | 3 | 부서 선택·범위 필터·세부 접기는 있지만 문제 부서 바로가기와 검색은 없습니다. |
| 8 | Aesthetic and Minimalist Design | 3 | 중복과 세로 길이는 줄었지만 metric·Gate·recovery가 한 run panel에 함께 남습니다. |
| 9 | Error Recovery | 4 | PIT에 데이터 새로고침·Mandate 확인·분석 재시작이 연결됐습니다. |
| 10 | Help and Documentation | 3 | 다음 조치가 보이지만 PIT 용어와 데이터 준비 조건의 짧은 설명은 더 줄일 수 있습니다. |
| **Total** |  | **34/40** | **Strong — 핵심 운영 흐름은 안정됐고, 복구 발견성과 전문 용어가 다음 과제입니다.** |

## Design Specificity Verdict

제품 특이성은 높습니다. 8개 Hermes 부서, Worker 추적, Risk/QA Gate, Paper 제출 경계, LangSmith 원문 비활성화가 HgFinance의 실제 업무 모델에 맞춰져 있습니다.

이번 자동 detector는 `ai-office/app/page.tsx`에서 추가 위반을 찾지 못했습니다. 이전에 지적한 운영 패널 반복, 상태 라벨 혼용, 반복 PIT event, PIT 복구 버튼, 모바일 세부 접기 문제는 구현상 개선됐습니다. 브라우저 오버레이는 자동화 도구가 없어 정적 코드 기준으로만 확인했습니다.

## Overall Impression

운영 콘솔이 이제 “현재 상태 확인 → 안전 경계 확인 → 필요할 때 상세 열기 → PIT 복구”의 흐름을 갖췄습니다. 남은 핵심은 기능을 더 넣는 것이 아니라, 보류 원인과 복구 버튼을 처음 진입한 사용자도 즉시 발견하게 만드는 일입니다.

## What's Working

- 실행 단계·직원 추적·최근 이벤트가 기본 접혀 첫 화면의 세로 길이와 인지 부담이 줄었습니다.
- `kind + 부서 + 요약`으로 반복 runtime event를 묶어 같은 PIT 보류가 새로운 장애처럼 보이지 않습니다.
- 상태 pill, LangSmith 상태, Mandate 승인 상태가 공통 한글 라벨을 사용합니다.
- PIT 보류에서 기존 저장 Mandate를 사용해 새 분석 요청을 보내므로 프론트가 백엔드 안전 경계를 우회하지 않습니다.
- 모바일에서 Worker·내부 메시지·LLM 성과를 native details로 분리했습니다.

## Priority Issues

### [P1] PIT 복구 패널이 특정 runtime 문구에 의존한다

- **Why it matters:** 초기 로딩, 과거 runtime projection, 또는 다른 오류 코드가 들어오면 PIT 진단은 존재해도 Operations Console의 복구 패널이 보이지 않을 수 있습니다.
- **Fix:** runtime message 문자열이 아니라 구조화된 `data_context.pit_readiness` 또는 `quality_status`를 우선 보고 복구 패널을 표시하세요. 메시지는 보조 설명으로만 사용하면 문구 변경에도 UI가 안전합니다.
- **Suggested command:** `$impeccable harden` → `$impeccable clarify`

### [P2] Operations에서 Mandate로 이동한 뒤 복귀 맥락이 약하다

- **Why it matters:** 대표가 `Mandate 설정 확인`을 누르면 설정 화면으로 이동하지만, 방금 보던 run과 부서 상태로 돌아가는 경로가 명시적이지 않습니다.
- **Fix:** Mandate 화면 상단에 `Operations Console로 돌아가기`를 추가하고, 이전 audience/run 맥락을 유지하세요. 새 탭이나 모달은 필요 없습니다.
- **Suggested command:** `$impeccable layout` → `$impeccable harden`

### [P2] 문제 부서 우선 탐색과 검색이 아직 없다

- **Why it matters:** 8개 부서가 있는 실행에서 Power User는 접힌 상세를 열고 다시 부서를 찾아야 합니다.
- **Fix:** Operations 상단에 `오류·보류만`, `업무 중`, `전체` 필터와 부서 검색을 추가하세요. 기본값은 오류·보류가 있으면 해당 부서 우선, 없으면 전체 요약으로 둡니다.
- **Suggested command:** `$impeccable distill` → `$impeccable adapt`

### [P2] 전문 용어의 첫 노출 설명이 짧게 부족하다

- **Why it matters:** PIT, BFF Read Model, Registry, Event Contract를 처음 보는 사용자는 데이터 부족·연결 장애·계획된 계약을 혼동할 수 있습니다.
- **Fix:** 첫 노출에 한 줄 설명을 붙이세요. 예: `PIT = 특정 시점으로 고정한 데이터`, `Registry = 등록된 직원 목록`, `Live event = 실제 실행 중 수신한 메시지`.
- **Suggested command:** `$impeccable clarify`

## Persona Red Flags

**Alex (Power User)**

- 반복 오류 집계는 좋아졌지만 문제 부서로 즉시 점프할 검색·단축키가 없습니다.
- LLM metric과 직원 상세가 서로 다른 접기 영역에 있어 모델 성능과 직원 상태를 비교하려면 두 영역을 열어야 합니다.

**Jordan (First-Timer)**

- PIT 복구 버튼은 발견 가능하지만, PIT가 무엇인지와 어떤 데이터가 부족한지는 진단 문장을 더 읽어야 합니다.
- Mandate 확인 후 Operations로 돌아가는 명시적 경로가 없어 화면 이동 맥락을 잃을 수 있습니다.

**Risk/QA Operator**

- Gate는 상단에서 확인할 수 있지만, 보류된 부서 Worker로 바로 연결되는 링크는 없습니다.
- 계획된 Contract와 live event가 분리됐지만, 각각의 목적을 설명하는 짧은 문구가 더 선명하면 좋습니다.

## Minor Observations

- live runtime event의 worker id는 현재 보조 줄에 노출됩니다. 직원 추적에 필요한 값이므로 삭제보다 기술 상세로 접는 선택이 적절합니다.
- `model_name`, stage, latency, eval score는 정량 관찰에 필요한 정보라 현재 노출이 제품 목적과 맞습니다.
- 네트워크 실패 시 재연결은 있지만, Operations의 BFF 상태 카드에서 바로 재시도하는 버튼은 없습니다.
- 폰트 preconnect와 reduced-motion 처리는 유지할 가치가 있습니다.

## Questions to Consider

- PIT 복구 패널은 runtime message가 없어도 `pit_readiness`가 보류이면 항상 보여야 할까요?
- Operations 상단의 기본 부서 필터는 `오류·보류 우선`과 `전체 부서` 중 어느 쪽이 대표님의 실제 관찰 방식에 더 맞나요?
