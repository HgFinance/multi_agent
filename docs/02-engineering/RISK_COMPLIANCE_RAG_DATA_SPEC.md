# Risk compliance-policy-worker 데이터 적재 기준

## 결론

원본 PDF를 바로 임베딩하지 않는다. 원본을 불변 보관하고, 텍스트·표·각주를
추출한 뒤 조항 단위의 계층형 chunk를 만들어 Pinecone에 적재한다. 현재 Risk
코드는 Pinecone query를 위한 `policy_query_vector`를 받을 수 있지만, PDF
추출·임베딩·upsert pipeline은 아직 구현 대상이다.

## 반드시 적재할 정책 데이터

| 문서군 | compliance-policy-worker가 찾는 근거 |
|---|---|
| 투자 Mandate | 목표, 위험성향, 자본, 단일 종목·섹터·총 익스포저, drawdown, 허용 시장, 주문 승인 방식 |
| Restricted List | 종목/issuer, 금지·감축·거래정지 상태, 적용 범위, 사유, effective 기간 |
| Concentration/Risk Policy | issuer·sector·gross/net exposure, leverage·derivatives, correlation bucket, breach 조치 |
| 내부 Compliance Policy | 자본시장법 등 외부 규정의 적용 조항, 내부 규정 번호, 금지행위와 예외 |
| Order/Approval Policy | 수동 승인 요건, Risk/Compliance/사용자 승인 매트릭스, kill switch, 신규 진입 차단 규칙 |
| 예외·Escalation Policy | 위반 시 `RESIZE/REJECT/HOLD/ESCALATE` 기준과 승인 주체 |

## Chunk metadata 계약

각 child chunk는 최소한 다음 metadata를 가져야 한다.

```json
{
  "chunk_id": "policy-concentration-001:v1.2.0:sec-3.2:p12:c04",
  "document_id": "policy-concentration-001",
  "document_type": "policy",
  "title": "Concentration Policy",
  "version": "1.2.0",
  "clause_id": "3.2",
  "page_start": 12,
  "page_end": 12,
  "effective_from": "2026-08-01",
  "effective_to": null,
  "jurisdiction": "KR",
  "authority": "internal-risk-policy",
  "source_sha256": "sha256:...",
  "parent_chunk_id": "policy-concentration-001:v1.2.0:sec-3"
}
```

본문에는 하나의 규칙과 필요한 정의가 함께 있어야 한다. 표의 행은 표 제목,
열 이름, 단위, 각주를 붙여 독립 chunk로 만들고, 법령·내부 규정의 조항 번호와
페이지를 보존한다. 현재 로컬 baseline의 child chunk 길이 기준은 약 800자이며,
Pinecone에서도 parent section과 child embedding을 함께 보존한다.

## 검색 시 worker에 전달할 입력

정책 DB에는 현재 포트폴리오의 VaR나 포지션을 넣지 않는다. worker query에는
다음 동적 상태를 별도 구조화 입력으로 넣고, 정책 DB에서는 그 상태를 판정할
규칙만 검색한다.

- `mandate_id`, `as_of`
- instrument/symbol, issuer, sector, asset class
- side, quantity, price, proposed notional
- 현재 single-issuer weight, sector weight, gross/net exposure
- current drawdown, VaR와 limit
- order mode와 요청 질문

즉 `risk-runner`가 수치와 한도를 결정하고, `compliance-policy-worker`는
“어느 문서의 몇 조항이 이 상태를 허용·위반·판단불가로 만드는가”를 근거와
함께 반환한다.

## 반환해야 할 근거

최종 보고서에는 최소 `document_id`, `version`, `clause_id`, `page`,
`effective_from/to`, 인용 본문, source hash, 그리고 `supports` 또는
`contradicts` 관계를 남긴다. 근거가 없거나 effective 기간이 맞지 않으면
`ambiguous/ESCALATE`이며 PASS로 바꾸지 않는다.

## 적재하지 말아야 할 데이터

- API key, broker credential, 사용자 식별 가능 정보
- LLM의 이전 답변만으로 만든 요약문
- 출처·버전·효력 기간이 없는 정책 문장
- 현재 포지션/VaR/NAV 같은 운영 상태(이 값은 canonical DB와 RiskEngine 소유)
- 만료 문서의 삭제본(삭제하지 말고 `effective_to`로 PIT 조회에서 제외)

PDF 원본은 `raw` 보관 영역에 hash와 함께 보존하고, OCR/추출 결과와 chunk
manifest를 재현 가능하게 저장한다. 실제 정책 PDF가 제공되기 전까지 현재
`skills/agentic-rag/corpus/compliance/*.md`는 샘플 placeholder이므로 운영
판정에 사용하지 않는다.
