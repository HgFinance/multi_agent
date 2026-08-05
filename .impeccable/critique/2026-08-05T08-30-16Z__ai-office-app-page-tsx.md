---
target: ai-office/app/page.tsx
total_score: 29
max_score: 40
na_heuristics: 
p0_count: 0
p1_count: 3
timestamp: 2026-08-05T08-30-16Z
slug: ai-office-app-page-tsx
---
## Design Health Score

| # | Heuristic | Score | Key Issue |
|---|---|---:|---|
| 1 | Visibility of System Status | 3 | 실행·부서·직원·Gate 상태는 보이지만 같은 실행의 상태가 여러 영역으로 분산됩니다. |
| 2 | Match System / Real World | 3 | CEO/부서/직원 은유는 강하지만 PIT·Registry·Event Contract가 여전히 내부 용어입니다. |
| 3 | User Control and Freedom | 3 | 모달 닫기·Escape·포커스 복귀와 상세 접기는 개선됐지만 긴 운영 화면의 빠른 복귀 경로는 약합니다. |
| 4 | Consistency and Standards | 3 | 공통 한글 상태 매핑이 생겼지만 CONNECTED, REGISTERED, 일부 이벤트 상태가 원문으로 남습니다. |
| 5 | Error Prevention | 3 | Read Model·Risk/QA Gate·Paper 제출 경계가 명확해 안전성은 좋습니다. |
| 6 | Recognition Rather Than Recall | 3 | PIT와 실행 오류는 번역됐지만 Event Contract·source·worker id를 해석해야 하는 순간이 남습니다. |
| 7 | Flexibility and Efficiency | 3 | 부서 탭·통신 범위 필터·상세 접기는 좋지만 문제 부서 우선 보기와 검색이 없습니다. |
| 8 | Aesthetic and Minimalist Design | 3 | 픽셀 오피스의 개성은 유지됐지만 Operations Console의 세로 길이와 흰 카드 비중이 큽니다. |
| 9 | Error Recovery | 3 | 재연결·다음 조치가 있지만 PIT 데이터 보류를 바로 해결하는 실행 경로는 없습니다. |
| 10 | Help and Documentation | 2 | 핵심 안내는 있으나 상태 코드와 데이터 준비 요건의 짧은 도움말이 부족합니다. |
| **Total** |  | **29/40** | **Good — 운영 화면은 안정됐지만 상태 압축과 용어 해석 비용을 더 줄여야 합니다.** |

## Design Specificity Verdict

제품 특이성은 높습니다. CEO Office, 8개 부서, 독립 Worker, Risk/QA Gate, LangSmith 원문 비활성화 정책이 실제 HgFinance 운영 모델에 맞게 표현되어 있어 일반적인 SaaS 대시보드와 구별됩니다.

다만 운영 상세 화면의 큰 카드, 상태 pill, Event Registry 목록은 다른 AI 운영 콘솔에도 옮겨 쓸 수 있는 구조입니다. 현재 자동 detector는 `ai-office/app/page.tsx`에서 추가 위반을 찾지 못했습니다. 이번 평가는 정적 코드와 기존 화면 구조 기준이며 브라우저 오버레이 검증은 도구 부재로 생략했습니다.

## Overall Impression

이전보다 훨씬 운영 가능한 화면이 됐습니다. 가장 큰 남은 문제는 기능 부족이 아니라 `지금 문제인가 → 무엇을 해야 하나 → 어느 직원이 관련됐나`를 한 화면에서 바로 연결하지 못하는 정보 흐름입니다.

## What's Working

- 모달의 Escape, 포커스 트랩, 포커스 복귀와 부서 탭의 tab/tabpanel 계약이 운영 화면의 기본 접근성을 크게 올렸습니다.
- `시점 고정 데이터가 부족해 분석을 안전하게 보류했습니다`처럼 오류를 사용자 언어와 다음 조치로 바꾼 방향이 금융 안전 경계와 잘 맞습니다.
- 전체 부서 요약과 선택 부서의 Worker·내부 메시지·redacted LLM metric을 연결해 직원 추적의 핵심 흐름이 생겼습니다.

## Priority Issues

### [P1] 상세 패널은 접혔지만 Operations Console의 첫 화면은 여전히 길다

- **Why it matters:** 실행 요약, 단계, Worker trace, Gate, 최근 이벤트 다음에 부서 내부 통신과 주문 Snapshot이 이어져, 장애 원인을 찾을 때 세로 스크롤 비용이 큽니다.
- **Fix:** 상세 영역은 기본 닫힘으로 두고, `오류/안전 보류가 있는 부서`만 첫 화면에서 한 줄 inspector로 노출하세요. 부서 통신은 선택된 scope의 live event를 먼저, 계획된 Contract는 별도 접기로 분리하면 됩니다.
- **Suggested command:** `$impeccable distill` → `$impeccable layout`

### [P1] 상태 표시용 라벨이 아직 한 가지 체계로 완전히 통일되지 않았다

