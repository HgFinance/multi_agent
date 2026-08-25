# AWQ Arithmetic LoRA — Adapter-only Replication

이 결과는 `Qwen2.5-14B-Instruct-AWQ`에
`hgfinance-awq-arithmetic-2epoch` LoRA adapter만 적용한 품질 측정이다. RAG, 외부
reasoning critic, 계산기, 단위 정규화, Guided JSON 및 Hybrid routing은 사용하지 않았다.

| 지표 | Adapter-only 실측 |
|---|---:|
| Internal Quality | 74.0% (37/50) |
| Critical Failures | 2 |
| Request Errors | 0 |
| Financial Arithmetic | 30.0% (3/10) |
| Structured Output | 40.0% (2/5) |
| FinQA | 75.0% (15/20) |
| TAT-QA | 80.0% (12/15) |
| FinanceBench diagnostic | 39.82% |
| Auto Mean | 0.7612 |

FinanceBench는 `manual_required`인 diagnostic 지표이며 공식 External Overall이 아니다.
기존에 전달된 39.2%와 이번 재현값 39.82%는 실행 출처를 섞지 않는다.

## 해석

- `74% / 2 / 30% / 40% / 75% / 80% / 0.7612` 조합은 산술 2-epoch
  adapter-only 실행에서 재현됐다.
- 이 결과는 혼합 SFT adapter인 `hgfinance-awq-finetune`의 76% 결과와 별개다.
- Hybrid Upgrade의 90%는 산술 adapter에 추가 파이프라인을 결합한 결과이므로 이 표로
  대체하지 않는다.
- 현재 endpoint가 `max_model_len=4096`으로 보고되어 historical 8192 runtime 표를
  덮어쓰지 않는다. 데이터셋 해시, 프롬프트, temperature와 scorer는 기존 frozen 계약을
  유지했다.

세부 원시 응답과 점수는 같은 디렉터리의 `internal50_raw.json`,
`internal50_score.json`, `external50_raw.json`, `external50_score.json` 및
`provenance.json`에 기록한다.
