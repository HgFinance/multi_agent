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
