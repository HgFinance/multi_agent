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
## 8. Worker 상시 호출과 법률 LLM-Wiki 조건부 검색

Risk Head는 Risk 사건마다 두 직원을 호출하는 것을 기본으로 한다. 다만 이것은
`compliance-policy-worker`가 매번 법률 코퍼스를 검색한다는 뜻이 아니다.

```text
Risk Head 사건 생성
  -> risk-runner: 항상 호출
  -> compliance-policy-worker: 항상 호출
       -> query_mode에 따라 법률 LLM-Wiki 검색 여부 결정
```

`compliance-policy-worker`의 `query_mode`는 Compliance Worker의 자문·검색 범위만
정의한다. Risk Runner의 수치 검증 모드가 아니다.

| query_mode | 의미 | 법률 LLM-Wiki |
|---|---|---:|
| `MANDATE_REVIEW` | 목표·위험 선호·자산 정책의 자연어 의미상 모호성·설명 부족 검토. 필수 필드·숫자 범위·주문 위반은 검사하지 않음 | 호출하지 않음 |
| `RISK_POLICY_REVIEW` | 내부 Risk 정책·Restricted List 근거 검토 | 내부 정책만 |
| `LEGAL_QUERY` | 법령·행정규칙·법령해석례·판례 질의 | 호출 |
| `MIXED_REVIEW` | 내부 정책과 법률 근거를 함께 검토. Risk 수치 판단은 Risk Runner가 독립 수행 | 호출 |
| `NOT_APPLICABLE` | 정책·법률 근거가 현재 질문과 무관 | 호출하지 않음 |

### Mandate 검증의 단일 책임

- `risk-runner / RISK_CHECK`: 필수 필드, 자료형, 숫자 범위, 익스포저·VaR·Drawdown·자산 정책, 현재 주문의 Mandate 위반을 결정론적으로 검사한다. `USER_MANDATE_BREACH`와 실행 게이트의 유일한 authoritative source다.
- `compliance-policy-worker / MANDATE_REVIEW`: 자연어로 입력된 목표·위험 선호·자산 정책의 모호성·설명 부족만 advisory로 기록한다. 수치를 계산하지 않고 Mandate 위반·법률 위반·주문 승인 여부를 판정하지 않는다.

따라서 Compliance 결과에는 `mandate_status`를 두지 않는다. 필요한 경우
`mandate_observations`, `clarification_questions`에 자문 내용을 남기며,
Mandate 위반 여부는 Risk Runner 결과의 `USER_MANDATE_BREACH`만 참조한다.

따라서 직원 호출은 상시 유지해 감사 trace와 두 직원의 독립 결과를 남기되,
법률 검색 tool과 LLM-Wiki 탐색은 조건부로 실행한다. 일반 Mandate 저장을 매번
법률 위반 검색으로 처리하지 않는다.

### Hermes Head routing task

부서장이 `compliance-policy-worker`에 전달하는 최소 routing 필드는 다음과 같다.

```json
{
  "query_mode": "LEGAL_QUERY",
  "law_wiki_required": true,
  "source_targets": ["law", "admrul", "expc", "prec"],
  "execution_mode": "EXPERIMENT",
  "arms": ["A", "B", "C"],
  "as_of": "2026-08-07",
  "required_citations": true
}
```

`order`가 존재하면 `risk-runner`는 항상 `REQUIRED`다. 법령·규정·법적 문제를
질문하거나 `query_mode`가 `LEGAL_QUERY`/`MIXED_REVIEW`이면 법률 LLM-Wiki를
호출한다. 구조화된 routing 값이 자연어 분류보다 우선한다.

### Legal target routing

| target | 데이터 |
|---|---|
| `law` | 현행 법률·시행령 등 현행법령 |
| `admrul` | 금융투자업 감독규정 등 행정규칙 |
| `expc` | 법령해석례 |
| `prec` | 판례 |

조항번호가 정규식으로 확인되고 wiki 페이지에 매칭되면 Arm C는 grep seed를
사용한다. 매칭되지 않으면 BM25 seed로 fallback한다. Arm B는 항상 BM25 seed를
사용한다. 실험 모드에서는 같은 입력을 A/B/C 모두 실행하고, 운영 모드에서는
golden set 결과와 별도 승인으로 선택된 Arm 하나만 실행한다.

법률 LLM-Wiki 결과는 법적 결론 자체가 아니라 검증 대상 후보와 근거를 반환한다.
`LEGAL_BASIS_FOUND`는 조항·citation·effective 기간·적용 범위 검증을 통과한
경우에만 사용한다. `NO_DIRECT_LEGAL_BASIS`는 법적으로 허용된다는 뜻이 아니라
현재 코퍼스에서 직접 적용되는 조항을 확인하지 못했다는 뜻이다.

## 9. 실험과 운영 경계

현재 LLM-Wiki A/B/C 비교는 실험 전용이다.

- `departments/03-risk/experiments/llm_wiki/`만 실험 산출물을 소유한다.
- `skills/agentic-rag`, `hermes/config.yaml`, worker registry에는 자동 배선하지 않는다.
- golden set 평가 전에는 production Arm을 변경하지 않는다.
- 평가 지표는 F1, Exact Match, 페이지당 읽은 문자 수다.
- 실험 코퍼스와 내부 Risk policy Pinecone namespace를 섞지 않는다.
