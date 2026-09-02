# 자연어 조건주문 AST 공정 평가 — 2026-09-01

## 결론

현재 결과는 **최종 성능 인증이 아니라 single-annotator held-out pilot**이다.
실제 Trading Hermes와 기존 `process_user_conditional_paper_rule` 경계를 사용한
30건×3회 평가에서 다음 결과를 얻었다.

| 지표 | 결과 | 95% CI | 판정 |
|---|---:|---:|---|
| AST exact match | 50/63 (79.37%) | Wilson 67.83–87.52% | 개선 필요 |
| 필드 Macro F1 | 95.70% | Bootstrap 90.83–99.11% | 필드 대부분은 맞지만 exact 오류를 가림 |
| Strict end-to-end correctness | 74/90 (82.22%) | Wilson 73.06–88.75% | 개선 필요 |
| 의미 동등성 Replay | 14/18 (77.78%) | Wilson 54.79–91.00% | 1회차·상태 없는 케이스 기준 |
| 모호 입력 사유 정확도 | 15/15 (100%) | Wilson 79.61–100% | Pilot 통과 |
| 미지원 입력 사유 정확도 | 9/12 (75.00%) | Wilson 46.77–91.11% | 개선 필요 |
| 안전한 reject-vs-accept | 27/27 (100%) | Wilson 약 87.54–100% | 오활성화 0건 |
| 조건 MCP exactly-once | 89/90 (98.89%) | Wilson 93.97–99.80% | 1건 malformed JSON |
| 지원 후보의 deterministic boundary 승인 | 58/63 (92.06%) | Wilson 82.73–96.56% | 잘못된 AST 승인도 있어 정확도 지표는 아님 |
| 동일 입력 3회 일관성 | 26/30 (86.67%) | Wilson 70.32–94.69% | 개선 필요 |
| 종단 지연 | p50 42.07초 / p95 55.52초 | Median Bootstrap 39.60–45.47초 | 가장 큰 운영 병목 |

따라서 발표에서 “자연어→AST 정확도 100%”라고 말하면 안 된다. 현재 방어 가능한
표현은 **“30건 held-out pilot에서 AST exact 79.37%, strict E2E 82.22%, 안전한
거절 100%; 독립 100건 최종 평가는 남아 있다”**이다.

## 평가 대상과 동결 정보

- 코드 기준: `2c3ba9c2eccb7661b0d3ac5fb82275db2b172749`
- 모델: `openai-codex/gpt-5.6-luna`, reasoning `medium`, 최대 12턴
- `apps/api/ceo.py` SHA-256:
  `552ee7620de5232fcc829b30d0db64d07ff206dcc18de42513a1f462ff511990`
- Trading `SOUL.md` SHA-256:
  `ca2fa5c04654284b79ba672f462674a61e4260b2aa15fc359e70e9d7e01b7444`
- 기존 PAPER MCP SHA-256:
  `e4b9d3660302b5a940cfb96bf114d6b16ced1844945d35ebf113351ee09da56d`
- 평가셋 SHA-256:
  `2a8ff48a91b6a68369b93fa705315559e7862f04bc28ee365d055765a3ca65f9`
- 구성: 지원 21건, 모호 입력 5건, 미지원 입력 4건. 부정 케이스 30%.
- 기존 CEO 프롬프트·Trading SOUL·Stress 테스트 파일과 정규화한 완전 동일 문장:
  0건.

평가셋은 실행 전에 해시로 동결했고 각 문장을 새 Hermes 세션에서 3회 실행했다.
다만 평가셋 작성자가 프롬프트 계약을 읽은 뒤 골드 AST도 작성했으므로, 외부 독립
평가자에 의한 완전 비공개 테스트셋은 아니다. 이 평가셋은 이번 실행 이후
회귀셋으로만 사용하고 최종 정확도에는 재사용하지 않는다.

## 실제 실행 경로

각 케이스는 다음 경로를 그대로 통과했다.

1. 임시 Hermes Kanban DB에 CEO root와 Trading primary 카드를 생성한다.
2. 모델에는 운영과 동일한 `Work on kanban task <id>`만 전달한다.
3. Trading Hermes가 실제 카드 본문과 현재 Trading SOUL을 읽는다.
4. 기존 `process_user_conditional_paper_rule` MCP를 호출한다.
5. 기존 Pydantic 스키마, 의미 validator, authority/idempotency/PAPER gate를 통과한다.
6. 외부 상태 소유자만 메모리 repository·가짜 종목 resolver로 대체한다.

새 파서, 새 실행기, 새 주문 경로는 만들지 않았다. 운영 DB, 공유 Kanban, Trading
API, PAPER broker, LIVE broker에는 쓰지 않았다. LIVE 주문은 0건이다. 평가 후
Compose 컨테이너 60/60이 running 상태였고 unhealthy/restarting/exited는 0개였다.

## 1회차와 반복 결과

| 회차 | AST exact | Strict E2E | MCP exactly-once | 지원 경계 승인 | p50 | p95 |
|---|---:|---:|---:|---:|---:|---:|
| 1회차 | 17/21 (80.95%) | 25/30 (83.33%) | 30/30 | 20/21 | 41.79초 | 55.19초 |
| 2회차 | 17/21 (80.95%) | 25/30 (83.33%) | 29/30 | 19/21 | 42.06초 | 60.67초 |
| 3회차 | 16/21 (76.19%) | 24/30 (80.00%) | 30/30 | 19/21 | 42.07초 | 51.51초 |

회차별 총점은 비슷하지만 같은 케이스의 결과가 항상 같지는 않았다. 따라서 평균
정확도만 제시하면 구조화 출력 불안정성이 숨는다.

## 확인된 실패

### 세 회차에서 반복된 결함

