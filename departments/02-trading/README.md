# 트레이딩본부 (Trading)

전 본부 Backend·Event·Docker 연결 기준은 [Department Backend Integration and Docker Plan](../../docs/02-engineering/DEPARTMENT_BACKEND_INTEGRATION_DOCKER_PLAN.md)을 따른다.
LLM 직원 런타임은 독립 LangGraph Worker와 Ollama `qwen3:1.7b`이며, 결정론 `desk-runner`는 모델을 호출하지 않는다. Hermes Profile은 `trading-department`다. `Modelfile`은 로컬 보조 실행용이고, Build·Eval·권한 기준은 [Ollama Department Modelfile Guide](../../docs/02-engineering/OLLAMA_DEPARTMENT_MODELFILE_GUIDE.md)를 따른다.
현재 실행 상태와 도현님 2주 계획·Daily Scrum은 [실행 현황과 통합 계획 v2.2](../../docs/PROJECT_IMPLEMENTATION_STATUS.md#42-도현님-트레이딩본부-회계포트폴리오본부와-공통-platform)을 따른다.

## Mission

Trade Case, Signal, OrderIntent 생성과 집행을 담당한다. Research Packet을 기반으로 Bull/Bear 토론 후
Trader/PM Agent가 진입·청산·크기·무효화 조건을 갖춘 구조화된 거래 제안(OrderIntent)을 만든다.

`trader-pm-agent`는 주문을 직접 전송하지 않는다. Risk/Compliance Gate 통과가 선행 조건이다
(`CLAUDE.md` "절대 깨면 안 되는 권한 분리" 참고). Agent Decision ≠ Strategy Signal ≠ OrderIntent ≠ Order.

## Current Worker 구성

구조조정 이후 Trading은 3명으로 실행된다.

| Worker | 방식 | 역할 |
|---|---|---|
| `bull-thesis-worker` | LLM | Research Packet 근거 기반 Bull thesis 작성 |
| `bear-thesis-worker` | LLM | Research Packet 근거 기반 Bear thesis 작성. Bull 결과와 독립 |
| `desk-runner` | 결정론 | Intent Builder, 계약 전이, 실행 가능성·비용·파생 Certification 처리 |

`desk-runner`는 기존 `trade-proposal-worker`, `order-constraint-worker`, `execution-planning-worker`,
`venue-cost-worker`, `derivatives-structure-worker`의 결정론 업무를 흡수한다. Risk 승인·Broker Submit은
여전히 Trading Worker의 권한이 아니다.

## Owner

도현님 — [TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE](../../docs/05-teams/TEAM_DOHYUN_TRADING_ACCOUNTING_GUIDE.md)

## 입력·출력 계약

- 입력: 리서치본부 Research Packet
- 출력: OrderIntent → `workflow` step 3 리스크본부로 전달. Risk 승인 후 `oms/`가 상태를 관리하고
  `broker/`가 모의 체결한다.

## 실행법

```bash
trading-department chat -q 'Propose a trade for [종목]'
python departments/02-trading/contracts/contracts.py
python departments/02-trading/oms/oms.py
python departments/02-trading/broker/paper_broker.py
python departments/02-trading/multileg/intent_group.py
python departments/02-trading/capability/derivatives.py
python departments/02-trading/execution/broker_rules.py
python departments/02-trading/execution/tca_memory.py
python departments/02-trading/employee_workers.py
python departments/02-trading/scripts.py
python departments/02-trading/api/app.py

uvicorn app:app --app-dir departments/02-trading/api      # Domain API 실행
```

## 테스트

- `contracts/contracts.py` — 계약 8개 영역 자체 점검
- `oms/oms.py` — OMS 불변식 13개 자체 점검
- `broker/paper_broker.py` — Paper Broker 8개 영역 자체 점검
- `multileg/intent_group.py` — F30 Multi-leg 13개 영역
- `capability/derivatives.py` — F31 Derivatives Capability 14개 영역
- `execution/broker_rules.py` — 거래소·브로커 규칙 RAG 8개 영역 (TR 365개 색인)
- `execution/tca_memory.py` — TCA 집행 기억 8개 영역 (`DATABASE_URL` 있으면 실 DB 읽기까지)
- `employee_workers.py` — 직원 규칙 근거 주입·인용 검증 4개 영역
- `scripts.py` — Bull/Bear 토론 파이프라인 18개 영역 (Hermes·네트워크 없음)
- `api/app.py` — Domain API 15개 영역 (TestClient. 네트워크·DB 없음)

## Handoff

- `hermes/` — Git 기준 Hermes Profile 사본
- `contracts/` — OrderIntent / RiskDecision / EventEnvelope 계약(Sprint D0), `philosophies.yaml`(철학별 집행 프리셋)
- `oms/` — 결정론적 OMS(Sprint D1). 구 경로 `execution/oms.py`는 2026-07-30에 삭제됐다
- `broker/` — Paper Broker(Sprint D1). 구 경로 `execution/paper_broker.py`는 2026-07-30에 삭제됐다
- `multileg/intent_group.py` — F30. Pair·Basket·Hedge·Roll처럼 여러 Leg가 하나의 의도인 주문.
  핵심은 주문 생성이 아니라 **부분 체결 복구 판정**이다 — ALL_OR_NONE에서 일부만 체결되면
  COMPLETED가 아니라 `PARTIAL_RECOVERY`이고, 복구 수단이 없으면 `FAILED_SAFE`로 떨어져
  신규 진입이 막힌다. 판정만 하고 실제 취소·청산은 OMS가 한다
- `capability/derivatives.py` — F31. **파생상품 거래를 켜는 코드가 아니다.** 팀 가이드 1.1대로
  계약 모델과 접수 차단 게이트를 만든다. Broker·Risk·Accounting 3개 Certification이 전부
  서명돼야 파생·공매도 주문이 통과하고, 그 전에는 Risk 승인이 있어도 접수 단계에서 막힌다.
  명목금액은 반드시 승수를 곱한다 — 빼먹으면 한도가 조용히 느슨해진다
- `execution/` — D4 집행 계층 (2026-08-05 신설).
  - `broker_rules.py` — 거래소·브로커 규칙 RAG. `docs/06-integrations/ls-openapi/` 문서를
    **그대로 읽어** TR 규칙을 색인한다(숫자를 코드에 복사해두지 않는다). 두 가지를 한다:
    검색 결과만 인용하게 하고 색인 밖 `rule_id`를 날조로 잡는 것, 그리고 **초당 한도를 넘는
    분할 설계를 결정론적으로 거부**하는 것(정정·취소 3/s, 계좌 조회 1~2/s). 임베딩을 쓰지
    않는 이유는 파일 상단 주석에 있다 — 규칙 검색에서 근사 이웃은 개선이 아니라 결함이다
  - `tca_memory.py` — TCA 환류. 과거 유사 집행의 실현 슬리피지로 `philosophies.yaml`
    참여율·분할 **조정안만** 만든다. 예산 초과는 예산을 올리지 않고 집행을 조이는 방향으로
    제안하며(개발 원칙 9), Paper 근거와 실집행 근거를 절대 섞지 않는다. 한도 반영은
    리스크본부, 전략 승격은 퀀트본부 권한이라 이 모듈에는 YAML 쓰기 경로가 없다
- `employee_workers.py` — 직원 3명. **LLM 은 Bull/Bear 둘뿐**이고 나머지는 결정론
  `desk-runner` 하나로 합쳐졌다(2026-08-06 tool 강등). 러너가 위 규칙 근거를 받아
  판정을 그대로 옮긴다 — 판정은 러너가 서술하기 전에 이미 끝나 있다
- `scripts.py` — Bull/Bear 독립 병렬 **2라운드** 토론. 두 토론자는 직원 런타임
  (LangGraph + Ollama)이고 사회는 부서장 Hermes(`trading-supervisor`)가 본다.
  토론자는 라운드마다 답하기 전에 혼자 사고하며(`config.debate.max_thinking_passes`),
  그 사고는 인용 근거가 아니고 상대에게 넘어가지 않는다. grounded 토론에 한해
  **OrderIntent 제안**까지 만든다(2026-08-05). 제안에는 `risk_decision_id`가 없고
  `submittable: False`라 OMS가 제출을 거부한다 — Risk Gate 선행이 문서가 아니라
  코드로 강제된다
- `api/` — Domain API(FastAPI). 위 모듈을 감싸기만 하고 **새 주문 판정 로직이 없다.**
  Hermes는 이 API/MCP 경계로만 부른다(같은 프로세스에 import하지 않는다).
  설계서: [TRADING_DOMAIN_API_SPEC.md](../../docs/02-engineering/TRADING_DOMAIN_API_SPEC.md)
- 팀 가이드 v1.2 상태 머신 2단 분리는 **반영 완료**(2026-08-03). 남은 것은 저장소다 —
  OMS 상태가 아직 프로세스 메모리이고 `execution.*` 연결은 미결이다(설계서 4절)
