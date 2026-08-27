---
name: methodology-scout
description: "Turn open-web methodology (papers, investor letters, practitioner writing, communities, other fields) into source-grounded hypotheses for the autonomous quant research lab. Enforces source discipline, competing explanations, and prior-art checks."
version: 0.1.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [research, methodology, hypothesis, factory, web-search]
    related_skills: [agentic-rag]
---

# Methodology Scout: 방법론 → 근거·가설

## Overview

이 스킬은 자율 연구실의 탐색 렌즈다. **웹에서 방법을 찾아 근거와 경쟁 가설로 만든다.**
산출물은 사람이 읽는 리포트에 그치지 않고, 연구실의 영속 `hypotheses/`와 등록 계획으로
넘길 수 있는 source-grounded evidence다. 계획 생성·실험 실행·결과 판정은
전략 실행은 Strategy Hermes와 결정론 검증면이 소유한다. Research HQ의
스카우트는 방법론 근거만 제공하고 `autonomous-quant-research` 실행 루프를
호출하지 않는다.

종목 방향·확률 예측은 이 스킬의 범위가 아니다. 방향 판단은 실험을 통과해 승격된
전략의 몫이고, 여기서 그것을 하면 프레임워크가 다시 투자판단을 하게 된다.

연구실: `departments/01-research/autonomous/`
결과 게이트: `departments/01-research/autonomous/result.py` (결정론)

## When to use

- 스카우트 주기가 돌아왔을 때 (렌즈별 주기는 `hermes/config.yaml` 의 `scout_operations`)
- 퀀트가 `UNMAPPED_VOCAB` 으로 반려해 어휘 등재를 요청해야 할 때
- 기각된 계열을 교훈에 대응해 재도전할 때

## 렌즈 (같은 도구, 다른 곳을 판다)

| 렌즈 | 어디를 | 그 렌즈에서만 나오는 것 |
|---|---|---|
| ACADEMIC | 학술지·arXiv·SSRN·학위논문 | 메커니즘 서술과 저자가 밝힌 실패 조건 |
| PRACTITIONER | 투자자 서한·운용사 코멘터리·데스크 노트 | 실제로 굴린 사람의 제약 조건 |
| COMMUNITY | 포럼·영상 트랜스크립트·오픈소스 | 글로 안 써진 관행(folklore) |
| CROSS_DOMAIN | 신호처리·정보이론·통계물리·생태학·제어이론 | 금융이 아직 안 써본 구조 |

**렌즈끼리 서로의 결과를 보지 않는다.** 한 렌즈가 놓친 광맥을 다른 렌즈가 잡는 것이
이 편제의 전부이고, 결과를 공유하면 네 명이 한 명이 된다.

## 절차

### 1. 수집 — 근거 카드를 만든다

각 소스마다 근거 카드 하나. **다음이 없으면 연구 근거가 아니다:**

- `refs` — URL, 제목, 발행일, 접근 시각, **원문 발췌**(≤500자). 요약이 아니라 인용이다
- `claimed_edge` — 소스가 주장하는 엣지 한 문장. 당신의 해석이 아니라 소스의 주장
- `market_context` — 소스가 실제로 다룬 시장과 기간

**출처 없는 카드는 폐기한다.** 기억으로 재구성하지 않는다 — 그것이 이 파이프라인의
첫 번째 오염원이고, 하류의 어떤 검사도 그것을 못 잡는다(결정론 검사는 URL 존재는
보지만 발췌가 원문과 같은지는 못 본다).

`inferred: true` — 메커니즘을 소스가 말한 게 아니라 당신이 추론했다면 반드시 표시한다.
실무자 글은 "무엇을 하는지"만 쓰고 "왜 되는지"는 안 쓰는 경우가 많다.

`testability: UNUSABLE` — 규칙으로 서술할 수 없으면 그렇게 적는다. **실패가 아니라
정상 산출이다.** 억지로 다듬어 넘기면 그 비용은 실험 예산에서 나간다.

### 1.1 공개식은 기준선이고, 후보는 별도로 파생한다

논문·글에 공개된 수식을 이름이나 창만 바꿔 신규 알파로 제출하지 않는다. `AST_READY`
리드는 다음 세 모드 중 하나와 함께 공개 기준선과 후보를 분리해 기록한다.

- `DIRECT_REPLICATION` — `SOURCE_BASELINE_EXPR`와 `CANDIDATE_SIGNAL_EXPR`가 같다.
  재현·데이터 QA 대조군으로 보존하지만 알파 후보로 발행되지 않는다.
- `MECHANISM_MUTATION` — 공개 기준식에서 경제적으로 무엇을 바꿨는지
  `DERIVATION_TRANSFORMS`와 `NOVELTY_RATIONALE`에 기록한다. exact 재사용과 창·상수만
  바꾼 동일 AST shape는 결정론 검사에서 거부된다.
