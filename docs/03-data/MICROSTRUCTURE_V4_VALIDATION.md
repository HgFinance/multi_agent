# 미시구조 v4 검증 기록

기준일: 2026-08-15
대상: `krx-microstructure-daily/v4`, `ms-daily-v4`

## 결론

v4는 공개 OFI 공식을 매개변수만 바꾼 판본이 아니다. 원시 10호가의 공간 구조와
체결 크기 축을 보존해 AST가 다음 불일치를 조합할 수 있게 한다.

- 최우선호가 압력과 깊은 호가 지지가 같은 방향인가.
- 작은 체결을 포함한 전체 흐름과 큰 체결 중심 흐름이 같은 방향인가.
- 위 불일치가 스프레드·일중 유동성 변화와 결합될 때만 예측력이 생기는가.

이 확장 과정에서 v3의 두 데이터 계약 오류도 발견했다.

1. 내부 `depth_imbalance`는 가용 1~10호가 합계였지만 외부 `quotes.bi`는 L1이었다.
2. 외부 `ticks.side`는 `1=매수, 5=매도`인데 v3가 이를 ±1처럼 곱했다. 삼성전자
   2026-08-13 표본에서 잘못된 OFI는 `2.866409`로 정의역 `[-1,1]`을 벗어났다.

v4는 외부 흐름에 수집기가 이미 만든 `ofi_contrib=+volume/-volume`을 사용한다.
v1~v3는 과거 실험 재현을 위해 변경하지 않으며, v3 OFI 계열 성과 수치는 경제적
증거로 재사용하지 않는다.

## 새 실행 필드

| 필드 | 정의 | 범위 |
|---|---|---:|
| `depth_imbalance_l1` | 일중 평균 `(bid_vol1-ask_vol1)/(bid_vol1+ask_vol1)` | [-1, 1] |
| `depth_imbalance_l10` | 일중 평균 `(sum(bid_vol1..10)-sum(ask_vol1..10))/총잔량` | [-1, 1] |
| `depth_imbalance_slope` | `L1-L10` | [-2, 2] |
| `size_weighted_ofi` | `sum(signed_quantity*quantity)/sum(quantity^2)` | [-1, 1] |

기존 `depth_imbalance`는 v4에서 L1로 통일한다. 새 필드를 읽는 AST만 v4를 요구하고,
기존 AST·템플릿은 불변 v3를 계속 읽는다. 따라서 v4 백필·등재 중에도 기존 공장은
중단되지 않는다.

실험의 본체 dataset_id와 별도로, 실제로 붙인 미시구조 데이터셋의 ID·버전·행수·
content hash를 config와 input hash에 봉인한다. 피처 카탈로그 조회도
`feature_set_version='ms-daily-v4'`를 명시해 같은 날짜의 과거 판본을 섞지 않는다.

## 실제 데이터 검증

2026-08-14 외부 FDW 원천 한 거래일을 실제 적재했다.

- 2,507종목, PASS 2,467 / WARN 40 / FAIL 0
- L1·L10 결측 0, `size_weighted_ofi` 결측 36
- 일반 OFI 범위 `[-1.000000, 0.899133]`
- 크기 가중 OFI 범위 `[-1.000000, 0.994576]`
- 공간 기울기 10/50/90백분위 `-0.389588 / -0.086408 / 0.272151`
- L1-L10 횡단면 상관 `0.543051`
- 일반-크기 가중 OFI 횡단면 상관 `0.811327`

삼성전자 2026-08-13 표본은 `L1=-0.041492`, `L10=-0.352099`,
`slope=+0.310606`이었다. 정상화된 일반 OFI는 `+0.066796`, 크기 가중 OFI는
`-0.069674`로 방향이 갈렸다. 새 축이 기존 값을 이름만 바꾼 것이 아님을 확인했다.

## 문헌에서 가져온 설계 원칙

- Cont-Kukanov-Stoikov는 체결량만보다 호가 신규·취소·시장가 이벤트의 OFI와 깊이를
  함께 보는 것이 단기 가격변화를 더 잘 설명한다고 보였다.
  <https://arxiv.org/abs/1011.6402>
- 다중 단계 OFI 연구는 더 깊은 호가 단계를 추가할 때 표본 외 적합도가 개선될 수
  있음을 보였다. <https://arxiv.org/abs/1907.06230>
- Deep Order Flow Imbalance는 원시 호가보다 정상화된 order-flow 입력이 유리하고,
  유효 지평이 종목별 평균 약 두 번의 가격변화처럼 매우 짧을 수 있음을 보고했다.
  <https://papers.ssrn.com/sol3/papers.cfm?abstract_id=3900141>
- 호가 복원력 연구는 충격 후 spread/depth 회복을 약 20번의 최우선호가 갱신 단위로
  관찰한다. <https://arxiv.org/abs/1602.00731>
- NautilusTrader의 공식 데이터 계약도 time/tick/volume/value 및 imbalance/runs
  bar를 구분하며, 정보 기반 bar에는 aggressor side가 필요하다고 명시한다.
  <https://nautilustrader.io/docs/latest/concepts/data/>

## 의도적으로 보류한 것

이벤트 순번·거래량 버킷과 충격 후 회복 AST는 원천 데이터로 계산 가능하다. 그러나
현재 FDW 원천은 하루 약 1,900만 체결·호가 행이고, `row_number` 같은 로컬 창 정렬이
pushdown되지 않으면 원천 전체 전송·정렬 병목이 다시 생긴다. 또한 현 백테스트의
목표 지평은 일 단위다. 따라서 다음 두 조건이 충족되기 전에는 이 기능을 실행 가능한
AST인 것처럼 노출하지 않는다.

1. 원천 쪽에서 event/volume/value bar를 사전 집계하고 처리시간·메모리를 측정한다.
2. 목표 지평을 `days`뿐 아니라 `book_updates`, `price_changes`, `volume`으로 표현하고
   거래비용 후 성과를 재는 실행 엔진을 붙인다.

## 최초 알파 진단 결과

2거래일 지평, 30개 비중첩 구간에서 단일 새 피처는 모두 사전 문턱 `|t|>=3`에
미달했다(`size_weighted_ofi 0.37`, `depth_imbalance_l10 0.28`,
`depth_imbalance_slope 0.13`). 실행 가능한 AST 다섯 개를 모두 공개한 채 추가로
측정했다(`scripts/measure_micro_v4_ast_candidates.py`).

- `abs(rank(L1)-rank(L10))`만 전체 창에서 IC `+0.0190`, t `3.40`이었다.
- 그러나 2026-07-11 이후 11개 구간에서는 IC `+0.0064`, t `0.62`로 붕괴했다.
- 나머지 네 조합은 전체 창에서도 모두 미달했다.
- 전체 창은 이관분 PIT가 `NONE`, 후기 창도 PIT 선언 조회 결과가 `?`라 거래 가능
  근거가 없다.

따라서 v4는 **새롭고 실행 가능한 탐색 좌표를 만들었지만 검증된 알파를 아직
발견하지 않았다.** t=3.40 한 건을 성공으로 승격하지 않고 실패/붕괴 이력으로
남긴다. 다음 후보는 이 결과에 대응하는 새 메커니즘이어야 한다.
