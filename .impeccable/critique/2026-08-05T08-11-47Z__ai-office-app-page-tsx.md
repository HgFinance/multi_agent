---
target: ai-office/app/page.tsx
total_score: 23
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 3
timestamp: 2026-08-05T08-11-47Z
slug: ai-office-app-page-tsx
---
## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|---|---:|---|
| 1 | Visibility of System Status | 3 | BFF, runtime, Worker, Gate 상태는 잘 보이지만 같은 상태가 여러 패널에 반복된다. |
| 2 | Match System / Real World | 3 | CEO·부서·직원 은유는 명확하지만 PIT, Registry, Event Contract 같은 내부 용어가 섞인다. |
| 3 | User Control and Freedom | 2 | 필터와 뒤로가기는 있으나 모달 Escape, 포커스 복귀, 명확한 닫기 제어가 없다. |
| 4 | Consistency and Standards | 2 | `IDLE`, `등록됨`, `대기`, `DEGRADED`가 같은 화면에서 혼용되고 탭 semantics도 완결되지 않았다. |
| 5 | Error Prevention | 3 | Read Model, 승인 Gate, 원문 비활성화 등 금융 안전 경계는 강하다. |
| 6 | Recognition Rather Than Recall | 2 | event kind, worker id, source path, PIT 사유를 사용자가 해석해야 한다. |
| 7 | Flexibility and Efficiency | 2 | 부서 선택·범위 필터는 있으나 긴 세로 화면과 반복 상태 때문에 반복 관찰 비용이 크다. |
| 8 | Aesthetic and Minimalist Design | 2 | 픽셀 오피스의 개성은 있으나 운영 화면이 큰 흰 카드와 중복 패널로 무거워진다. |
| 9 | Error Recovery | 2 | 오류와 재연결 버튼은 있으나 사용자가 다음에 무엇을 준비해야 하는지 즉시 알기 어렵다. |
| 10 | Help and Documentation | 2 | 안내 문구는 있으나 기술 용어별 설명과 상태별 해결 방법이 부족하다. |
| **Total** |  | **23/40** | **Acceptable — 운영 흐름과 정보 계층을 한 번 더 정리해야 한다.** |

## Design Specificity Verdict

제품 특이성은 높다. CEO Office, Hermes 부서, LangGraph Worker, Risk/QA Gate, Paper Order 경계가 실제 HgFinance의 업무 모델을 반영하고 있으며, LangSmith 원문을 숨기고 정량 metric만 보여주는 정책도 제품에 맞다.

다만 운영 화면의 카드·pill·상단 창 장식·좌측 포인트 선은 다른 AI 운영 대시보드에도 그대로 옮겨 쓸 수 있는 패턴이다. 자동 검출기는 좌측 side-tab 3건과 픽셀 오피스의 grid background 2건을 advisory/warning으로 잡았고, `live-progress b`의 `transition: width` 1건을 quality warning으로 잡았다. 앞의 5건은 브랜드와 게임 캔버스 의도가 확인되는 false positive에 가깝다. 실제 개선 우선순위는 장식 제거보다 운영 정보의 중복과 상태 해석 비용이다.

## Overall Impression

안전한 금융 운영 도구라는 제품 철학은 잘 드러난다. 가장 큰 기회는 기능을 더 넣는 것이 아니라, 운영 콘솔을 `현재 실행 → 문제가 있으면 다음 조치 → 직원 상세`의 한 흐름으로 압축하는 것이다.

## What's Working

- `BFF Read Model`, `runtime`, `Gate`, `Paper Order`를 분리해 화면에서 금융 상태를 임의 계산하지 않는 경계가 명확하다.
- 전체 부서 선택과 내부 Worker 상태, LLM 정량 metric을 한 화면에서 확인할 수 있어 추적 가능성은 이전보다 좋아졌다.
- Mandate 고급 설정을 `<details>`로 숨기고, LangSmith Input/Output 원문을 비활성화한 정책을 UI에 직접 표시한 점이 좋다.

## Priority Issues

### [P1] 운영 콘솔이 같은 상태를 여러 번 보여준다

- **Location:** `ai-office/app/page.tsx`의 `OperationsConsoleView`, `DepartmentRuntimePanel`, `DepartmentCommunicationPanel`, `OpsPanel`
- **Why it matters:** 전체 부서 카드, 부서 실행 단계, 내부 추적, 통신 계약, Snapshot이 한 페이지에 연속 배치되어 사용자가 지금 봐야 할 상태와 상세 상태를 구분하기 어렵다. 오류가 나면 같은 PIT 보류 메시지를 여러 곳에서 다시 읽게 된다.
- **Fix:** 첫 화면에는 실행 요약 8개와 현재 문제가 있는 부서만 두고, 부서 선택 시 Worker·내부 메시지·LLM metric을 펼치는 단일 inspector로 통합한다. 주문 Snapshot은 별도 탭이나 접힌 상세로 이동한다.
- **Suggested command:** `$impeccable distill` → `$impeccable layout`

### [P1] 모달과 탭이 키보드 사용자를 완전히 지원하지 않는다

- **Location:** `ai-office/app/page.tsx:659-760`, `ai-office/app/ops/DepartmentCommunicationPanel.tsx:203-218`
- **Why it matters:** 프로필/브리핑 모달에 Escape 닫기, 초기 포커스, 포커스 트랩, 닫힌 뒤 포커스 복귀가 없다. `role="tablist"`는 `aria-controls`, 연결된 `tabpanel`, 화살표 이동이 없어 screen reader 사용자가 선택 상태를 안정적으로 이해하기 어렵다.
- **Fix:** native dialog 또는 동일 동작의 focus management를 추가하고, 부서 선택은 실제 tab/tabpanel 계약을 완성하거나 단순 button group으로 낮춘다. 모든 닫기/필터 버튼에 명시적 accessible name과 상태를 제공한다.
- **Suggested command:** `$impeccable harden`