- `CROSS_DOMAIN_TRANSFER` — 금융 밖 구조를 시장에 옮긴다. 반드시
  `MARKET_STRUCTURE_TRANSFER`와 시장 변수 대응 논리를 적는다.

허용 변형은 `STATE_CONDITION`, `CLOCK_CHANGE`, `BOOK_DEPTH_CHANGE`,
`MECHANISM_INTERACTION`, `RESIDUALIZE_PUBLIC_SIGNAL`, `FAILURE_MODE_INVERSION`,
`MARKET_STRUCTURE_TRANSFER`, `TARGET_CHANGE`다. 변형 이름만 선언해서는 안 되며 실제
후보 AST도 공개 기준선과 달라야 한다. 공개 문헌은 메커니즘과 반증 조건의 근거이지,
그 공개 수식이 지금도 수익을 낸다는 근거가 아니다.

### 2. 실패 기억 대조 — 이미 소진한 경로인가

가설을 등록하기 **전에** `FAILURE_MEMORY.md`와 기존 결과를 조회한다. 같은
representation·label·sampling·regime 경로가 반복되면 숫자만 바꾸지 말고 pivot한다.

대응은 "다시 해보겠다"가 아니라 **무엇을 바꿔서 그 교훈을 피하는가**다.
예: `BEAR_FRAGILE` → "하락장 표본을 2창에서 5창으로 늘려 재검증한다"

### 3. 가설 설계 — 연구 차원으로 사상한다

`representation`, `label`, `sampling`, `horizon`, `regime`, `model`을 명시한다.
**자유 서술만 남기지 않는다.** 같은 아이디어가 다른 이름으로 반복되면 연구실의
anti-loop가 작동하지 않으므로, 차원과 parent lineage를 함께 기록한다.

실행에 필요한 데이터나 방법이 없으면 **계획을 억지로 만들지 않고 BLOCKED**로 기록한다.
비슷한 것으로 대신 돌리면 그 결과는 이 가설의 증거가 아니라 다른 전략의 성적이다.

### 4. 경제적 근거 — 누가 반대편에서 잃어주는가

`counterparty` 를 반드시 적는다. "과거에 잘 됐다"는 근거가 아니라 관찰이다.
답해야 할 것은 **왜 이 엣지가 아직 남아 있는가** — 어떤 제약(규제·유동성·의무·
행동편향) 때문에 반대편이 계속 지는가.

### 5. 경쟁 설명 — 독립 회의론자에게 넘긴다

회의론자(RES-15)는 **당신의 채택 사유를 보지 않는다.** 초안만 받아 가장 강한
비-알파 설명을 만든다: `BETA_EXPOSURE` / `LIQUIDITY_PREMIUM` / `DATA_MINING` /
`COST_UNACCOUNTED` 중 최소 하나.

경쟁 설명과 falsifier 없이는 계획을 만들지 않는다. 자기가 쓴 것을 자기가 반박하면
그것은 반증이 아니라 자기 검열이므로, 별도 검토 기록으로 남긴다.

### 6. 계획 등록 — 결정론 게이트로 넘긴다

자율 연구실의 계획·결과 게이트가 판정한다. 막는 것: 출처 없는 근거, 측정 불가능한
가설, 누수, 비용·OOS·강건성 증거 누락, 같은 경로의 무의미한 반복.

**게이트는 "그럴듯한가"를 판정하지 않는다** — 등록 가능한 질문과 반증 조건을 확인하고,
그럴듯한지는 독립 실험과 결과가 판정한다.

## 도구

- `research.web.search` / `.open` / `.verify` — 스카우트만. 회의론자·기획자는 **의도적으로 금지**다
  (검색을 주면 반증 대신 보강을 시작한다)
- 논문 검색 MCP (`paper-search`) — arXiv·Semantic Scholar·Crossref·PubMed 등, API 키 불필요
- 영상 자막 MCP (`youtube-transcript`)
- 공개 SearXNG 인스턴스는 ToS 위반이라 쓰지 않는다. **우리가 운영하는 주소만.**

## 하지 않는 것

- 종목 방향·확률 예측
- 매수·매도·비중 권고
- 전략 승격 판단
- 검색으로 찾은 스니펫을 검증 없이 사실로 사용
- 소스가 보고한 수치를 우리 백테스트 결과와 같은 자리에 두는 것
  (`source_reported_effect` 는 **별도 필드**다 — 남의 시장·남의 기간 숫자가 우리 결과처럼
  읽히는 순간 미검증 값이 근거로 승격된다)

## 실패 처리

- 소스 접근 실패 → 리드 미생산으로 기록. **지어내지 않는다**
- 어휘 미사상 → 어휘 등재 요청, 기획안 보류
- 회의론자가 경쟁 설명을 못 만들면 → 그 사실 자체를 적는다. 주장이 유난히 깨끗하거나
  유난히 모호하다는 신호다
- 편집장 큐가 상한(20건)이면 → 스카우트 소집 정지. 읽히지 않을 리드를 계속 만드는 것이
  상주 운영의 기본 실패 모드다
