# Workflow Boundary

이 디렉터리는 부서 내부 로직을 구현하지 않고, 부서 사이의 순서·handoff 계약·실패 방향만 관리한다.

## Canonical flows

- `investment-case.yaml`: `research → trading → risk → QA → OMS/Fill → accounting → CEO`
- `strategy-research.yaml`: `quant-backtest → QA → CEO`
- `workforce-management.yaml`: `HR 설계 → HR 평가 → QA 권한 검증 → CEO 승인 → HR lifecycle`
- `agent-evolution.yaml`: `HR 개선 후보 → HR 개정 → QA 검증 → CEO 승인 → HR Shadow/Rollback`
- `event-routing.yaml`: 이벤트별 allow-list 라우팅. 순차 Workflow가 아니며 결정론적 이벤트는 `ENTRY_BLOCKED`로 처리한다.

## 실행 규칙

```bash
source ~/claude/bin/activate
python -m orchestration.workflows.runner --workflow investment-case --mode dry-run --json
python -m orchestration.workflows.runner --workflow investment-case --mode paper-e2e --symbol AAPL --quantity 100 --limit-price 200.00 --json
python -m unittest discover -s tests/orchestration -p 'test_*.py' -v
```

`dry-run`의 `VALIDATED`는 부서 adapter를 호출했다는 뜻이 아니다. 실제 adapter를 명시적으로 주입하지 않은 `live` 실행은
`BLOCKED`와 해당 step의 안전 행동으로 끝난다. 따라서 이 계층은 도메인 성공을 위조하지 않는다.

`paper-e2e`는 각 부서 Hermes Profile에 비변경 smoke prompt를 보내고 handoff 계약을 순서대로 통과시키는 연결 검증이다.
Paper 주문·브로커 제출·Ledger/DB/Notion 쓰기는 수행하지 않는다. 실제 운영 E2E는 별도의 승인된 production adapter가 필요하다.

## 변경 경계

- Risk/QA/Trading/Accounting의 `scripts.py`와 도메인 엔진은 이 디렉터리에서 수정하지 않는다.
- Risk 승인 전에는 OMS/Fill로 넘어갈 수 없다.
- QA 실패는 `ESCALATE`, Risk 실패는 `REJECT`, 체결·원장 반영 실패는 `HOLD`/`BREAK` 방향이다.
- Workflow 계약 변경은 이 디렉터리의 YAML, `multi-agent-workflow.yaml` registry, 계약 테스트를 함께 검토한다.