- **Why it matters:** `연결됨`, `CONNECTED`, `등록됨`, `REGISTERED`, `대기`, `IDLE`, `저하`, `DEGRADED`가 같은 콘솔에 섞이면 직원이 실제로 일 중인지 판단하는 속도가 떨어집니다.
- **Fix:** 모든 status pill은 공통 `readableRuntimeStatus`를 통과시키고, raw code는 `title` 또는 “기술 상세”에만 두세요. `LIVE`, `등록됨`, `계획됨`도 같은 상태군으로 정리해야 합니다.
- **Suggested command:** `$impeccable clarify`

### [P1] 오류 요약과 이벤트가 반복되어 원인보다 소음이 커질 수 있다

- **Why it matters:** 같은 PIT 보류가 여러 runtime message로 들어오면 사용자는 새로운 실패가 반복된다고 오해합니다.
- **Fix:** `kind + department + reason` 기준으로 최근 동일 이벤트를 묶고 `3회 반복`처럼 집계하세요. 첫 카드에는 원인·영향·다음 조치만 두고 raw event와 source는 펼친 상세로 이동하세요.
- **Suggested command:** `$impeccable distill` → `$impeccable clarify`

### [P2] PIT 보류는 설명됐지만 복구 행동이 연결되어 있지 않다

- **Why it matters:** “국내 종목과 시장 스냅샷을 준비하세요”까지는 이해되지만 사용자는 어느 화면에서 무엇을 준비하고 재실행해야 하는지 다시 찾습니다.
- **Fix:** PIT 진단 카드에 `Mandate 확인`, `데이터 새로고침`, `분석 다시 시작` 중 실제 가능한 버튼만 노출하고, 불가능한 항목은 비활성화 사유를 표시하세요. 자동 통과나 가짜 데이터 생성은 금지합니다.
- **Suggested command:** `$impeccable harden` → `$impeccable onboard`

### [P2] 작은 화면에서 운영 데이터가 카드 수직 스택으로 길어진다

- **Why it matters:** 부서 탭, Worker Registry, 내부 메시지, metric, Contract가 모두 수직으로 쌓이면 모바일에서는 한 명의 상태 확인에도 여러 화면을 내려야 합니다.
- **Fix:** 모바일에서는 선택 부서의 `업무 중 / 대기 / 오류` 요약을 상단 고정하고, Worker·메시지·LLM 성과·Contract를 각각 native `<details>`로 나누세요. event kind와 worker id는 첫 줄에서 잘라 보여주고 전체 값은 title/details에 두면 됩니다.
- **Suggested command:** `$impeccable adapt`

## Persona Red Flags

**Alex (Power User)**

- 전체 8개 부서 중 문제가 있는 부서로 바로 점프할 검색/단축키가 없습니다.
- 동일 오류의 반복 이벤트를 수동으로 비교해야 하고, LangSmith metric은 선택 부서 inspector까지 내려가야 합니다.

**Jordan (First-Timer)**

- “PIT”, “Registry”, “Event Contract”, “BFF Read Model”을 처음 만나면 안전 보류와 연결 장애를 구분하기 어렵습니다.
- 재연결 버튼은 있지만 분석 재실행의 전제조건과 준비 위치가 한 문장으로 연결되지 않습니다.

**Risk/QA Operator**

- Gate 상태는 상단에 있지만 실제 관련 Worker를 보려면 부서 상세를 다시 열고 선택해야 합니다.
- `planned` Contract와 실제 `live` event가 같은 목록 형식에 가까워 계획과 실행의 차이를 재확인해야 합니다.

## Minor Observations

- Operations Console의 LangSmith 상태도 공통 한글 라벨을 사용하면 일관성이 좋아집니다.
- 모달은 동작상 좋아졌지만 `section[role=dialog]`보다 native `<dialog>`를 쓰면 브라우저의 modal semantics를 더 적은 코드로 얻을 수 있습니다. 현재 구현을 바꿀 때만 고려하세요.
- `@import` 기반 폰트 로딩은 preconnect로 완화됐지만 네트워크가 느릴 때 첫 화면의 폰트 이동은 남을 수 있습니다.
- `source` 경로와 worker id는 개발자에게 유용하므로 삭제하지 말고 기술 상세 영역으로 숨기는 편이 제품 정체성과 운영성을 모두 지킵니다.

## Questions to Consider

- 첫 화면의 대표 질문을 “실행 중인가?”와 “왜 보류됐나?” 중 어느 쪽으로 고정할까요?
- 기본 상세를 닫고 문제 부서만 자동으로 여는 방식이 전체 부서 상태를 항상 펼쳐두는 방식보다 대표님의 운영 흐름에 맞을까요?
- PIT가 보류된 경우, 프론트에서 제공할 수 있는 실제 복구 버튼은 데이터 새로고침·Mandate 수정·재실행 중 어디까지인가요?
