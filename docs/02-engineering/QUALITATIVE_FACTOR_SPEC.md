# 정성 팩터 명세 (Qualitative Factor Spec) v0.1

> 상태: 초안 — 팀 검토 전
>
> 기준일: 2026-08-12
>
> 소유: 재일 (리서치본부) / 소비자: 퀀트본부
>
> 상위 기준: [MASTER_PLAN](../HEDGE_FUND_MASTER_PLAN.md) → [DATA_GOVERNANCE_GUIDE](DATA_GOVERNANCE_GUIDE.md) → 이 문서

## 1. 이 문서가 정하는 것

DART 공시를 **MCP로 그때그때 조회**해서 정성 요인을 읽고, 그것을 **점수 팩터**로 만들어
퀀트가 쓰게 하는 경로의 기준과 원칙.

정하는 것은 넷이다.

1. MCP에서 실제로 가져올 수 있는 데이터가 무엇인가 (§3)
2. 어떤 팩터를 만들고, 각각의 채점 기준이 무엇인가 (§5)
3. LLM이 하는 일과 결정론 Python이 하는 일의 경계 (§4)
4. 점수를 어떻게 적재해야 PIT가 성립하고 용량이 안 늘어나는가 (§6)

## 2. 왜 이 경로를 만드는가

### 2.1 분업

| 축 | 담당 | 입력 | 소급 재현 |
|---|---|---|---|
| 가격 파생 정량 지표 | 퀀트 백테스트 | `market.market_bars` (TimescaleDB) | 가능 |
| 펀더멘털 정량 점수 | 리서치 [`fundamental_scores.py`](../../departments/01-research/evidence/fundamental_scores.py) | `research.financial_facts` | 가능 |
| **정성 요인 점수 팩터** | **리서치 정성 분석가 (신설)** | **DART MCP 실시간 조회** | **불가 — forward-only** |

세 번째 줄이 이 문서의 대상이다. 앞의 둘을 대체하지 않는다.

### 2.2 저장 정책 변경

현재 DB 937MB 중 재무·공시 원본 계열이 293MB(31%)다.

```
research.evidence_chunks        586 MB    43,404행   ← 별개 문제, 이 문서 범위 밖
research.financial_facts        192 MB   106,750행   ┐
research.documents               78 MB   138,019행   ├ 293MB — 정성 팩터가 대체하는 범위
research.document_instruments    23 MB   124,263행   ┘
```

정성 팩터는 **원문을 적재하지 않고 점수와 인용 좌표만 적재**한다(§6). 종목당 관측
한 건이 수백 바이트라 원본 대비 3~4자리 작다.

### 2.3 실제로 내린 것 (2026-08-12)

`collector_scheduler.py`의 **`document-archive` Job 하나만** 내렸다. 판단 기준은
"실시간 적재가 필요 없고, MCP가 1:1로 대체할 수 있는가"였다.

확정된 역할 분담: **퀀트는 정량분석만, 정성분석은 점수 팩터로.**

| Job | 실시간성 | 조치 | 사유 |
|---|---|---|---|
| `document-archive` | 없음 (daily 20:00) | **내림** | MCP `get_attachments`가 1:1 대체. **원문이 안 들어오면 `evidence_chunks`도 안 늘어난다** — 신규 유입을 끊는 유일한 지점 |
| `macro` | 없음 (daily 07:30) | **내림** | 거시는 점수 팩터의 재료로. MCP: FRED·KOSIS. ⚠ **ECOS는 단독 MCP가 없다** |
| `geopolitical` | 없음 (daily 07:20) | **내림** | 지정학도 점수 팩터로. GPR·GDELT 원계열 120일치를 매일 다시 쌓지 않는다 |
| `disclosure` | **있음** (10분 주기, 장중) | 유지 | 장중 공시 감지 + QF-05 재료 |
| `financial` `cashflow` | 없음 | 유지 | F-Score 8/9 · Altman Z가 이 위에 있다 (정량 축) |
| `corporate-action` `company-profile` | 없음 | 유지 | 기준 데이터·원장 경로 |

내린 세 Job은 **주석으로 남기고 수집기 파일은 지우지 않았다.** MCP 대체가 실측으로
검증되기 전까지 되돌릴 수 있어야 한다.

### 2.4 내리면서 받아들인 것

`macro`·`geopolitical`은 테이블이 남고 **갱신만 멈춘다.** 소비처가 살아 있다.

| 굳는 테이블 | 계속 읽는 곳 |
|---|---|
| `research.macro_observations` | 퀀트 `data_resolution.py`(**`observed_at` PIT 소스로 등록됨**), `narrative_guard.py`, 공개 대시보드 뷰, `research_v2.py` 계약 |
| 지정학 계열 | `agents/geopolitical_analyst.py` (37KB, 전적으로 의존) |

