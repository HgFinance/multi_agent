# Production Hedge Fund Data Collection and Governance Guide

> 문서 상태: Draft v1.3
> 최상위 기준: [HEDGE_FUND_MASTER_PLAN.md](../HEDGE_FUND_MASTER_PLAN.md)  
> 적용 대상: Research, Shadow, Paper, Limited Live, Production Proprietary 및 External Capital 환경  
> 목적: 수집해야 할 데이터, 데이터의 소유권·품질·시점·계보·보안·보존·사용권과 운영 절차를 정의한다.  
> 주의: 법정 보존기간, 개인정보, 투자자 기록 및 거래소 데이터 사용권은 관할과 계약에 따라 달라지므로 법률·Compliance 검토 후 확정한다.
> 전사 Source, 부서별 Data Product와 Library 구현안: [RESEARCH_DATA_SOURCES_AND_LIBRARIES.md](RESEARCH_DATA_SOURCES_AND_LIBRARIES.md)
> 팀별 데이터 구현 가이드: [재일](../05-teams/TEAM_JAEIL_RESEARCH_QUANT_GUIDE.md) · [도현](../05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md) · [동규](../05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md) · [영주](../05-teams/TEAM_YOUNGJU_CEO_HR_GUIDE.md)
> Frontend 데이터 계약: [AI_OFFICE_FRONTEND_PLAN.md](../02-engineering/AI_OFFICE_FRONTEND_PLAN.md)

## 1. 문서 목적

헤지펀드의 전략, Risk, 주문, 원장과 NAV는 입력 데이터보다 신뢰할 수 없다. 본 지침은 다음 질문에 일관된 답을 제공한다.

1. 어떤 데이터를 수집해야 하는가?
2. 누가 데이터의 Owner와 Steward인가?
3. 언제 발생했고 시스템이 언제 알게 되었는가?
4. 원본과 정정본을 어떻게 보존하는가?
5. Research, Backtest와 Production이 동일 의미의 데이터를 사용하는가?
6. 데이터 오류가 어떤 Strategy, Decision, Order와 NAV에 영향을 주었는가?
7. 누가 데이터를 조회·수정·배포할 수 있는가?
8. 거래소와 Vendor 계약이 허용하는 범위에서 사용하고 있는가?
9. 장애와 손상 후 어느 시점까지 복구할 수 있는가?
10. 감사 요청에 데이터와 계보를 재현할 수 있는가?

## 2. 비협상 원칙

- 원본 데이터는 수정하거나 덮어쓰지 않는다.
- 데이터 정정은 새로운 Version 또는 Correction Event로 기록한다.
- `event_time`, `received_at`, `observed_at`과 `ingested_at`을 구분한다.
- Backtest는 해당 시점에 시스템이 알 수 있었던 데이터만 조회한다.
- 모든 Production Dataset에는 Owner, Data Contract와 품질 SLO가 있어야 한다.
- 주문, 체결, Position, Cash, Ledger와 NAV 데이터는 Agent가 직접 수정할 수 없다.
- Research Data와 Production-authorized Data를 명확히 분리한다.
- 사용권이 확인되지 않은 데이터는 Production 판단과 외부 보고에 사용하지 않는다.
- 중요한 파생 데이터는 원천 데이터와 계산 Version까지 추적한다.
- 삭제보다 법정·계약상 보존 의무가 우선하며 Legal Hold를 지원한다.
- 데이터 장애는 기술 Incident와 Trading Risk Incident로 함께 평가한다.

### 2.1 전략의 데이터 적격성

데이터가 존재한다는 사실과 전략에 사용할 수 있다는 사실은 다르다. 모든 Strategy Version은 필요한 Data Product 목록을 선언하고 다음 조건을 통과해야 한다.

| 검사 | 확인 질문 | 실패 시 상태 |
|---|---|---|
| Coverage | 필요한 종목·기간·Field가 충분한가 | `RESEARCH_BLOCKED` |
| Point-in-Time | 당시 알 수 없던 수정값이나 미래 정보가 섞이지 않았는가 | `INVALID_DATASET` |
| Quality | 결측·지연·오류율이 전략 SLO 안인가 | `SHADOW_ONLY` 또는 차단 |
| License | Backtest, LLM 입력, 저장과 거래 판단 사용권이 있는가 | `UNAUTHORIZED` |
| Lineage | Raw부터 Feature·Label·Signal까지 재현 가능한가 | `NON_REPRODUCIBLE` |
| Live Parity | 연구 데이터와 운영 데이터의 의미·주기가 일치하는가 | `PAPER_ONLY` |

Long/Short 전략에는 가격 데이터 외에 Borrow Availability, Borrow Fee, Recall과 공매도 규칙 Version이 필요하다. Futures·Options 전략에는 Contract Master, 만기, Multiplier, Settlement, Margin, Chain, Greeks 입력과 Corporate Action Mapping이 필요하다. 누락된 입력을 LLM 추정값으로 채워 전략을 승격하지 않는다.

`StrategyDataEligibility` 결과는 Dataset Manifest와 Strategy Registry에 함께 저장한다. 사용 중인 Data Product가 지연·만료·사용권 변경 상태가 되면 관련 전략의 신규 진입을 자동 차단한다.

## 3. 역할과 책임