| 케이스 | 기대 | 실제 | 영향 |
|---|---|---|---|
| H02 | “아래”를 `LT` | `LTE` | 임계값과 정확히 같을 때 잘못 Trigger |
| H06 | 30분봉의 15봉 거래량 평균 | `15M` 기본 거래량 평균으로 해석 | 평가 주기·기간 모두 변경 |
| H12 | 보유 수익률 `PNL_PERCENT` | 존재하지 않는 `POSITION_RETURN` | deterministic boundary에서 안전 거절 |
| H30 | `UNSUPPORTED_INDICATOR` | `CONDITION_EXPRESSION_CLARIFICATION_REQUIRED` | 거절은 안전하지만 사용자 안내 사유가 부정확 |

### 회차별로 달라진 결함

| 케이스 | 발생 회차 | 증상 |
|---|---:|---|
| H10 | 3회차 | “넘으면”을 `GT` 대신 `GTE`로 변환 |
| H13 | 2회차 | Trailing 후보의 tool-call JSON이 깨져 MCP 도달 실패 |
| H15 | 3회차 | 지원되는 순차 조건을 일반 표현 확인 필요로 거절 |
| H20 | 1회차 | “넘으면”을 `GT` 대신 `GTE`로 변환; 2·3회차는 정답 |

H12와 H13은 fail-closed라 잘못된 주문 활성화는 없었다. 반면 H02, H06, H10,
H20은 스키마상 유효한 다른 AST가 ACTIVE가 될 수 있으므로 정확도 측면에서 우선
수정 대상이다.

## 의미 동등성과 조건 감시

1회차의 상태 없는 지원 케이스 18건에 대해 골드와 예측 AST를 각각 기존 evaluator에
넣어 후보쌍당 64개 결정론 프레임을 Replay했다. 14/18만 같은 시점에 같은 행동을
냈다. 실패는 H02, H06, H12, H20으로 AST exact 실패와 일치했다.

Trailing 2건과 temporal sequence 1건은 1회차에 골드 AST와 동일했다. 별도 기존
Worker·validator·evaluator·상태전이 회귀는 다음과 같이 통과했다.

```text
282 passed in 1.18s
```

이 282개 회귀 통과를 Trigger precision/recall로 바꾸어 쓰면 안 된다. 독립적으로
라벨링한 positive/negative 시세 Replay 세트가 없기 때문이다. 현재 근거는
“canonical AST 이후 구현 회귀 통과”이며, 조건 감시의 독립 precision·recall은
아직 미측정이다.

## CEO 라우팅과 사용자 평가

CEO 라우팅 회귀는 현재 `STABLE_CASES` 21건과 `UNDECIDED_CASES` 3건이 통과하고,
known defect 3건이 xfail이다. 보수적으로 24/27, 88.89%라고 쓸 수 있지만 저장소
내부에서 만든 회귀셋이므로 독립 대표성 근거는 아니다.

```text
26 passed, 3 xfailed, 58 subtests passed
```

실제 사용자 페르소나 평가는 참가자가 없어 수행하지 않았다. 두 명의 독립 AST
라벨러 합의도 수행하지 않았다. 이를 임의 점수로 채우지 않는다.

## 통계 해석

- 비율 지표: Wilson 95% CI.
- F1·중앙 지연: seed `20260901`, 10,000회 Bootstrap 95% CI.
- 단일 시스템 평가이므로 p-value를 붙이지 않았다.
- 비교할 사전 동결 baseline prompt가 없어 McNemar test를 수행하지 않았다.
- 21개 지원 문장은 표본이 작아 CI가 넓다. 최종 주장을 위해서는 외부 2인 라벨링한
  비공개 100건이 필요하다.

## 다음 우선순위

1. `넘으면/초과/이상`, `아래/이하/미만`을 골든 comparator 표로 고정한다.
2. `N봉 평균`과 `N분봉`을 분리하고, 명시된 primary timeframe을 보존한다.
3. 포트폴리오 필드 카탈로그에서 `PNL_PERCENT`를 Hermes가 그대로 선택하게 한다.
4. 알려진 미지원 지표는 일반 표현 오류가 아니라 `UNSUPPORTED_INDICATOR`로 분류한다.
5. malformed tool JSON을 재현하는 구조화 출력 회귀를 추가한다. unknown outcome에는
   자동 재호출하지 않는다.
6. 같은 30건은 수정 후 개발 회귀로만 사용한다. 최종 수치는 새 비공개 100건,
   2인 라벨 합의, paired baseline으로 다시 측정한다.
7. 정확도 수정 후 지연을 최적화한다. 4턴 제한 예비 실행은 복합 케이스의 MCP 호출을
   누락시켰으므로 현재 기본값 12턴을 바로 4턴으로 낮추면 안 된다.

## 재현 산출물

민감한 운영 상태와 미래 프롬프트 누수를 피하기 위해 평가셋·원시 세션은 Git 밖에
보관했다.

- `/home/ubuntu/hgfinance-eval-20260901/holdout_v1.json`
- `/home/ubuntu/hgfinance-eval-20260901/results_faithful_pass1.json`
- `/home/ubuntu/hgfinance-eval-20260901/results_faithful_pass2_3.json`
- `/home/ubuntu/hgfinance-eval-20260901/score_faithful_all3.json`
- `/home/ubuntu/hgfinance-eval-20260901/semantic_faithful_pass1.json`
- `/home/ubuntu/hgfinance-eval-20260901/run_holdout.py`
- `/home/ubuntu/hgfinance-eval-20260901/score_holdout.py`
- `/home/ubuntu/hgfinance-eval-20260901/semantic_replay.py`

원시 세션 export는 Hermes의 `--redact` 옵션으로 저장했고 입력은 합성 문장뿐이다.
