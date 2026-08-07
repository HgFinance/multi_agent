# Risk mandate worker flow

사용자 투자 설정은 `RiskMandateAssessmentRequest`로 정규화한 뒤 Risk 부서장(`risk-head`)이 직원 fan-out 입력으로 만든다.

```text
사용자 폼
  -> POST /risk/v1/mandates/{mandate_id}/assess
  -> risk-head: mandate 검증·input_hash 생성·동일 입력 봉인
      -> risk-runner: 결정론적 한도/노출/VaR 검사
      -> compliance-policy-worker: 정책 근거·Pinecone evidence 검사
  -> risk-head: 두 보고서 fan-in 및 advisory 권고
```

두 직원은 같은 `input_hash`를 받아 독립적으로 실행한다. `risk-runner`의
`authoritative: true`는 수치 판정의 출처가 결정론적이라는 뜻이며, 두 직원과
부서장 결과 모두 `binding: false`다. 주문 생성·Risk Engine 최종 판정·사람의
수동 승인은 이 경로의 책임이 아니다.

관찰된 포트폴리오 상태가 없으면 VaR·노출·drawdown을 추정하지 않고 `HOLD`로
종료한다. 정책 근거가 없거나 Pinecone 연결이 구성되지 않으면
`compliance-policy-worker`는 `DEGRADED/ESCALATE`를 반환한다.

Pinecone data plane 연결은 다음 환경변수만 사용한다.

- `PINECONE_API_KEY`
- `PINECONE_INDEX_HOST`
- 선택: `PINECONE_NAMESPACE`

키와 기타 비밀값은 저장소 파일·문서·로그에 기록하지 않는다. 현재 API는
Pinecone `query`에 사용할 `policy_query_vector`가 요청에 포함된 경우에만
Pinecone을 조회한다. 임베딩 생성기는 이 계약 밖에 두어, 사용할 embedding
provider를 별도로 승인·설정할 수 있게 한다.