| 역할 | 책임 |
|---|---|
| Data Governance Committee | 정책, 중요 데이터, 예외와 우선순위 승인 |
| Chief Data Owner | 전사 데이터 책임과 예산 |
| Domain Data Owner | 데이터 의미, 사용 목적, 품질 SLO 승인 |
| Data Steward | Catalog, 품질, Issue와 Metadata 관리 |
| Data Platform Team | 수집, 저장, 처리, 복구와 Serving |
| Security | 분류, 접근, 암호화와 사고 대응 |
| Compliance/Legal | 사용권, 보존, 개인정보와 규제 적용 검토 |
| Model Risk | Feature, Label, Dataset과 Point-in-Time 검증 |
| Fund Operations | Position, Cash, Ledger와 NAV 정합성 검증 |
| Internal Audit | 통제 Evidence와 예외 독립 검사 |

한 사람이 여러 역할을 맡더라도 Production Dataset의 생성자와 최종 품질 승인자는 분리한다.

## 4. 데이터 Domain 목록

### 4.1 Market Data

- 주식 Trade, Quote, Auction과 Market Status
- 선물 Trade, Quote, Open Interest와 Settlement
- 옵션 Trade, Quote, Option Chain과 Open Interest
- Index와 Benchmark
- Order Book 또는 Market-by-Price
- 거래소 거래 상태, VI, Halt와 Price Limit
- 금리, FX와 필요한 Reference Rate

### 4.2 Reference Data

- Security Master와 영구 Instrument ID
- Symbol, Exchange, Currency와 MIC
- Sector, Industry와 Classification
- Trading Calendar와 Session
- Tick Size, Lot Size와 Contract Multiplier
- Futures Family, Expiry, Roll와 Settlement Rule
- Option Expiry, Strike, Exercise Style와 Settlement Type
- Corporate Action과 Symbol Change

### 4.3 Fundamental and Issuer Data

- 재무제표와 원본 공시
- 실적, Guidance와 Earnings Calendar
- 배당, 자사주, 유상증자와 주식분할
- 임원, 주요주주와 Insider Transaction
- 기업 행동과 신용 관련 Event

### 4.4 News and Document Data

- 기업·산업·거시 뉴스
- 거래소·감독기관·기업 공시
- Earnings Call Transcript
- 중앙은행, 경제지표와 정책 문서
- 내부 Research Note와 Investment Memo
- 문서 원문, Source URL, 게시·관측 시각과 License

### 4.5 Alternative Data

- Sentiment와 공개 소셜 데이터
- Web Traffic, App, Search와 기타 Vendor Dataset
- Prediction Market 또는 설문
- Supply Chain, Shipping 또는 Satellite 계열 데이터

Alternative Data는 개인정보, 이용약관, 대표성, Revision과 Survivorship Bias를 별도 검토한 뒤 사용한다.
X 공개 Post는 승인된 계정 Watchlist와 공식 API로만 수집한다. Platform User ID, Post ID,
게시·최초 관측 시각, 수정·삭제 상태와 수집 목적을 보존하고, 본문·Embedding·장기 Archive는
계약상 허용된 범위에서만 처리한다. 삭제나 비공개 전환은 RAG와 Cache까지 전파한다.
유명 인사의 의견은 `UNVERIFIED_SOCIAL`로 분류하며 공시, 독립 뉴스 또는 시장 데이터로
교차 검증되기 전에는 주문이나 전략 배포의 단독 근거가 될 수 없다.

### 4.6 Trading and Execution Data

- Signal과 Portfolio Intent
- Risk/Compliance Approval
- Internal/Broker Order와 상태 전이
- Execution Report, Fill, Cancel과 Reject
- Drop Copy와 Broker Statement
- 수수료, 세금, Slippage와 Market Impact
- Multi-leg Order와 Leg Allocation

### 4.7 Portfolio and Risk Data

- Position, Cash, Buying Power와 Exposure
- Strategy, Book, Pod와 Fund Allocation
- Delta, Gamma, Vega, Theta와 Margin
- VaR, Stress, Liquidity와 Concentration
- Limit, Breach, Override와 Kill Switch
- Counterparty와 Collateral Exposure

### 4.8 Fund Operations and Accounting

- Double-entry Ledger
- Cash, Position, Fee와 Tax Lot
- Corporate Action Accounting
- Initial/Variation Margin과 Collateral
- Reconciliation Break와 Resolution
- Preliminary, Reviewed와 Official NAV
- Management/Performance Fee
- Investor Capital Account와 Capital Activity

### 4.9 Strategy, Model and Agent Data

- Hypothesis와 Research Mandate
- Dataset Manifest와 Feature/Label Definition
- Experiment, Backtest와 Validation Result
- Model/Strategy Artifact와 Registry State
- Prompt, Model, Tool과 RAG Corpus Version
- Agent Run, Decision, Evidence와 Confidence
- Shadow/Paper/Live Outcome과 Reflection

### 4.10 Operational, Security and Audit Data

- Deployment와 Configuration Change
- Service Metric, Log와 Trace
- Identity, Access와 Secret Audit
- Incident, Work Item과 Approval
- Vendor Status와 Entitlement
- Runbook Execution과 DR Evidence

## 5. 수집 우선순위

### Tier 0: 거래와 원장 필수 데이터

누락 또는 오류 시 신규 주문을 차단한다.

- Broker Order, Execution Report와 Drop Copy
- Position, Cash, Margin과 Collateral
- Risk Limit과 Approval
- Ledger, Reconciliation과 NAV
- Instrument Master, Calendar와 Contract Rule
- 보유 자산의 Trade/Quote와 Market Status

### Tier 1: 실시간 투자 필수 데이터

품질 저하 시 영향 Strategy를 `ENTRY_BLOCKED` 또는 Degraded Mode로 전환한다.

- Realtime Universe Trade/Quote
- Underlying, Futures와 Active Option Chain
- Benchmark, Sector와 Reference Rate
- Corporate Action와 중요 공시
- Production Feature와 Signal 입력

