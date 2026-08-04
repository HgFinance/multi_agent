# Risk·QA TEST / PRODUCTION Pipeline Runbook

검토일: 2026-08-04  
상태: TEST pipeline implemented, PRODUCTION intentionally OFF

## 1. 실행 프로파일

| 프로파일 | 데이터 | LLM | 외부 연결 | 현재 상태 |
|---|---|---|---|---|
| `test` | synthetic ResearchPacket fixture | 결정론적 Qwen-shaped stub | 없음 | 실행 가능 |
| `production` | 실제 Research/API/DB 데이터 | 실제 Worker·Ollama | 승인된 Adapter 필요 | OFF |

`test`는 운영 성공을 의미하지 않는다. 파이프라인 배선, 계약, Worker topology,
fallback, trace/replay를 검증하는 용도다. `production`은 실제 Adapter acceptance가
완료되기 전까지 `OFF/HOLD`로만 반환한다.

## 2. TEST 전체 흐름

```text
ResearchPacket fixture
  → packet contract + input_hash + PIT check
  → Risk deterministic gate skeleton (binding=false)
  → Risk 4 Worker Graphs
  → QA deterministic gate skeleton (binding=false)
  → AI-QA 5 Worker Graphs
  → trace/replay manifest inspection
  → test gate
```

모든 조건부 signal을 fixture에 포함하므로 Risk 4명과 QA 5명이 모두 한 번씩
실행된다. Worker는 `Guard → allow-listed Tool → Qwen-shaped summary → schema`
순서를 지키며, 실패하면 `DEGRADED/ESCALATE`와 `HOLD` 방향으로 종료한다.

## 3. 재일님 Research handoff 반영

TEST fixture는 다음 계약을 고정한다.

- `packet_id`, `artifact_id`, `case_id`, `trace_id`
- `as_known_at`, `input_hash`, `source_refs`
- claim별 `evidence_refs`, `observed_at`
- Risk·QA 입력이 같은 `trace_id`와 Research Packet `input_hash`를 공유

`as_known_at` 이후의 Evidence는 fixture에서도 허용하지 않는다. 실제 Research API가
연결될 때도 이 필드와 `ResearchPacket v1` 계약을 먼저 검증한 뒤 Risk·QA로 넘긴다.

## 4. 실행 명령

저장소 루트에서 실행한다.

```bash
python scripts/run_risk_qa_test_pipeline.py --mode test
python scripts/run_risk_qa_test_pipeline.py --mode production
python -m pytest tests/e2e/test_risk_qa_pipeline_profiles.py -q -p no:warnings
```

기대 결과:

- TEST: `pipeline_status=COMPLETED`, `safe_action=NO_ACTION`, Risk 4명·QA 5명 실행. QA fixture는 의도적인 unsupported claim을 포함하므로 QA skeleton decision은 `WARN`이며 운영 PASS가 아니다.
- PRODUCTION: `pipeline_status=OFF`, `safe_action=HOLD`, Worker 미실행

## 5. Production 전환 조건

다음 조건을 충족하기 전에는 Production flag를 만들거나 실제 Credential을 TEST에
주입하지 않는다.

1. Research API의 실제 `ResearchPacket v1` 조회와 PIT/ACL 검증
2. Risk·QA 내부 API/Redis/Supabase Adapter의 timeout·idempotency·schema 검증
3. 실제 정책 Corpus 교체 및 citation/contradiction golden set 통과
4. `qa-check`, deterministic Risk Engine, Trace/DB persistence의 E2E acceptance
5. Redis/Supabase/Timescale 통합 테스트에서 `skip` 없는 runtime 증거
6. 운영 Credential preflight, Profile/Worker model contract, RLS 권한 검토

Production 전환은 이 문서의 조건을 만족하는 별도 변경으로 진행한다. 현재 TEST
pipeline은 실제 주문·Risk 승인·QA PASS·원장 변경을 수행하지 않는다.

기존 `departments/03-risk/scripts.py --run`와
`departments/06-ai-qa-audit/scripts.py --run`도 현재 기본적으로 같은 Production
guard에 걸린다. `RISK_QA_PRODUCTION_ENABLED=true`가 없으면 실제 데이터나
Credential을 읽지 않고 종료한다.
