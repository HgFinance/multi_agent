# Intraday Microstructure Alpha Lane

> 구현 기준: `krx-intraday-causal-v1`  
> 목적: 일봉으로 압축되던 호가·체결 가설을, 실제로 주문 가능한 시간축에서 검증한다.

## 결론

기존 `krx-microstructure-daily` v1~v5는 종목·일자 한 행이므로 데이터 품질과
일봉 보조 신호에는 유효하지만, 수초~수분짜리 주문장 신호의 주 실행면이 될 수 없다.
새 lane은 일봉 공장을 변경하지 않고 다음 순서를 별도로 고정한다.

```text
수신 완료 호가/체결
  -> decision_time 이전 정보만 feature 계산
  -> 사전등록 latency 이후 진입 호가
  -> 5초/30초/300초 markout
  -> taker 왕복 또는 보수적 passive FIFO 체결
  -> 비용 후 OOS edge
```

방향 예측 정확도나 mid-price IC는 최종 합격 기준이 아니다. 실제 진입·청산 가격의
스프레드를 넘고, 체결된 거래의 비용 후 성과가 양수여야 한다.

## 문헌에서 채택한 원칙

| 근거 | 채택한 규칙 |
|---|---|
| [Cont, Kukanov, Stoikov — Price Impact of Order Book Events](https://arxiv.org/abs/1011.6402) | 체결량만이 아닌 best bid/ask 신규·취소·가격 이동을 포함한 quote-event OFI |
| [Xu, Gould, Howison — Multi-Level OFI](https://arxiv.org/abs/1907.06230) | L1과 L10 불균형을 분리하여 깊은 주문장 정보 손실 방지 |
| [Cont, Cucuringu, Zhang — Cross-Impact OFI](https://arxiv.org/abs/2112.13213) | 지연 OFI 효과는 빠르게 감쇠하므로 일봉이 아닌 복수 초단기 horizon 사용 |
| [Stoikov — Micro-Price](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2970694) | spread와 L1 imbalance를 결합한 microprice offset을 독립 feature로 측정 |
| [Briola, Bartolucci, Aste — Deep LOB Forecasting](https://arxiv.org/abs/2403.09267) | 분류 정확도 대신 완결 가능한 거래와 실행 성과 평가 |
| [Huang, Lehalle, Rosenbaum — Queue-Reactive Model](https://arxiv.org/abs/1312.0563) | passive 주문을 상태 의존 queue로 취급하고 단순 무조건 체결 금지 |
| [Lokin, Yu — Fill Probabilities](https://arxiv.org/abs/2403.02572) | passive/active 선택에서 fill probability를 별도 결과로 기록 |
| [KRX 주문 유형·체결 원칙](https://global.krx.co.kr/contents/GLB/06/0602/0602020202/GLB0602020202T5.jsp) | 연속매매에서 가격·시간 우선 원칙 적용, FIFO queue-ahead 모델의 거래소 근거 |
| [KRX 거래시간](https://global.krx.co.kr/contents/GLB/06/0602/0602020204/GLB0602020204T1.jsp) | 기본 연구 구간을 정규 연속매매 09:00~15:20 KST로 제한; 동시호가는 별도 가설 |

## 결정론적 시간 계약

원천의 세 시각을 섞지 않는다.

- `event_time`: 거래소 사건 시각
- `received_at`: 우리 수집기가 받은 시각
- `observed_at`: 저장·검증 계층에서 관측 가능한 시각
- `available_at = max(received_at, observed_at)`

feature는 `event_time <= decision_time`이면서
`available_at <= decision_time`인 사건만 사용한다. 진입은
`decision_time + order_latency`보다 늦지 않게 알려진 최신 유효 호가다. 각 label은
진입 이후 horizon의 최신 유효 호가를 사용한다. train/test purge 간격은 가장 긴
horizon과 latency의 합보다 짧을 수 없다.

`received_at`이 없는 이관 데이터는 이 lane에서 사용하지 않는다. exchange timestamp가
초 단위 반올림 때문에 수신 시각보다 약간 미래로 보이면 미래 사건을 채택하지 않고 그
전에 알려진 유효 호가로 되돌아간다.

best ask가 best bid보다 낮은 crossed quote와 0 이하 가격은 feature에서 제외한다. 다만
조용히 버리지 않고 `source_quality`에 원천 건수를 기록하여 데이터 경보와 전략 성과를
분리한다. 유효 sample이 0개면 causality를 `PASS`로 보이지 않고 `NO_EVIDENCE`로
종결한다.

## 현재 실행 모델

### Taker

매수는 ask에 진입하고 미래 bid에 청산한다. 매도는 bid에 진입하고 미래 ask에
청산한다. 양쪽 fee도 차감한다. 따라서 mid-price markout이 양수여도 spread보다 작으면
자동 기각된다.

### Passive FIFO lower bound

현재 L10 데이터는 개별 주문 ID가 없는 snapshot이다. 정확한 queue position이라고
주장하지 않는다. 진입 시 보이는 동일 가격 잔량을 전부 `queue_ahead`로 두고, 이후
반대 방향 실제 체결량이 그 잔량을 넘어야만 체결로 인정한다. 취소 잔량은 전혀
credit하지 않으므로 보수적 lower bound다. 정확한 체결확률 모델은 MBO 또는 주문
ID·취소 이벤트가 수집될 때까지 `passive_exact_queue_supported=false`로 fail closed 한다.

## 실데이터 acceptance 결과

2026-08-14 삼성전자(`005930`) 09:30~10:30 KST 한 시간에 실행했다.

- 원시 호가 3,687행, 체결 103,980행
- 5초 causal sample 642개
- 수수료는 아직 0bps로 두었으므로 아래 손실은 spread만으로도 발생한 보수적 진단
- 미래 정보·stale quote 위반 0건, causality `PASS`
- 모든 단순 feature와 8-feature purged expanding ridge가 비용 후 `REJECT`
- joint model taker OOS:
  - 5초: mid markout `+2.34bps`, 실행 `-1.95bps`, 48거래
  - 30초: mid markout `+4.58bps`, 실행 `-3.57bps`, 47거래
  - 300초: mid markout `+12.61bps`, 실행 `-2.61bps`, 252거래
- L1 imbalance passive lower bound:
  - fill rate 5초 `1.30%`, 30초 `13.04%`, 300초 `64.05%`
  - 체결당 net edge는 세 horizon 모두 음수

이는 한 종목·한 시간의 진단이므로 알파 유무를 일반화하지 않는다. 다만 기존 일봉
결과로는 보이지 않던 핵심 병목, 즉 **예측력은 일부 있어도 spread와 adverse selection을
넘지 못한다**는 사실을 실제 데이터로 분리 측정했다.

후속 다종목·다일 진단에서 2026-08-14 SK하이닉스 300초 joint model이 한 번
`+8.89bps`를 보였지만 알파로 승인하지 않았다. 같은 구간의 8월 12일과 13일은 각각
`-15.88bps`, `-16.92bps`였고, 8월 14일 원천 호가도 4,761건 중 crossed quote
451건으로 `WARN`이었다. 8월 4~11일 이관 구간은 `received_at`이 없어 sample 0개,
`NO_EVIDENCE`로 차단된다. 즉 새 lane은 우연한 하루의 양수 결과와 PIT 불가능 데이터를
성공 사례로 세탁하지 않는다.

## 실행

```bash
python scripts/measure_intraday_microstructure.py \
  --symbol 005930 \
  --start 2026-08-14T00:30:00Z \
  --minutes 60 \
  --sample-seconds 5 \
  --lookback-seconds 30 \
  --horizons 5 30 300 \
  --latency-ms 250
```

출력에는 causal audit, 각 단일 feature의 taker/passive 성과, spread-abstention을 적용한
purged walk-forward joint model이 함께 포함된다.

## 다음 단계

1. 이 lane의 feature schema를 별도 intraday AST 타입으로 노출한다. 기존 daily AST와
   같은 `MICROSTRUCTURE` 이름으로 섞지 않는다.
2. 여러 종목·여러 거래일을 streaming partition으로 측정하고, 종목·일자 단위로
   walk-forward fold를 구성한다.
3. signal threshold를 test 결과로 고르지 않고 train 내부에서만 calibration하며,
   최종 주차는 에이전트가 보지 못하는 holdout으로 봉인한다.
4. MBO가 없으면 passive 결과는 lower bound로만 사용한다. exact queue simulator를
   통과한 것처럼 승격하지 않는다.
5. 탐색 목적함수를 `mid IC`가 아니라 `net edge × fill probability × capacity`와
   stability/DSR/PBO의 다목적으로 바꾼다.

## 2026-08-16 공장 통합 상태

위의 1~5번은 이제 별도 측정 스크립트가 아니라 정식 연구→퀀트 원장 경로에 연결됐다.

- `ExperimentProposalV1`은 `research_lane`과 Event/Context/Qualities/Direction/Output
  `semantic_plan`을 보존한다. `INTRADAY_EVENT`는 raw `market_quotes`와
  `market_ticks`, `intraday_signal_expr`가 없으면 발행되지 않는다.
- `intraday_alpha_ast.py`는 초 단위 lag/rolling/ewma/z-score와 상태 조건을 지원하고,
  물리 단위가 다른 값을 더하거나 비교하는 수식을 Gate 0에서 거부한다.
- Gate 0는 이야기의 event/context와 수식의 실제 관측 필드를 대조한다. 예를 들어
  `ORDER_FLOW` 가설을 microprice-only 수식으로 제출하거나 `TIGHT_SPREAD` 조건을
  spread 필드 없이 구현하면 실험 예산을 쓰기 전에 거부된다.
- 숫자 horizon·threshold 변경은 새 idea family가 아니다. semantic fingerprint와
  AST shape fingerprint가 함께 trial family를 정한다. 따라서 같은 실행 목표라도
  경제적으로 다른 수식 구조는 남의 시도 예산을 물려받지 않고, 같은 구조의 창·상수
  튜닝은 한 family로 합산된다. exact fingerprint는 과거의 동일 수식 재사용을 막는다.
- `intraday_experiment_runner.py`는 평가 직전 최대 5개 보정 세션에 인과적으로 유효한
  quote와 trade가 함께 존재한 `krx_all` 전 종목을 고정하고 그 다음 세션들만 평가한다.
  수익률이나 quote-event 수로 top-N을 고르지 않는다. 종목은 메모리를 제한하는 내부
  shard로만 나누며 모든 shard를 동일한 experiment·trial·다중검정 원장에 합친다.
  shard 크기는 실행 세부사항이라 실험 identity나 새로운 시도로 세지 않는다.
- 호출 시각은 실험 identity에서 제외한다. 같은 세션·종목·원천 계보의 재시도는 완료
  결과를 재사용하고, 원천 행 수나 관측 시각이 달라진 경우에만 새 실험으로 센다. 실패한
  동일 입력은 한 worker만 원자적으로 다시 점유해 무한 호출이 trial 수를 부풀리지 않는다.
- 겹치는 5초 표본을 독립 관측치로 세지 않는다. KRX 세션별 PnL로 축약한 뒤 session
  bootstrap, DSR, walk-forward fold, family PBO를 적용한다. PBO는 현재 실험을 공통
  원장에 기록한 뒤 계산하며, 4개 이상의 비교 가능한 family variant가 없으면
  `PBO_UNMEASURED`로 HOLD한다.
- shard 실행기는 원시 호가·체결·표본을 shard가 끝날 때 폐기하고 합계·세션 수익·정확한
  포트폴리오 동시기회 timestamp delta만 누적한다. capacity 하위 분위수도 결정론적
  10,000개 reservoir로 제한한다. 요청 종목 중 sample이 생긴 비율이 80% 미만이면
  `INSTRUMENT_COVERAGE_BELOW_MINIMUM`으로 HOLD하여 일부 종목 결과를 전체처럼 보이지 않는다.
- passive는 미체결 기회를 0 PnL로 포함한다. 체결된 건만 보고하는 selection bias를
  막으며 snapshot L10의 결과는 계속 `FIFO_NO_CANCELLATION_CREDIT_LOWER_BOUND`이다.
- 비용을 생략해도 0bp로 돌지 않는다. `krx-intraday-execution-v1`은
  [2026년 상장주식 매도세](https://kind.krx.co.kr/external/2026/03/18/002312/20260318010780/11011.htm)
  20bp(KOSPI 거래세 5bp+농특세 15bp, KOSDAQ 거래세 20bp)와
  [대표 온라인 위탁수수료](https://kind.krx.co.kr/external/2026/05/15/001427/20260515003114/11013.htm)
  1.5bp/side를 대칭 환산한 11.5bp/side를 기본으로 쓰며, 10bp 미만 입력은
  Gate 0에서 거부한다. 실제 계좌 비용이 더 높으면 사전등록 값도 올려야 한다.
- 종목별 point-in-time 대차 가능 여부·차입료·공매도 체결 제약은 현재 원천에 없다.
  따라서 정식 lane의 `position_mode`는 `LONG_ONLY`만 허용하고 음수 신호는 abstain한다.
  `LONG_SHORT`를 비용 없는 숏으로 실행해 수익을 부풀리는 경로는 Gate 0에서 막는다.
- 각 기회의 L1 실행 가능 수량, 하위 10% 수량, horizon 내 최대 동시 포지션 수도
  원장에 남긴다. 이는 수익 신호와 별개인 capacity/portfolio 병목 진단이며, 실제 주문
  크기를 자동 승인하는 값은 아니다.
- Quant의 최대 결정은 `SUBMIT_TO_QA`다. 이 값은 production 승격이 아니며 Risk·QA·CEO
  승인 경계를 그대로 유지한다.
- intraday 후보가 SHADOW에서 PAPER 이상으로 가려면 최소 1,000개 실관측 이벤트,
  양(+)의 live net markout, 사전등록 latency 이내의 p95가 필요하다. Passive라면 예측
  fill과 실제 fill의 calibration MAE도 0.15 이하여야 한다. 백테스트 Sharpe만으로는
  이 실행 현실성 관문을 통과할 수 없다.
- 완주 결과와 실패 교훈은 `intraday_experience.py`가 매 주기 원장에서 재계산해 Scout와
  Planner 모두에게 준다. 빈 semantic cell, 실패 shape, positive/negative-associated
  component를 보여 주되 인과 기여라고 주장하지 않는다. 다음 후보는 빈 cell 탐색,
  단일 메커니즘 편집, 서로 다른 조각 재결합 중 하나를 명시해야 한다.
- Hermes/LLM은 숫자를 OOS 결과에 맞추는 optimizer가 아니라 금융수학적 equation
  skeleton과 경제적 prior의 제안자다. 모든 신규 `INTRADAY_EVENT/AST_READY` 리드는
  `FORMULA_THESIS`에 목표(`MIDPRICE_MARKOUT`/`TAKER_NET_PNL`/`PASSIVE_FILL_ADJUSTED_PNL`), 함수형태,
  예상 부호, 계수 정책, AST 각 field의 경제적 역할, 반증 가능한 식별 예측을 적는다.
  결정론 validator는 target과 semantic output의 일치, AST의 단위·복잡도와 함께
  `STATE_CONDITIONAL`의 `where`, `CROSS_SCALE`의 복수 clock, `DEPTH_DIVERGENCE`의
  L1/L10 동시 사용처럼 주장한 함수형태가 식에 실제로 보이는지 검사한다. OOS 계수
  fitting은 허용하지 않으며, 12개 population·ablation·실패 기억을 통한 구조 탐색만 한다.
  quality-diversity 점수는 어떤 후보를 먼저 시험할지 정할 뿐 성과나 승격 판정이 아니다.

- 단위가 맞는 것만으로도 충분하지 않다. 수식 접수기는 각 field가 값 경로(`VALUE`)나
  명시적 상태 분기(`GATE`)에 실제로 영향을 주는지 구조적으로 감사한다. 항상 음이 아닌
  `trade_count`·depth·spread에 `sign()`을 씌워 거의 상수 +1로 만든 장식 항,
  동일식의 `x-x`/`x/x`, 같은 then/else를 가진 `where`, 0을 곱해 다른 항을 지운 식은
  백테스트 전에 거부한다. 기존 계약 리드도 Proposal 조립과 Publish Gate에서 현재 v3
  감사기를 다시 통과해야 하므로, 과거 validator의 허점을 이용한 식이 그대로 실행되지
  않는다. 거부 응답은 무차원 pressure를 경제적으로 정당화한 BPS scale과 결합하는 법,
  signed change에는 `rolling_zscore`/`delta`, 진짜 상태에는 `where(gt(...))`를 쓰는
  비자동 수리 힌트를 돌려준다. 코드는 가설을 몰래 바꾸지 않고 Hermes가 근거·ablation과
  함께 다시 제출하게 한다.

### 공유 재생 진화 평가 v5

수식 생성량보다 원시 호가·체결 재생 시간이 훨씬 큰 현재 병목에서는 12개 초안을 각각
독립 raw replay에 넣지 않는다. Planner는 서로 다른 니치의 계약 유효 v3 리드를 한
Proposal에 2~8개까지 연결하고, 그중 하나만 주 수식으로 사전등록한다. 접수기는 나머지
정확한 lead AST와 명시된 lineage parent를 최대 7개의 `SCREENING_ONLY` sidecar로
자동 부착한다. 에이전트가 sidecar 본문을 직접 쓰거나 출처·fingerprint를 바꾸면
`SCREENING_POPULATION_INVALID`로 거부한다.

런타임은 모든 후보가 요구하는 clock과 horizon의 합집합으로 lane spec을 한 번 만들고,
passive 후보가 하나라도 있으면 passive label까지 포함한 상위집합을 한 번 생성한다.
각 instrument/session 표본은 한 번 정렬·인과성 감사된 뒤 후보별 독립 누산기로 전달된다.
후보마다 기회, 비용 후 net, gross markout, fill, coverage, session bootstrap, DSR은 따로
계산되며 원장 metric에는 `screening_candidate=<ast_fingerprint>` 차원을 붙인다. 주 결과를
읽는 Orchestrator·복구기·실험 카드는 이 차원을 명시적으로 제외한다.

선별 후보는 net/gross/coverage/주 수식 대비 구조 novelty/복잡도의 비지배 순위를 받지만
결정은 항상 `SCREENING_ONLY`다. 양의 net 또는 Pareto 1위여도 production이나 QA로
승격할 수 없고, 다음 주기의 독립 주 실험 후보(`SCREEN_SURVIVOR`)가 될 뿐이다. 반대로
그 측정치는 실패·성공 기억과 다음 세대 번식에는 사용한다. sidecar 노출은 공짜 탐색이
아니므로 현재 DSR trial 수와 이후 exact-formula trial 조회에 포함한다. sidecar의 source
lead id는 소비하지 않아 독립 확인 경로를 남기되, 반복 선별도 장부에서 누적된다.

이 구조는 희소한 최종 보상 하나보다 구조·신규성 피드백을 쓰는
[AlphaSAGE](https://arxiv.org/abs/2509.25055), 성공·실패를 다음 생성에 증류하는
[FactorMiner](https://arxiv.org/abs/2602.14670), 항별 영향도를 탐색 신호로 쓰는
[IGSR](https://arxiv.org/abs/2605.29184), 다양성 archive를 보존하는
[MAP-Elites](https://www.frontiersin.org/journals/robotics-and-ai/articles/10.3389/frobt.2016.00040/full),
저비용 단계에서 후보를 넓게 비교하고 생존자에 자원을 집중하는
[Hyperband](https://www.jmlr.org/papers/v18/16-558.html)의 공통 원칙을 현재 PIT·비용·승격
경계 안에 제한적으로 옮긴 것이다. 수식을 잘 찍는 것만으로 충분하지 않다는 지역별
실증 경고도 반영했다. 중국의
[칭화대 GP 팩터 연구](https://newetds.lib.tsinghua.edu.cn/qh/paper/summary?dbCode=ETDQH&sysId=294427)는
감쇠·과적합·거래 규칙과 비용을, 일본의
[호가장 GP 일중 전략 연구](https://www.jstage.jst.go.jp/article/iscie1988/21/12/21_12_400/_article/-char/ja/)는
호가 정보와 거래 규칙의 공동 평가를 다룬다. 이 문헌의 보고 수익을 우리 성과로
간주하지 않고 생성기와 검증기의 결합 방식만 참고한다.

운영 전제는 `20260816150000_intraday_alpha_factory.sql` 적용과
`krx-intraday-events/v1` manifest 존재다. 원천에 `received_at`이 없으면 해당 행은
증거에서 제외한다. 가용 세션이 60개 미만이어도 10세션 이상이면 측정은 수행하되,
알파 실패로 오인하지 않고
`INCONCLUSIVE/UNDERPOWERED_DATA`로 환류한다. 즉 짧은 표본의 탐색 비용은 감당하지만
그 표본을 검증 완료로 과장하지 않는다.

문헌에서 직접 반영한 설계 축은 semantic search의 [AlphaSchema](https://arxiv.org/abs/2607.26642),
구조 편집 기억의 [AlphaMemo](https://arxiv.org/abs/2606.20625), 성공·실패 skill memory의
[FactorMiner](https://arxiv.org/abs/2602.14670), 생성·평가·탐색 분리의
[AlphaBench](https://openreview.net/pdf?id=d97Q8r7ZKZ), 검증 가능한 진화 탐색의
[AlphaEvolve](https://deepmind.google/blog/alphaevolve-a-gemini-powered-coding-agent-for-designing-advanced-algorithms/),
수식 skeleton과 scientific prior 분리의 [LLM-SR](https://arxiv.org/abs/2404.18400),
evaluator·island·짧은 skeleton을 결합한
[FunSearch](https://www.nature.com/articles/s41586-023-06924-6), LLM 단독보다 기존 탐색기의
초기화·정체 탈출에 LLM을 쓰는
[HARLA](https://journal.hep.com.cn/fcs/EN/10.1007/s11704-025-41061-5), 그리고 다면 평가의
[AlphaEval](https://arxiv.org/abs/2508.13174)이다. 이들 결과의 수익률을 이 시스템의
성과로 전용하지 않고 생성기·검증기·기억의 역할 분리만 채택한다. 통계 관문은
[Deflated Sharpe Ratio](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=2460551),
[SPA](https://papers.ssrn.com/sol3/papers.cfm?abstract_id=264569),
[PBO](https://escholarship.org/uc/item/4w1110bb)의 “탐색 횟수를 증거에 포함한다”는 원칙을
따른다.