### [P1] 동작 중 상태의 모션 대체가 없다

- **Location:** `ai-office/app/globals.css:49,185`, `ai-office/app/office.css:145,178,298-320`
- **Why it matters:** `rise`, `pulse`, `blinkDot`, `roomPulse`, 직원 걷기/타이핑/대화 애니메이션이 지속 실행되지만 `prefers-reduced-motion` 대응이 없다. 운영 화면에서 상태 변화가 많은 사용자는 피로를 느낄 수 있고, 진행률은 `width` layout transition으로 불필요한 레이아웃 비용도 발생한다.
- **Fix:** reduced-motion에서는 위치 이동·반복 깜빡임을 정지하고 색/텍스트/정적 아이콘으로 상태를 유지한다. 진행률은 transform 기반으로 바꾸거나 짧은 상태 전환만 남긴다.
- **Suggested command:** `$impeccable animate` → `$impeccable optimize`

### [P2] 상태 코드와 오류 사유가 사용자 행동으로 번역되지 않는다

- **Location:** `ai-office/app/page.tsx:1202-1320`, `ai-office/app/ops/DepartmentCommunicationPanel.tsx:292-337`
- **Why it matters:** `PIT`, `NO_VALID_PORTFOLIO_CANDIDATES`, `DEGRADED`, `worker-context.v1`가 대표나 처음 보는 운영자에게 다음 행동을 알려주지 않는다. 긴 원문 사유가 이벤트 카드에 반복되어 핵심이 묻힌다.
- **Fix:** 상태를 `무엇이 문제인가 / 영향 / 다음 조치` 3열 또는 요약 문장으로 보여주고, 원시 코드와 source path는 “세부 정보”에 둔다. 예: “국내 종목 0개 → 분석 보류 → 데이터 연결 후 다시 실행”.
- **Suggested command:** `$impeccable clarify`

### [P2] 반응형에서 작은 조작 요소와 긴 데이터 행이 남아 있다

- **Location:** `ai-office/app/globals.css:112,157,721-727`, `ai-office/app/ops/OpsPanel.tsx:145-220`
- **Why it matters:** dashboard audience/filter/dept selector의 일부 버튼은 30–34px 높이이고, 모바일에서는 긴 event kind·worker id가 여러 줄로 늘어나 스캔성이 떨어진다. 좁은 화면에서 동일 정보가 수직으로 길게 쌓인다.
- **Fix:** 터치 기준 44px을 우선하고, 모바일에서는 상태·부서·시간을 상단 고정 요약으로 묶은 뒤 원문 event detail을 접는다. 표는 행을 카드로 바꾸되 핵심 2–3개 필드만 먼저 보여준다.
- **Suggested command:** `$impeccable adapt`

## Persona Red Flags

**Alex — Power User**

- 1초 REST polling과 WebSocket 갱신은 최신성을 주지만 운영 콘솔의 모든 하위 패널을 다시 렌더링할 가능성이 있다.
- 전체 부서 → 실행 단계 → 내부 추적 → 이벤트 → Snapshot을 모두 내려가야 직원 한 명의 원인을 확인할 수 있다.
- 키보드 단축키, 빠른 부서 검색, 마지막 선택 부서 유지가 없다.

**Jordan — First-Timer**

- `PIT`, `BFF`, `Registry`, `Event Contract`, `LangGraph`를 설명 없이 만난다.
- `PIT 입력이 준비되지 않아 안전하게 실행을 차단했습니다`는 안전하지만 후보/시장 snapshot이 왜 0인지와 무엇을 눌러야 하는지가 없다.
- `IDLE`, `REGISTERED`, `대기`, `DEGRADED`가 비슷해 보인다.

**Risk/QA Operator**

- Risk Gate와 QA Gate가 화면 하단에 있어 현재 실행의 다음 안전 경계를 즉시 비교하기 어렵다.
- 부서 내부 메시지와 부서 간 Event Contract가 모두 카드 목록으로 표현되어 실제 runtime event와 계획된 계약의 차이를 계속 확인해야 한다.

## Minor Observations

- 모달 닫기 버튼은 `✕` 텍스트만 있어 accessible name을 명시하는 편이 안전하다.
- 전역 focus 스타일이 `button, a`에만 적용되어 input/select/textarea의 focus 상태가 디자인상 약하다.
- `@import`로 Google Fonts를 불러와 네트워크 지연 시 첫 렌더가 흔들릴 수 있다.
- 상태 pill은 운영 코드와 한국어 표시가 한 컴포넌트에서 섞이므로 표시용 label 매핑을 공통화하면 일관성이 좋아진다.

## Questions to Consider

- 운영 콘솔의 첫 화면에서 사용자가 반드시 알아야 하는 하나의 질문은 “실행 중인가?”인가, “왜 보류됐나?”인가?
- 전체 부서 8개를 한 번에 펼쳐두는 것이 정말 필요한가, 아니면 문제가 있는 부서만 먼저 보여주는 것이 더 빠른가?
- LangSmith metric과 내부 Worker 상태를 하나의 직원 inspector에서 연결하면 현재의 반복 패널을 얼마나 줄일 수 있는가?