### Tier 2: Research와 판단 강화 데이터

오류 시 관련 Agent/Strategy를 비활성화하고 핵심 거래·원장은 계속 운영한다.

- News, Transcript와 Sentiment
- Fundamentals와 Macro
- Alternative Data
- RAG Document와 Decision Memory

### Tier 3: 분석·보고 편의 데이터

- 비핵심 Dashboard Aggregation
- 장기 연구용 Derived Table
- 실험 중인 Feature
- 비공식 Commentary

AI Office의 캐릭터 위치, 선택 Tab과 화면 Cache도 Tier 3 Projection이다. Agent, 주문, Position, PnL, NAV와 Trading State의 공식 값으로 승격하지 않는다. UI Event는 원본 Domain Event를 대체하지 않으며 재연결 시 공식 Snapshot에서 다시 만든다. 전 종목 Tick 원문은 Pixel Office로 복제하지 않고 필요한 집계 값과 Artifact Reference만 전달한다.

## 6. 데이터 중요도와 보안 분류

| 등급 | 예시 | 기본 통제 |
|---|---|---|
| Public | 공개 공시, 공개 경제지표 | 무결성·출처 검증 |
| Internal | 일반 운영 Metric, 비민감 문서 | 직원·서비스 권한 |
| Confidential | 전략, Position, PnL, Vendor 계약 | 최소 권한, 암호화, 반출 승인 |
| Restricted | Broker Key, 투자자 PII, 송금, Secret | JIT, MFA, 이중 승인, 전량 Audit |

동일 Dataset 안에 여러 등급이 섞이면 가장 높은 등급을 적용하거나 필드 단위로 분리한다.

## 7. 식별자 표준

사람이 읽는 Ticker를 Primary Key로 사용하지 않는다.

필수 식별자:

```text
instrument_id
issuer_id
underlying_id
contract_family_id
fund_id
pod_id
book_id
strategy_id
account_id
counterparty_id
vendor_id
dataset_id
document_id
event_id
decision_id
order_id
fill_id
ledger_entry_id
case_id
```

- `instrument_id`는 Symbol 변경, Exchange 이동과 Corporate Action에도 안정적이어야 한다.
- 공급자 Symbol은 `vendor_symbol_mapping`에서 유효기간과 함께 관리한다.
- 선물과 옵션은 원 계약 ID와 Continuous/Synthetic Series ID를 분리한다.
- 내부 Order ID는 Broker Order ID와 다대다 Mapping을 지원한다.

## 8. 시간 표준

모든 Timestamp는 Timezone이 포함된 UTC로 저장하고 UI에서 시장 시간으로 변환한다.

필수 시간 필드:

| 필드 | 의미 |
|---|---|
| `event_time` | 거래소·원천에서 Event가 발생한 시각 |
| `published_at` | 문서 또는 지표가 공식 게시된 시각 |
| `received_at` | Gateway가 처음 수신한 시각 |
| `observed_at` | 회사 시스템이 처음 알 수 있었던 시각 |
| `ingested_at` | 저장소에 기록된 시각 |
| `effective_from/to` | 사실이 현실에서 유효한 기간 |
| `system_from/to` | 해당 Version이 시스템에 존재한 기간 |
| `corrected_at` | 정정 Version이 생성된 시각 |

시장 데이터는 Exchange Timestamp와 Local Receive Timestamp를 모두 보존한다. 문서는 게시 시각과 최초 관측 시각을 분리한다.

## 9. Point-in-Time과 Bitemporal 원칙

Backtest와 Replay의 기본 조회 조건:

```text
observed_at <= decision_time
AND effective_from <= decision_time
AND (effective_to IS NULL OR decision_time < effective_to)
AND system_from <= replay_as_of
AND (system_to IS NULL OR replay_as_of < system_to)
```

다음 상황을 반드시 Version으로 보존한다.

- 재무제표 Restatement
- 경제지표 Revision
- 뉴스 본문 수정
- Corporate Action 정정
- Vendor Backfill
- 잘못된 Trade/Quote 취소 또는 정정
- Instrument Classification 변경

현재의 최신 정정값을 과거 Backtest에 자동 적용하지 않는다. `as_known_at`과 `latest_corrected` 조회를 별도 API로 제공한다.

## 10. 데이터 저장 Zone

```text
Source
  -> Landing
  -> Raw Immutable
  -> Normalized
  -> Curated / Point-in-Time
  -> Serving / Hot State
  -> Feature / Model Dataset
  -> Archive / Audit Vault
```

### 10.1 Landing Zone

- 수신 직후 임시 저장
- 수신 Batch, File, Message와 Checksum 기록
- Parsing 전 원문 보존
- 실패 데이터도 버리지 않고 Quarantine으로 이동

### 10.2 Raw Immutable Zone

- 공급자 원본 Payload와 Header
- 압축과 Object Versioning
- Append-only
- Source License와 Entitlement Metadata 연결
- 재처리의 기준 원본

### 10.3 Normalized Zone

- Canonical Schema로 변환
- Instrument ID와 UTC 적용
- 단위, Currency와 Decimal 정규화
- 중복 표시와 품질 Flag 포함

### 10.4 Curated Point-in-Time Zone

- 검증된 Business Entity와 관계
- Bitemporal Version
- Corporate Action-adjusted와 Raw View 분리
- Research와 Production 승인 상태

### 10.5 Serving Zone

- Redis 또는 Memory의 Hot State
- SQL/Time-series Serving Table
- RAG Index와 Document Store
- Feature Online Store
- Serving 데이터는 원천이 아니라 재생성 가능한 Cache

