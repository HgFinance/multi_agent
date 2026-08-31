# Hermes Supervisor Bounded ReAct A/B 테스트

> 실행 시각(UTC): 2026-08-31T02:24:13.300296+00:00
> Benchmark: `hermes-supervisor-react-ab-v1` · 모델 기준: `gpt-5.6-luna` · 반복: `3` · 동시 실행: `4`

## 결론

이 문서는 현재 Hermes Supervisor Prompt와 Bounded ReAct Prompt를 동일한 합성 Observation Packet에 실행한 파일럿 A/B 결과입니다. ReAct 변형은 세 Supervisor에 공통으로 bounded state/action/observation/stop 규칙을 추가했습니다.

실제 Tool 호출과 외부 상태 변경은 차단했습니다. 따라서 아래 결과는 주문·Kanban 위임·실제 검색 성능이 아니라, **감독자 프롬프트가 근거 부족·충돌·에스컬레이션·라우팅·종료를 얼마나 정확하게 선택하는지**를 측정한 결과입니다.

## 요약 지표

| Supervisor | Variant | Case pass | Decision | Evidence handling | Safety | Mean latency(ms) | Mean tokens |
|---|---:|---:|---:|---:|---:|---:|---:|
| Research HQ | baseline | 0.00% | 0.00% | 16.67% | 100.00% | 19653.01 | 15063.56 |
| Research HQ | react | 0.00% | 0.00% | 16.67% | 100.00% | 19585.46 | 15427.61 |
| QA/Audit | baseline | 0.00% | 0.00% | 0.00% | 55.56% | 11611.69 | 11826.83 |
| QA/Audit | react | 0.00% | 0.00% | 5.56% | 66.67% | 10825.78 | 12196.06 |
| CEO | baseline | 0.00% | 0.00% | 33.33% | 100.00% | 8842.07 | 15229.72 |
| CEO | react | 0.00% | 0.00% | 33.33% | 100.00% | 8560.16 | 15591.61 |

## ReAct 효과 Delta (ReAct - Baseline)

| Supervisor | Case pass | Decision | Evidence handling | Safety | Mean latency | Mean tokens |
|---|---:|---:|---:|---:|---:|---:|
| Research HQ | 0.00%p | 0.00%p | 0.00%p | 0.00%p | -67.55 ms | 364.06 |
| QA/Audit | 0.00%p | 0.00%p | 5.56%p | 11.11%p | -785.91 ms | 369.22 |
| CEO | 0.00%p | 0.00%p | 0.00%p | 0.00%p | -281.92 ms | 361.89 |

## 측정 설계

- Baseline과 ReAct는 같은 모델, 같은 프로필 Persona, 같은 Case, 같은 반복 횟수로 실행했습니다.
- 차이는 ReAct군의 `SOUL.md`에 `bounded-react-supervisor-v1` 정책을 추가한 것뿐입니다.
- 프로필별로 근거 충분·부족·PIT stale·충돌·권한 경계·결정론 판정·신선 상태·병렬 위임 Case를 포함했습니다.
- 각 실행은 새 임시 Hermes Home에서 시작해 이전 대화·메모리·세션 오염을 막았습니다.
- `-t ''`로 Toolset을 비워 실제 검색·Kanban·승인·쓰기 경로를 사용하지 않았습니다.

## 지표 정의

- **Case pass**: JSON 형식, 기대 상태, 다음 행동, 대상 부서, evidence 처리, 안전 조건을 모두 만족한 비율입니다.
- **Decision**: `status`, `next_action`, `targets`가 Case 정답과 일치한 비율입니다.
- **Evidence handling**: 요구된 evidence reference, 충돌 플래그, missing evidence를 정확히 처리한 비율입니다.
- **Safety**: `unsafe_action_attempted=false`를 지킨 비율입니다. 결정론 판정 보존 Case에서는 별도 fidelity도 확인했습니다.
- **Latency**: Hermes 프로세스의 wall-clock 시간입니다. Provider API 지연과 초기화 비용을 포함합니다.
- **Tokens/API calls**: Hermes가 생성한 usage report 기준입니다. Tool 호출은 의도적으로 0으로 제한했으므로 Tool 효율 지표는 이번 실험에서 산출하지 않습니다.

## 해석 주의사항

1. 이번 결과만으로 실제 Research 검색 품질이나 CEO의 실제 부서 생성 품질을 확정할 수 없습니다. 실제 read-only Tool 결과를 연결한 2차 Shadow Test가 필요합니다.
2. ReAct가 품질을 개선하더라도 지연·토큰 증가가 도입 기준을 넘으면 적용하지 않습니다.
3. QA의 결정론 PASS/WARN/FAIL, Risk/OMS, Ledger, NAV 권한은 ReAct 평가 대상이 아니며 계속 코드와 독립 통제 계층이 소유합니다.
4. 내부 추론 전문을 평가하거나 저장하지 않고, 구조화된 최종 JSON과 usage/latency만 평가했습니다.

## 원자료

- 상세 실행 행: `artifacts/hermes-react-ab-20260831/results.jsonl`
- 집계 JSON: `artifacts/hermes-react-ab-20260831/summary.json`
- 실행기: `scripts/benchmark_hermes_react_ab.py`
