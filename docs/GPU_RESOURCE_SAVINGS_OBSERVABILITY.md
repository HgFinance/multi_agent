# GPU 자원 절감 상시 측정

## 적용 상태

2026-08-30 기준 AWS L4 단일 vLLM 경로에 다음을 연결했다.

- DCGM exporter: GPU 사용률, VRAM, 전력, 누적 에너지
- vLLM native `/metrics`: 대기열, 실행 중 요청, KV cache, preemption, 토큰, TTFT/E2E latency
- Prometheus: 15초 scrape/evaluation 및 workload-normalized recording rule
- Grafana: `HgFinance GPU Resource Savings` 대시보드

기존 DCGM exporter를 추가로 띄우지 않고, 기존 vLLM scheduler/조건주문/Risk 경로도 변경하지 않는다.

## 무엇을 절감으로 보는가

GPU 사용률이 낮다는 사실만으로 절감이라고 판정하지 않는다. 유휴 GPU는 사용률이 낮지만 결과도 만들지 않기 때문이다.
대시보드의 핵심 효율 지표는 다음이다.

| 지표 | 의미 |
| --- | --- |
| `hgfinance:vllm:tokens_per_busy_gpu_second` | 실제 GPU가 바쁜 시간 1초당 처리 토큰 |
| `hgfinance:vllm:busy_gpu_seconds_per_1k_tokens` | 토큰 1,000개를 처리하는 데 사용한 busy GPU 시간 |
| `hgfinance:vllm:energy_mj_per_1k_tokens` | 토큰 1,000개당 DCGM 에너지 |
| `hgfinance:vllm:memory_peak_savings_vs_7d_ratio` | 같은 시각 7일 전 peak VRAM 대비 감소율 |
| `hgfinance:vllm:energy_savings_vs_7d_ratio` | 같은 시각 7일 전 토큰당 에너지 대비 감소율 |
| `hgfinance:vllm:busy_gpu_seconds_savings_vs_7d_ratio` | 같은 시각 7일 전 토큰당 busy GPU 시간 대비 감소율 |

`*_vs_7d`에서 양수는 감소, 음수는 회귀다. vLLM 토큰/에너지 지표는 이번 연결 이후 7일 데이터가 쌓이기 전까지 값이 비어 있는 것이 정상이다. 비어 있는 값을 0% 절감으로 표시하지 않는다.

VRAM/전력은 기존 DCGM 원시 시계열을 기준선으로 사용하므로, 현재 Prometheus 보존 기간에 7일 전 데이터가 있으면 즉시 비교할 수 있다. 이는 동일 workload라는 보장이 없는 운영 비교값이며, 릴리스 승인용 인과적 절감 증거로 과장하지 않는다.

## 안전 기준

- `vllm:num_requests_waiting`, KV cache, preemption, TTFT p95, E2E p95를 절감 지표와 함께 본다.
- queue/preemption/latency가 악화되면 효율 향상으로 승인하지 않는다.
- P0 조건주문 실행과 Risk guard는 계속 GPU 외 결정론 경로다.
- vLLM priority/preemption 정책은 이 계측 변경에서 활성화하지 않았다.

## 확인 방법

```bash
curl -s http://127.0.0.1:9090/api/v1/targets
curl -s 'http://127.0.0.1:9090/api/v1/query?query=hgfinance%3Avllm%3Aenergy_mj_per_1k_tokens'
curl -s 'http://127.0.0.1:9090/api/v1/query?query=hgfinance%3Avllm%3Aenergy_savings_vs_7d_ratio'
```

현재 대시보드는 Prometheus의 `HgFinance` 폴더에 provision되며 15초마다 갱신된다. 절감률을 보기 전에는 처리 트래픽이 있어야 하며, 7일 기준선이 없는 지표는 `No data`가 올바른 상태다.