### 10.6 Audit Vault

- 주문, 승인, 원장, NAV, Access와 변경 Evidence
- Append-only/WORM 성격
- 일반 Application Admin도 수정 불가
- Legal Hold와 독립 Replica

### 10.7 Cloud-neutral 물리 구현과 AWS 예시

Cloud Provider는 아직 확정하지 않는다. `현재 Core 기준`은 구현팀이 지금 따라야 할 저장소이며 AWS Mapping은 Migration 후보 예시다.

| 논리 Zone | 현재 Core 기준 | Cloud-neutral 요구 | AWS 선택 시 예시 | 핵심 통제 |
|---|---|---|---|---|
| Landing | Supabase private Storage | Versioned Object Storage | S3 Landing Prefix/Bucket | 제한된 수명, Checksum, Quarantine |
| Raw Immutable | Parquet + private Storage | WORM 지원 Object Storage | S3 Versioning + Object Lock | Append-only, 공급자 License와 Retention |
| Time-Series | 별도 TimescaleDB, 리서치·퀀트 전용 | 시계열 DB + 권한 분리 | Managed Timescale 또는 검증된 VM 구성 | 직접 Credential은 리서치·퀀트에만 부여 |
| Operational SoR | Supabase PostgreSQL | HA PostgreSQL | Aurora/RDS PostgreSQL | 거래·승인·원장 ACID와 RLS |
| Normalized | Parquet + Dataset Manifest | Parquet + Data Catalog | S3 + Glue Data Catalog | Schema, Partition, Quality Flag |
| Curated | Parquet + Supabase Metadata | Lakehouse + 세분화된 권한 | S3/Iceberg + Lake Formation | Point-in-Time, 승인 상태, Row/Column 권한 |
| Serving | Redis + Domain API | Redis, Search, Read Model | ElastiCache, OpenSearch | 재생성 가능 Cache와 최소 권한 |
| Feature/Model | private Storage + Supabase Registry | Artifact Storage + Registry | S3 + SageMaker Metadata | Dataset Manifest와 Artifact Digest |
| Audit Vault | Append-only Event + private Storage | 별도 보안 영역의 WORM Vault | Log Archive 계정의 S3 Object Lock | Legal Hold, Cross-Region Replica |

공급자와 무관하게 Production Data, Fund Operations, Trading, Security와 Log Archive의 관리 경계를 분리한다. Cloud별 계정·Bucket·KMS·복구 설계는 공급자 선정 ADR이 승인된 뒤 별도 Architecture 문서로 작성한다.

## 11. Canonical Data Contract

모든 Dataset은 코드와 함께 Version 관리되는 Contract를 가져야 한다.

```yaml
dataset: market_trade_v1
owner: market_data_owner
steward: market_data_steward
criticality: tier_1
classification: confidential
primary_key:
  - provider
  - instrument_id
  - sequence
event_time_field: event_time
freshness_slo_ms: 500
schema:
  price:
    type: decimal
    nullable: false
    min_exclusive: 0
  size:
    type: decimal
    nullable: false
quality_rules:
  - sequence_monotonic
  - event_time_not_future
  - valid_instrument
retention_policy: market_raw_policy
license_policy: vendor_contract_ref
consumers:
  - feature_engine
  - replay
change_policy: backward_compatible_only
```

Contract 변경은 Consumer 영향, Backfill, Dual-read 기간과 Rollback을 포함한다.

## 12. Ingestion 지침

각 Source Adapter는 다음을 구현한다.

- 인증과 Credential Rotation
- Heartbeat와 연결 상태
- Sequence Gap과 중복 탐지
- Snapshot + Incremental 정합성
- Retry, Backoff와 Rate Limit
- Backpressure와 Priority
- Raw Payload 저장
- Checksum과 Message Count
- Source Clock Drift 측정
- Schema Drift와 Unknown Field 기록
- Dead-letter/Quarantine Queue
- 장애 시 Trading State 연동

File 수집은 파일명에 의존하지 않고 Manifest, Size, Hash, Record Count와 Business Date를 검증한다.

## 13. Data Quality Framework

### 13.1 품질 차원

- `Completeness`: 필요한 Record와 Field가 존재하는가?
- `Accuracy`: 외부 기준 또는 독립 Source와 일치하는가?
- `Timeliness`: 결정 시점 전에 도착했는가?
- `Consistency`: Source, Zone과 시스템 간 의미가 일치하는가?
- `Uniqueness`: 중복 Event와 Entity가 제거 또는 표시됐는가?
- `Validity`: Schema, 범위와 Business Rule을 통과하는가?
- `Lineage`: 원천과 변환 Version을 추적할 수 있는가?

### 13.2 품질 심각도

| 등급 | 예시 | 기본 조치 |
|---|---|---|
| DQ-0 Critical | 보유 Position 가격 오류, 원장 유실 | HALT 또는 Reduce Only, SEV Incident |
| DQ-1 High | Sequence Gap, 잘못된 Corporate Action | 관련 Strategy Entry Blocked |
| DQ-2 Medium | 일부 News 지연, Secondary Field 누락 | Quarantine, Degraded Mode |
| DQ-3 Low | 비핵심 Metadata 누락 | Backlog와 정기 수정 |

### 13.3 기본 품질 Rule

Market Data:

- Sequence 단조 증가와 Gap
- Timestamp 미래값과 Clock Drift
- 음수·0 가격, 비정상 Size
- Bid > Ask
- 거래 상태와 가격 제한
- Cross-vendor Price Deviation
- Staleness와 Update Frequency

Reference Data:

- Instrument ID 유일성
- Symbol Mapping 유효기간 겹침
- Calendar와 Session 완전성
- Contract Multiplier와 Expiry 누락

Trading/Ledger:

- Order State 유효 전이
- Fill Quantity가 승인 수량을 초과하지 않음
- Position = 이전 Position + Fill + Corporate Action
- Double-entry Debit/Credit 합계 0
- Broker/Internal Position과 Cash 일치

Document/RAG:

- Source, URL, 게시·관측 시각 필수
- Content Hash와 중복
- Symbol/Issuer Mapping Confidence
- License와 Production 사용 상태
- Prompt Injection Flag

## 14. Quarantine과 정정 절차

품질 Rule을 통과하지 못한 데이터는 삭제하지 않는다.

```text
Detected
-> Quarantined
-> Impact Analysis
-> Owner Review
-> Corrected Version or Rejected
-> Backfill / Recompute
-> Consumer Verification
-> Case Closed
```

정정 시 기록:

- 원본 Record ID
- 오류 유형과 탐지 Rule
- 영향 Dataset/Strategy/Decision/Order/NAV
- 정정 Source와 승인자
- 이전/이후 값
- 재계산 대상과 결과
- 투자·회계 영향

Production 결과에 영향을 준 경우 Data Case를 Risk, Operations와 Internal Audit에 연결한다.

## 15. Market Data 관리

### 15.1 주식

필수:

- Trade와 NBBO 또는 해당 시장 Best Quote
- Session, Auction와 Halt
- Volume, Turnover와 Corporate Action
- Benchmark와 Sector Mapping

선택:

- Full Depth Order Book
- Participant 또는 Venue 정보
- Odd Lot와 Auction Imbalance

### 15.2 선물

- 원 계약별 Trade/Quote
- Open Interest와 Settlement
- Contract Multiplier, Tick Value와 Margin
- Expiry, Last Trading와 Roll Calendar
- Continuous Contract 생성 Rule과 Version

실제 주문·원장은 Continuous Symbol이 아니라 원 계약 ID를 사용한다.

### 15.3 옵션

- Underlying 동시 Quote
- Expiry, Strike, Call/Put
- Bid/Ask와 Size
- Volume와 Open Interest
- 공급자 IV/Greeks가 있으면 원본 보존
- 내부 IV/Greeks와 Pricing Input
- Option Chain Snapshot와 Subscription 이력

Wide, One-sided, Stale Quote는 Surface 입력에서 제외하되 원본은 보존한다.

## 16. Reference Data와 Corporate Action

Reference Data 오류는 여러 시스템에 동시에 전파되므로 Tier 0으로 관리한다.

- Security Master Daily Snapshot과 변경 Event
- Vendor Symbol Mapping과 유효기간
- Merger, Split, Dividend, Rights와 Delisting
- Futures/Options Contract Adjustment
- 거래 Calendar, Holiday와 조기 종료
- Sector/Classification Version

Corporate Action 적용 전 영향 Preview를 생성하고 Position, Price, Share Count, Cost Basis와 Backtest Adjustment를 각각 검증한다.

## 17. Fundamental, Macro와 Document 관리

- 원본 문서와 Parsing 결과를 분리
- Filing Period와 Publication Time 구분
- Reported, Restated와 Normalized 값 분리
- Currency, Unit, Scale과 Accounting Standard 기록
- 경제지표의 Observation Period, Release와 Revision 기록
- Document 언어, Encoding과 OCR 품질
- Summary가 원문을 대체하지 않음

수치 Extraction은 원문 위치, Table, Confidence와 Parser Version을 저장한다.

## 18. RAG Data 지침

### 18.1 Document Metadata

```text
document_id
issuer_id / instrument_ids
source
source_url
source_type
published_at
observed_at
effective_from
ingested_at
language
license_policy
reliability_score
content_hash
parser_version
chunk_version
embedding_model_version
security_classification
production_authorized
```

### 18.2 Chunking

- 제목, Section, Table과 문단 경계를 보존
- Chunk마다 원문 Offset과 Page/Table 위치 기록
- 서로 다른 문서와 Revision을 한 Chunk에 섞지 않음
- Table은 구조화 데이터와 원문 표현을 함께 보존
- Embedding 변경 시 새로운 Index Version 생성

### 18.3 Retrieval

- 먼저 시간, Symbol, Source, License와 권한으로 Filter
- Keyword와 Vector Search 결합
- Source Reliability와 Freshness로 Rerank
- 중복과 Syndicated News 제거
- Agent에 Document ID와 인용 위치 전달
- Production Decision에 사용된 Retrieval Result Snapshot 보존

### 18.4 안전

- 외부 문서의 지시문을 Tool 명령으로 실행하지 않음
- Prompt Injection과 Secret 요청 Pattern 표시
- Untrusted Content와 System Policy를 분리
- 인용 없는 중요 수치와 주장 차단

### 18.5 Hermes Memory·Session Search·Skill 거버넌스

Hermes의 지속 Memory는 Agent가 다음 Session에서도 역할과 교훈을 이어 가기 위한 작은 지식 저장소다. 원시 Market Data, 문서 전체, 최신 Position 또는 공식 의사결정 원장을 복제하는 용도로 사용하지 않는다.