**결정 사항**: "어느 정도 재현 가능하면 MCP로 충분하다"에 따라 **원계열의 소급 재현은
포기한다.** 점수 팩터는 §6.3대로 forward-only다.

**따라서 지켜야 할 것**:
- 팩터 경로가 서기 전까지 `geopolitical_analyst`의 출력을 "최신"으로 읽지 않는다.
- 계열이 굳는 사실은 `research-data-steward`(07:15 리서치 평면 DQ)에서 **드러나야 한다.**
  안 드러나면 그건 Steward의 결함이며 별도로 고친다 — 조용히 굳은 계열을 최신으로
  읽는 것이 이 변경의 유일한 실질 위험이다.
- `config/geopolitical_themes.txt`의 테마 정의는 팩터 설계에 그대로 재사용한다.

> ⚠ 이 결정은 용량 대책으로 **부분적**이다. 이미 쌓인 `evidence_chunks` 586MB는
> 그대로 남는다 — 증가만 멈춘다. 회수하려면 retention 정책이 따로 필요하다
> (임베딩 차원 축소는 [database/README.md](../database/README.md) 106행에 따라 ADR 대상).

## 3. MCP에서 가져올 수 있는 데이터

### 3.1 원천 API와 실제 필드

이 저장소 수집기들이 이미 DART를 겪었다. 아래는 그 실측을 옮긴 것이며, MCP는 같은 API를
감쌀 뿐이므로 **같은 한계를 그대로 물려받는다.**

