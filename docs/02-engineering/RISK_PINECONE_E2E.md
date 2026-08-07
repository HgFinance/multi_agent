# Risk Pinecone E2E

Risk의 mandate assessment는 두 직원 경계를 유지한다.

```text
Risk input
  -> risk-runner
  -> evaluate_order_compliance (order context가 있는 경우)
  -> RiskEngine-style deterministic checks
  -> compliance-policy-worker
  -> query_pinecone_risk_policy
  -> risk-head fan-in
  -> risk-assessment.v1
```

## 직원 권한

- `risk-runner`: 결정론적 한도·VaR·자산정책 검사. `authoritative: true`다.
- `compliance-policy-worker`: Risk 정책 근거 조회와 설명. `authoritative: false`이며 주문을 제출하지 않는다.
- Risk Head: 결과를 종합하지만 `binding: false`다.

## Pinecone 경계

Risk client는 `risk-compliance-policy` namespace만 조회한다. 요청 payload의 namespace를 받지 않는다.
필수 환경변수는 `PINECONE_API_KEY`, `PINECONE_INDEX_HOST`이며, 런타임 upsert와 index 생성은 하지 않는다.

검색 결과는 `chunk_id`, `document_id`, `version`, `effective_from`을 필수 metadata로 요구한다.
`effective_from <= as_of <= effective_to` 조건을 만족하지 않는 chunk는 버린다.

## 실패 동작

- Pinecone credential/timeout/응답 shape 오류: `UNAVAILABLE` 또는 `DEGRADED`
- embedding 또는 유효 근거 부족: `INCONCLUSIVE`/`ESCALATE`
- RiskEngine 주문 한도 초과: `RESIZE` 또는 `REJECT`
- 실패 시 `APPROVE` fallback은 없다.

## 검증

```bash
source ~/claude/bin/activate
ruff check departments/03-risk
python -m pytest departments/03-risk/tests -q -rs
python -m pytest tests/api/test_risk_domain_mandate_api.py tests/api/test_risk_mandate_bff.py -q -rs
```

실제 Pinecone smoke test는 유효한 index host와 embedding vector가 준비된 경우에만 별도로 실행한다.
국가법령정보센터 API 인증은 별도 `LawApiClient` 경로에서 `LawSearch` JSON 응답으로 검증한다.