| 분류 | 허용 예 | 금지 예 | 공식 저장소 |
|---|---|---|---|
| 역할 Memory | Mandate 요약, 담당 Queue, 보고 형식 | Tool 권한을 우회하는 지시 | Versioned Agent Profile |
| 사용자 Preference | 보고 주기, 선호하는 설명 수준 | 인증정보, 개인정보 원문 | Supabase `governance` |
| 업무 교훈 | 실패 유형, 다음 실행 시 확인할 Checklist | 근거 없는 시장 사실과 수치 | `audit` Evidence와 원본 Record |
| Session Search | 과거 Case와 Trace를 찾는 검색 단서 | 감사 원장 대체, 최신 상태 판정 | Supabase `audit`·`governance` |
| Skill | 승인된 수집·검증·보고 절차 | 자동 주문, Risk 우회, 자기 권한 확대 | Skill Registry와 Git Version |

Memory 항목에는 가능한 경우 `source_record_id`, `evidence_id`, `observed_at`, `owner`, `classification`, `expires_at`을 함께 둔다. 시간에 따라 변하는 사실은 값 자체보다 공식 Read API와 Snapshot ID를 기억한다. Secret, Broker Token, 개인정보 원문, 미공개 중요정보, 현재 주문·Position·Cash·NAV·Risk Limit은 Memory에 기록하지 않는다.

Skill과 Profile 변경은 다음 상태를 거친다.

```text
CANDIDATE -> EVIDENCE_VERIFIED -> EVAL_READY -> SHADOW
          -> APPROVED -> ACTIVE -> MONITORED
          -> ROLLED_BACK | RETIRED
```

- Candidate를 만든 Agent가 자신의 변경을 단독 승인할 수 없다.
- AI QA/감사본부가 Golden·Adversarial·Regression Eval 결과를 Append-only로 기록한다.
- 인사팀은 기존 Skill 확장과 새 Agent 채용 중 무엇이 적절한지 검토한다.
- CEO Agent는 예산·Mandate·조직 영향이 있는 변경만 승인하며 Risk Policy 변경은 리스크본부 동의가 필요하다.
- 활성 Version은 이전 Version, 배포 시각, 승인 Decision, Dataset·Prompt·Model·Tool Version과 Rollback Target을 가진다.
- Memory 삭제·만료·정정도 Audit Event로 남기며, 파생 요약이 삭제된 원문을 계속 노출하지 않는지 검사한다.