| DART API | MCP 도구(예: korean-dart-mcp) | 쓸 수 있는 것 | 확인된 한계 |
|---|---|---|---|
| 2019001 공시검색 | `search_disclosures` | `rcept_no`, `report_nm`, `rcept_dt`, `corp_name`, 제출인 | **`rcept_dt`에 시각이 없다(날짜만).** 09:00 판단이 15:00 공시를 미리 본 것처럼 보일 수 있다 |
| 2019002 기업개황 | `get_company` | 업종코드, 결산월, 설립일, 정식명칭 | 회사당 1호출. 법인등록번호·주소·대표자명은 응답에 있어도 **쓰지 않는다** |
| 2019003 공시원문 | `download_document`, `get_attachments` | 원문 ZIP, 첨부 HWP/PDF → 마크다운 | 파싱 품질이 문서 양식에 좌우된다 |
| 2019017 다중회사 주요계정 | `get_financials` | BS/IS 주요계정 | 현금흐름표 없음 |
| 2019020 단일회사 전체 재무제표 | `get_financials`(전체) / `get_xbrl` | BS/IS/**CF** | 회사당 1호출. 계정명 비표준 |
| 지분·보수·대량보유 | `get_shareholders`, `get_executive_compensation`, `get_major_holdings` | 지분율, 보수, 5%룰 변동 | — |

### 3.2 반드시 알고 시작해야 하는 제약 (수집기 실측)

1. **공시 시각이 없다.** `rcept_no` 앞 8자리가 접수일이고 그 안에 시각이 없다.
   [`opendart_collector.py`](../../departments/01-research/collectors/opendart_collector.py)가
   `published_at_precision=DAY`로 표시하고 *"Backtest는 observed_at을 본다"*로 처리한다.
   정성 점수도 **같은 규칙을 따른다** — 점수의 시각은 공시일이 아니라 **우리가 채점한 시각**이다.
2. **정정공시가 있다.** `report_nm` 앞에 `[기재정정]`·`[첨부정정]`이 붙는다. 원본을 덮어쓰지
   않고 `status=CORRECTED`로 남긴다. 정성 팩터에서는 이것이 **재료이자 팩터**다(§5.5).
3. **매출총이익이 표준 계정에 없다.** 그래서 F-Score가 8/9다. 정성 팩터로도 이 값을
   만들어내지 않는다 — 없는 것을 추정으로 채우지 않는다.
4. **CFO 계정명이 회사마다 갈린다**(실측 10가지 변형). 총액만 골라야 하고, 이 규칙은
   이미 `is_cfo_account`에 있다. 정성 분석가는 이 판정을 **다시 만들지 않고 재사용한다.**
5. **호출 예산.** 무료 티어 20,000 req/일. 종목 2,596개 × 도구 여러 개면 하루치가 금방
   찬다. §7의 배치 정책을 지켜야 한다.

### 3.3 MCP가 제공하는 "분석 프레임"은 쓰지 않는다

`korean-dart-mcp`은 `insider_signal`·`disclosure_anomaly`·`buffett_quality_snapshot`
같은 **자체 해석 도구**를 함께 제공한다. 편리하지만 이 저장소에서는 **채점에 쓰지 않는다.**

[`methods.py`](../../departments/01-research/evidence/methods.py)의 규율이
*"인용(citation)이 없는 방법은 등재하지 않는다. 출처 없는 임의 규칙은 기법이 아니라 취향이다"*
이기 때문이다. 제3자가 문서화하지 않은 가중치를 팩터로 들이면 그 규율이 무너지고,
나중에 팩터가 왜 그렇게 움직였는지 설명할 수 없다.

> **원칙**: MCP에서는 **원천 데이터(primitive)만** 가져오고, 해석 프레임은 우리가
> 인용과 함께 만든다.

## 4. LLM과 결정론의 경계

CLAUDE.md 개발 원칙: *"LLM은 관련성 판단·서술에만 쓴다. PIT 필터·인용 검증·한도 검사는
결정론적 Python."* 정성 팩터는 이 경계를 이렇게 지킨다.

```
MCP 조회 (원천 데이터)
  → [LLM] 공시 원문에서 해당 항목의 근거 문장 추출 + 정성 판정(YES/NO/UNKNOWN)
  → [결정론 Python] 판정을 점수로 환산 · 인용 검증 · 결측 처리 · 분모 계산
  → 점수 + 인용 좌표 적재
```

**LLM이 하는 것**: 근거 문장 찾기, 서술의 방향 판정(개선/악화/불명), 요약.
**LLM이 하지 않는 것**: 점수 부여, 가중치 결정, 최종 등급 산출, 결측 채우기.

판정은 반드시 3값이다 — `True` / `False` / `None(재료 없음)`.
`fundamental_scores.py`의 `Signal` 데이터클래스와 같은 모양을 쓴다.

### 4.1 무엇이 "정성 입력"인가 — 편입 판별 규칙

> **정량 지표가 이미 답할 수 있는 질문은 정성 팩터에 넣지 않는다.**

정성 팩터의 입력은 **숫자로 존재하지 않는 것**이어야 한다. 재무제표에서 계산되는 값을
LLM에게 다시 읽히면 정확도만 잃고 얻는 게 없다. 아래 4종만 정성 입력으로 인정한다.

| 정성 입력 유형 | 예 | 왜 정성인가 |
|---|---|---|
| **선언(declaration)** | 지배구조 핵심지표 준수/미준수 | 회사가 선언한 사실이지 계산된 값이 아니다 |
| **설명(explanation)** | 미준수 사유, 정정 사유 | 서술이며, 그 **구체성·일관성**이 정보다 |
| **구조(structure)** | 이사회 구성, 감사기구 독립성, 승계정책 유무 | 제도의 존재 여부는 재무제표에 없다 |
| **서사(narrative)** | 경영진 논의, 사업 위험 서술의 톤·변화 | 텍스트에만 있다 |

**반례 — 정성 팩터에서 뺀 것들** (v0.1 초안에서 잘못 넣었던 항목):

| 뺀 항목 | 왜 | 어디로 |
|---|---|---|
| 배당성향, 연속배당 횟수, 배당 감소 여부 | 재무제표에서 **계산된다** | 정량 축 (`fundamental_scores.py`) |
| 임원 보수 총액 / 순이익 비율 | 공시 수치의 나눗셈이다 | 정량 축 |
| 최대주주 지분율 구간 | 공시된 숫자다 | 정량 축 |
| 내부자 순매수 건수 | 세는 것이다 | 정량 축 |

남는 정성 질문은 이런 것이다 — *"배당정책을 주주에게 **통지했는가**, 그 정책 서술이
**구체적인가**"*. 배당성향 30%라는 사실은 정량이고, 회사가 배당정책을 문서로 세워
알렸는지는 정성이다.

## 5. 팩터 정의

각 팩터는 **인용 → 재료(MCP 도구) → LLM 판정 → 결정론 채점 → 결측 규칙** 순으로 정의한다.
아래 5개가 v0.1 대상이다.

### 5.1 QF-01 배당 정책 신뢰도

- **인용**: Lintner, J. (1956). *Distribution of Incomes of Corporations Among Dividends,
  Retained Earnings, and Taxes.* AER 46. / Michaely, Thaler & Womack (1995).
  *Price Reactions to Dividend Initiations and Omissions.* JF 50.
- **재료**: `search_disclosures`(현금·현물배당결정), `get_financials`(당기순이익, 이익잉여금)
- **LLM 판정**: 배당 관련 공시에서 ① 배당 성향에 대한 회사의 서술이 있는가 ② 그 서술이
  전기 대비 유지/상향/하향 중 무엇인가
- **결정론 채점** (각 1점, 재료 없으면 분모에서 제외):
  1. 최근 3개 회계연도 연속 배당 결정 공시 존재
  2. 주당배당금이 전기 대비 감소하지 않음
  3. 배당성향이 100% 이하 (이익을 넘겨 배당하지 않음)
  4. 배당 중단·미실시 공시 없음
- **결측 규칙**: 배당 이력이 아예 없는 회사는 0점이 아니라 **채점 제외**(무배당 정책과
  자료 부재를 구분한다). 결과에 `available=0`으로 표기한다.

### 5.2 QF-02 지배구조 집중 리스크

- **인용**: Gompers, Ishii & Metrick (2003). *Corporate Governance and Equity Prices.* QJE 118.
- **재료**: `get_shareholders`, `get_major_holdings`, `get_executive_compensation`
- **LLM 판정**: 사업보고서 지배구조 항목에서 특수관계자 거래·내부거래 서술의 존재와 방향
- **결정론 채점** (역점수 — 높을수록 리스크):
  1. 최대주주 지분율 구간 (과소 <20%, 과다 >70% 각 +1)
  2. 최근 1년 최대주주 변경 공시 존재 (+1)
  3. 임원 보수 총액이 당기순이익 대비 임계 초과 (+1)
  4. 특수관계자 거래 서술 존재 (+1)
- **결측 규칙**: 지분 공시가 없으면 해당 신호 제외. **리스크 0으로 읽지 않는다** —
  `UNKNOWN`으로 표기하고 퀀트는 이를 결측으로 다룬다.

### 5.3 QF-03 내부자 거래 시그널

- **인용**: Lakonishok, J. & Lee, I. (2001). *Are Insider Trades Informative?* RFS 14.
- **재료**: `search_disclosures`(임원·주요주주 특정증권등 소유상황보고서)
- **LLM 판정**: 보고서에서 거래 유형(장내매수/매도/증여/상속)과 사유 서술 분류
- **결정론 채점**: 최근 90일 순매수 건수 − 순매도 건수를 구간화(−2..+2).
  상속·증여·스톡옵션 행사는 **정보성 거래가 아니므로 제외**(인용 논문의 처리와 동일).
- **결측 규칙**: 보고서가 없으면 0점이 아니라 `UNOBSERVED`. 내부자가 거래를 안 한 것과
  우리가 못 본 것을 구분한다.

### 5.4 QF-04 공시 서술 톤

- **인용**: Loughran, T. & McDonald, B. (2011). *When Is a Liability Not a Liability?
  Textual Analysis, Dictionaries, and 10-Ks.* JF 66. / Li, F. (2008). *Annual report
  readability, current earnings, and earnings persistence.* JAE 45.
- **재료**: `get_attachments`(사업보고서 HWP/PDF → 마크다운), `download_document`
- **LLM 판정**: 경영진 논의 부분에서 부정어·불확실성 표현의 존재와 전기 대비 변화 방향
- **결정론 채점**: 톤 방향(개선/유지/악화) 3값 + 문서 길이 변화율 구간.
  ⚠ **한국어 금융 감성 사전이 없다.** Loughran-McDonald는 영문 10-K 기준이므로
  그대로 쓸 수 없다. v0.1에서는 **LLM 판정 3값만 쓰고 사전 기반 점수는 `BLOCKED`로 둔다**
  (`methods.py`의 `blocked_by`에 "한국어 금융 감성 사전 부재"로 등재).
- **결측 규칙**: 첨부 파싱 실패는 `None`. 파싱 실패를 중립(유지)으로 읽지 않는다.

### 5.5 QF-05 공시 정정 빈도 (회계 신뢰도)

- **인용**: Hribar, P. & Jenkins, N. (2004). *The Effect of Accounting Restatements on
  Earnings Revisions and the Estimated Cost of Capital.* RAS 9.
- **재료**: `search_disclosures` — `report_nm`의 `[기재정정]`·`[첨부정정]` 접두
- **LLM 판정**: 정정 사유 서술이 **단순 오기**인지 **수치 변경**인지 분류
- **결정론 채점**: 최근 2년 수치 변경 정정 건수를 구간화(역점수).
  단순 오기 정정은 가중치를 낮춘다.
- **결측 규칙**: 공시 이력 자체가 짧은 신규 상장사는 채점 제외.
- **비고**: 이 팩터의 재료는 **이미 수집기가 만들고 있다**(`status=CORRECTED`).
  MCP 없이도 가장 먼저 만들 수 있는 팩터다.

## 6. 점수 스키마와 PIT

### 6.1 원칙

> **원문을 쌓지 말고, 점수를 관측으로 쌓는다.**

한 번의 채점이 한 행이다. 원문·첨부·청크는 **적재하지 않는다.** 대신 인용 좌표
(`rcept_no` + 문서 내 오프셋)만 남겨 나중에 MCP로 다시 열어볼 수 있게 한다.

### 6.2 테이블 (제안)

```sql
create table research.qualitative_scores (
  id              bigserial primary key,
  instrument_id   uuid not null references reference.instruments(id),
  factor_key      text not null,          -- 'QF-01' ...
  scored_at       timestamptz not null,   -- 채점 시각
  as_known_at     timestamptz not null,   -- PIT 기준시각 (= scored_at)
  score           numeric,                -- 결측이면 null
  available       int not null,           -- 채점된 신호 수 (분모)
  total_signals   int not null,           -- 정의된 신호 수
  verdict         text not null,          -- OK | UNKNOWN | UNOBSERVED
  citations       jsonb not null,         -- [{rcept_no, span, quote_hash}, ...]
  method_version  text not null,          -- methods.py 의 방법 버전
  model_version   text,                   -- LLM 판정에 쓴 모델
  unique (instrument_id, factor_key, as_known_at)
);
```

`as_known_at`은 [`data_resolution.py`](../../departments/04-quant-backtest/pipeline/data_resolution.py)가
`financial_facts`에 이미 쓰는 컬럼명과 같다. 퀀트가 같은 규격으로 읽을 수 있다.

### 6.3 PIT 규칙

1. **forward-only.** 오늘 채점한 점수는 `as_known_at` **이후**의 의사결정에만 쓴다.
2. **소급 생성 금지.** 과거 날짜로 점수를 만들지 않는다. 오늘 조회한 공시로 작년을
   채점하면 그건 look-ahead다.
3. **덮어쓰기 금지.** 재채점은 새 행이다(`unique`가 시각을 포함하는 이유).
4. **한계를 명시한다.** 이 방식은 히스토리가 오늘부터 쌓인다. **초기에는 백테스트 표본이
   부족해 팩터 검증이 불가능하다.** 이 사실을 감추지 않는다 — `methods.py`의
   `validated=False`가 그 자리다.

## 7. 호출 예산과 배치 정책

무료 티어 20,000 req/일. 전 종목 매일 전 팩터는 불가능하다.

- QF-05(정정 빈도)는 **기존 수집 데이터로 계산** — MCP 호출 0
- QF-01·QF-03은 공시 목록 기반 — 종목당 1~2호출, 주 1회 배치
- QF-02는 지분 공시 기반 — 분기 1회
- QF-04는 첨부 파싱이 무거움 — **정기보고서 제출 시점에만**, 종목당 연 4회

호출 실패는 점수 0이 아니라 `verdict=UNKNOWN`으로 남긴다. 실패를 나쁜 점수로 바꾸면
자료가 부실한 회사가 자동으로 나쁜 회사가 된다(`fundamental_scores.py`가 경고하는
바로 그 오류).

## 8. 등재 절차

새 팩터는 코드보다 먼저 [`methods.py`](../../departments/01-research/evidence/methods.py)에
등재한다.

- `citation` 없으면 등재 불가
- 부분 구현이면 `partial_reason`에 무엇이 빠졌는지 적는다
- `validated=False`로 시작한다. `research.analyst_calibration`에 표본이 쌓이기 전에는
  기여를 말하지 않는다
- **`ANALYSTS` 튜플에 정성 분석가 ID를 추가해야 한다** (현재 RES-03~RES-09).
  신규 ID 배정은 리서치본부장 결정 사항이다.

## 9. 미결 사항

이 문서가 정하지 않는 것 — 임의로 정하지 않는다.

1. **정성 분석가의 RES-xx 번호** — 리서치본부장 배정
2. **어느 DART MCP를 쓸 것인가** — 후보와 전송방식 제약은 §3, 선정은 별도
3. **QF-02·QF-03의 구간 임계값** — 표본 없이 정하면 취향이다. 초기에는 구간화 없이
   원값을 남기고, 표본이 쌓인 뒤 정한다
4. **한국어 금융 감성 사전** — QF-04의 사전 기반 점수는 그때까지 BLOCKED
5. **점수를 전략 신호로 승격하는 게이트** — 팩터는 advisory다. 주문·한도로 직행하지
   않는다(CLAUDE.md 권한 분리). 승격 경로는 기존 전략 승격 체인을 따른다