전체 조직 학습 Loop와 본부별 권한은 [마스터 플랜 5.10](../HEDGE_FUND_MASTER_PLAN.md#510-hermes-memory-기반-조직-재귀적-자기-개선)을 따른다.

## 19. Trading, Position과 Ledger 데이터

### 19.1 Source of Truth

| 데이터 | Operational Source | 독립 검증 Source |
|---|---|---|
| Order Intent | Portfolio/Strategy Service | Risk Approval Log |
| Broker Order | OMS | Broker Query |
| Fill | Drop Copy/Execution Report | Daily Statement |
| Intraday Position | Position Service | Broker Position |
| Official Position | Fund Ledger | Broker/Custodian/Admin |
| Cash | Cash Ledger | Bank/Broker/Custodian |
| NAV | Accounting Engine/Admin | Independent Review/Audit |

### 19.2 불변성

- Order와 Fill 상태는 Event로 기록
- Ledger 수정은 반대 분개와 새 분개 사용
- Position Snapshot과 생성 Event Offset 저장
- NAV는 Version 상태를 `preliminary/reviewed/official/restated`로 구분
- 수동 Broker 거래도 External Trade Event로 Capture

## 20. Feature와 Model Dataset 관리

### 20.1 Feature Definition

```yaml
feature_id: relative_volume_5m_v2
owner: quant_research
entity: instrument_id
event_time: event_time
lookback: 5m
inputs:
  - market_trade_v1
calculation_code_hash: sha256:...
point_in_time_safe: true
online_offline_parity_test: required
null_policy: reject
```

### 20.2 Label

- 미래 Horizon과 Price Source 명시
- 거래 비용 전/후 Label 분리
- Delisting와 Missing Future Price 처리
- Overlapping Label의 Purge/Embargo Rule
- Corporate Action와 Session Boundary 처리

### 20.3 Dataset Manifest

```yaml
dataset_id: ds_2026_001
created_at: 2026-07-27T00:00:00Z
as_known_at: 2026-07-26T23:59:59Z
source_versions:
  market_trade: v1
  security_master: 2026-07-26
feature_set: feature_set_v3
label_set: label_set_v2
universe_policy: universe_v4
train_range: 2022-01-01/2024-12-31
validation_range: 2025-01-01/2025-12-31
holdout_range: 2026-01-01/2026-06-30
row_count: 123456789
manifest_hash: sha256:...
quality_report_id: dqr_001
license_scope: research_and_internal_production
```

Dataset을 재생성할 수 없는 실험은 Strategy 승격 대상이 아니다.

## 21. Data Lineage와 Catalog

Catalog 필수 항목:

- Business/Technical 이름과 설명
- Owner와 Steward
- Source와 Vendor
- Schema와 Data Contract
- Criticality와 Classification
- Freshness, Quality와 Availability SLO
- Upstream/Downstream Lineage
- Production Consumer
- Retention과 License
- 최근 품질 Incident
- 마지막 Access Review

Lineage 예시:

```text
Vendor Trade
-> Raw Payload
-> Normalized Trade
-> 1m Bar
-> Feature Snapshot
-> Model Input
-> Agent Decision
-> Order Intent
-> Fill
-> Ledger
-> NAV
```

Column-level Lineage가 어려운 초기에는 Dataset-level부터 시작하되 주문·Risk·NAV 경로는 필드 수준 추적을 우선한다.

## 22. 접근과 Serving 지침

- Production Consumer는 승인된 API/View만 사용
- 운영 DB 직접 Query를 Strategy/Agent에 허용하지 않음
- Read/Write Identity 분리
- Bulk Export는 승인, Watermark와 Audit 요구
- Investor PII와 Strategy IP는 별도 Domain/Key 사용
- Query 결과에 Dataset Version, As-of와 Quality Status 포함
- Cache는 TTL, Source Offset과 Staleness 노출
- `production_authorized=false` 데이터는 주문 경로에서 차단

## 23. Market Data License와 Entitlement

Dataset별 다음 권리를 Catalog에 저장한다.

- Research 사용
- Internal Production 사용
- Display/Non-display 사용
- Derived Data 생성
- Historical 저장
- External Reporting
- 재배포
- 사용자/서버/Device 수
- 국가·법인·계정 제한
- 계약 종료 후 보존·삭제

Entitlement 변경은 영향을 받는 Dashboard, Feature, Model, Agent와 보고서를 자동 식별해야 한다.

## 24. 보존과 Storage Tier

법정·계약상 Schedule이 확정되기 전에는 주문, 체결, 원장, NAV, 승인, 투자자와 Audit 데이터를 임의 삭제하지 않는다.

초기 Storage Tier 원칙:

| 데이터 | Hot | Warm | Cold/Archive |
|---|---|---|---|
| 보유 자산 Market Data | 장중 | 최근 운영기간 | License 허용 장기 Archive |
| 전체 Tick/Quote | 최근 Replay 기간 | 압축 Columnar | 비용·License 기반 |
| Option Chain | Active Chain | Snapshot/압축 | 연구·License 기반 |
| Order/Fill/Risk | 현재 상태 | 전체 검색 가능 | 규정 보존 + WORM |
| Ledger/NAV | 현재 회계기간 | 전체 Version | 장기 불변 보존 |
| Document/RAG | 최신 Index | Version별 Index | 원문 Archive |
| Logs/Traces | Incident 대응기간 | 집계 | Audit 중요 Event만 장기 |

Retention Policy에는 기간, 근거, 삭제 방식, Legal Hold와 Owner가 포함되어야 한다.

## 25. Backup과 Disaster Recovery

- Raw, Contract, Ledger, Audit와 Registry를 별도 Backup Class로 관리
- Backup은 Production Credential과 분리
- 암호화, Versioning와 삭제 방지
- 정기 Restore Test 없이 Backup 성공으로 간주하지 않음
- Event Offset과 Database Snapshot 정합성 기록
- Region 장애 시 Reference, Position, Risk와 Ledger 복구 순서 정의
- 복구 후 Broker/Custodian와 Reconciliation 전 신규 진입 금지

목표 RPO/RTO는 마스터 플랜의 Production SLO와 일치해야 한다.

## 26. 개인정보와 투자자 데이터

- 수집 목적과 법적 근거 기록
- 최소 수집과 목적 외 사용 금지
- PII Tokenization 또는 별도 Vault
- Production 외 환경 Masking
- 투자자 문서와 자금 데이터 접근 이중 통제
- 국외이전, Subprocessor와 Vendor 위치 관리
- 정보주체 요청과 법정 보존 충돌 처리
- Breach 영향 분석과 통지 Runbook

Agent/LLM Prompt에는 원문 PII를 기본적으로 전달하지 않는다.

## 27. 데이터 변경관리

변경 유형:

- Source/Vendor 변경
- Schema 추가·삭제·Type 변경
- Business Definition 변경
- Corporate Action 처리 변경
- Feature/Label 변경
- Retention/License 변경
- Quality Rule과 Threshold 변경

필수 절차:

1. Change Proposal
2. Consumer/Lineage Impact
3. Contract Compatibility Test
4. Backfill과 Dual-run 계획
5. Model/Strategy 재검증 필요성 판단
6. 승인과 배포
7. 관찰기간
8. 구 Version 폐기

Production Consumer가 있는 Field 삭제·의미 변경은 Dual-read 기간 없이 배포하지 않는다.

## 28. Data Incident 대응

Incident 예시:

- 실시간 Feed Gap 또는 Staleness
- 잘못된 Corporate Action
- Broker/내부 Position 차이
- Future Data Leakage
- Vendor가 과거 데이터를 무통보 수정
- License/Entitlement 위반
- 투자자 데이터 노출

대응 순서:

```text
Detect
-> Classify Criticality
-> Contain Consumers
-> Trading State Decision
-> Preserve Evidence
-> Identify Affected Lineage
-> Correct / Backfill
-> Recompute Decisions and NAV
-> Independent Verify
-> Resume
-> Postmortem
```

Resume 전 Data Owner, Risk와 Operations 승인이 필요한 Critical Dataset 목록을 사전에 정의한다.

## 29. 일일 운영 Checklist

### 장전

- Security Master와 Trading Calendar 변경 확인
- Corporate Action와 Contract Expiry 반영
- Market Data Entitlement와 Feed Health
- 전일 Broker/Position/Cash Break 해결
- Feature Online/Offline Parity와 Model Input Freshness
- Production Dataset Quality Certification

### 장중

- Feed Sequence, Staleness와 Cross-vendor Deviation
- 보유 자산과 Active Chain 품질
- Data Quality Incident와 영향 Strategy
- Cache/Serving Lag와 Event Bus Backlog
- Broker Order/Fill와 Position Projection

### 장후

- Raw Record Count와 Checksum
- Broker/Custodian/FCM Reconciliation
- Settlement, Corporate Action와 Ledger
- Preliminary/Official NAV Data Quality
- Backfill, Quarantine와 미해결 Case
- 다음 거래일 Calendar/Expiry 준비

## 30. 핵심 Metric과 SLO

수집:

- Source Availability
- Record/Message Rate
- Sequence Gap과 Duplicate Rate
- p50/p95/p99 Ingestion Latency
- Clock Drift

품질:

- Contract Pass Rate
- Null/Invalid/Quarantine Rate
- Cross-source Deviation
- Freshness SLO 위반
- Correction과 Backfill 수

운영:

- Data Incident MTTD/MTTR
- 영향을 받은 Strategy/Order/NAV 수
- Dataset Certification 정시율
- Unresolved Data Case Aging
- Restore Test 성공률

거버넌스:

- Owner/Contract/Lineage 보유율
- Production-authorized Dataset 비율
- License/Retention Metadata 완전성
- Access Review와 Entitlement 예외
- 미승인 Direct Query 수

## 31. 구현 단계

### Phase D0: Inventory와 Ownership - 1~2주

- 전체 Source/Dataset Inventory
- Owner, Steward, Criticality와 Classification
- Vendor/License Register
- Canonical ID와 시간 표준

### Phase D1: Raw와 Contract - 3~5주

- Landing/Raw Immutable Zone
- Data Contract Registry
- Checksum, Sequence와 Quarantine
- Market/Reference Tier 0·1 수집

### Phase D2: Point-in-Time와 Quality - 6~8주

- Bitemporal Model
- Quality Rule Engine
- Corporate Action Versioning
- Data Incident와 Impact Lineage

### Phase D3: Research/RAG Dataset - 9~11주

- Document Metadata와 Chunk Version
- Dataset Manifest
- Feature/Label Registry
- Online/Offline Parity

### Phase D4: Production Certification - 12~14주

- Production Authorization Gate
- Entitlement Enforcement
- Backup/Restore와 DR
- Daily Data Certification
- Broker/Ledger/NAV Reconciliation

이 Workstream은 마스터 플랜 개발 로드맵과 병렬로 실행한다.

## 32. Production Data Launch Gate

- 모든 Tier 0·1 Dataset에 Owner와 Steward가 지정됐다.
- Data Contract와 Schema Compatibility Test가 있다.
- `event_time`, `observed_at`, `ingested_at`을 구분한다.
- Point-in-Time Replay와 미래 데이터 유입 검사를 통과한다.
- Production Source의 License와 Entitlement가 승인됐다.
- 보유 자산 Feed Gap과 Staleness가 Trading State에 연결된다.
- Broker Order/Fill/Position/Cash를 독립 Source와 대사한다.
- Ledger와 NAV의 Version, Correction과 Audit가 동작한다.
- Dataset-to-Decision-to-Order-to-NAV Lineage를 조회할 수 있다.
- Backup Restore와 목표 RPO/RTO를 실제 측정했다.
- Data Incident, Quarantine, Backfill과 Resume Runbook을 훈련했다.
- Restricted/PII 데이터의 접근과 LLM 전달 통제를 검증했다.
- Versioning/WORM, 세분화된 Data Access와 암호화 Key 분리가 검증됐다.
- Tier 0·1 데이터의 Cross-Region 복제와 실제 Restore를 검증했다.

## 33. 금지 사항

- Ticker를 영구 Primary Key로 사용
- 수정 데이터를 원본 위에 덮어쓰기
- 게시 시각 없이 뉴스·문서를 Backtest에 사용
- 현재 구성종목으로 과거 Universe 재구성
- 현재 Option Chain을 과거 시점에 적용
- License가 불명확한 데이터를 Production Agent에 제공
- Agent가 운영 DB에 임의 SQL 또는 Write 수행
- Quality Flag 없는 Forward Fill
- 수동 Spreadsheet를 공식 Position/NAV 원장으로 사용
- Dataset Manifest 없는 전략 승격
- Restore Test 없이 Backup 완료 처리
- 미해결 Tier 0 Data Incident 상태에서 신규 진입 재개

## 34. 샘플 Dataset Register

| Dataset | Tier | Owner | Source | Freshness | Production 사용 |
|---|---|---|---|---|---|
| security_master | 0 | Reference Data | Exchange/Vendor | 장전 확정 | 필수 |
| market_trade | 1 | Market Data | Exchange/Vendor | 실시간 | 필수 |
| market_quote | 1 | Market Data | Exchange/Vendor | 실시간 | 필수 |
| futures_contract | 0 | Derivatives Data | Exchange/Vendor | 장전+변경 | 필수 |
| option_chain | 1 | Derivatives Data | Exchange/Vendor | 실시간 | 제한 Chain |
| corporate_action | 0 | Reference Data | Exchange/Vendor | Event 기반 | 필수 |
| news_document | 2 | Research Data | Licensed Source | 분 단위 | 승인 Source |
| broker_execution | 0 | Trading Ops | Broker/Drop Copy | 실시간 | 필수 |
| position_cash | 0 | Fund Operations | Internal+Broker | 실시간/Close | 필수 |
| fund_ledger | 0 | Fund Accounting | Internal | Event 기반 | 필수 |
| official_nav | 0 | Fund Accounting | Admin/Internal | Daily | 필수 |
| agent_decision | 2 | AI Platform | Internal | Event 기반 | 근거 저장 |
| model_dataset | 2 | Quant Research | Curated | Version 기반 | 승인 필요 |

## 35. 최종 운영 원칙

> 모든 Production Decision은 당시 시스템이 알 수 있었던 승인된 데이터로 재현돼야 한다. 모든 데이터는 Owner, Contract, Quality, Point-in-Time, Lineage, License, Retention과 Access Policy를 가져야 하며, 오류 발생 시 영향 Strategy·Order·Ledger·NAV를 추적하고 안전하게 중단·정정·재개할 수 있어야 한다.
