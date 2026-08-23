# FinanceBench Unit/Scale analysis

## Automatic signal

| Variant | Diagnostic mean | Auto proxy (`score >= 0.5`) |
|---|---:|---:|
| Hybrid baseline | 0.5679 | 7/15 |
| Unit/scale normalization | 0.5039 | 7/15 |

The proxy can be used as a repeatable engineering regression signal, but it must not be reported as official FinanceBench accuracy. FinanceBench remains manual-adjudication data.

## Unit/Scale regressions and improvements

| Case | Expected | Baseline | Unit/Scale | Finding |
|---|---|---|---|---|
| `financebench_id_03473` ROA | `0.01` | explanatory `1.46%` | `1.42493406255` | Unit route changed a semantically valid percentage-form answer into an incorrect scalar/scale result. |
| `financebench_id_05915` fixed asset turnover | `17.98` | `17.98` with explanation | `4.49561018437` | Unit prompt caused formula/denominator loss; clear regression. |
| `financebench_id_00222` quick-ratio health | Yes, ratio 1.57 | `No` | `1.6055895745` | The question is a boolean-with-reason task, not a scalar-only task; both routes use the wrong answer contract. |
| `financebench_id_06247` DPO | `42.69` | truncated | `42.6942090112` | Unit route fixed a baseline truncation and is an improvement. |
| `financebench_id_00394` highest segment | CIB, $3,725m | wrong $3,100m | missing-data claim | Evidence extraction/segment selection issue, not unit normalization. |
| `financebench_id_00206` JPM gross margin | Not relevant for a financial institution | insufficient evidence | insufficient evidence | Needs a general domain glossary definition, not a numeric fallback. |
| `financebench_id_00521` Ulta acquisitions | No acquisitions | insufficient evidence | insufficient evidence | Fail-closed policy is too aggressive when the evidence supports a negative answer. |
| `financebench_id_00606` Ulta wages percentage | Increased | insufficient evidence | insufficient evidence | Fiscal-year mapping and evidence reconciliation are missing. |

## Recommended correction

1. Keep Unit/Scale only for a `numeric_scalar` or explicitly requested numeric formula task.
2. Route boolean, relevance, comparison, list, and evidence-selection questions through the existing AWQ/glossary answer path.
3. Make the numeric contract carry `result_unit`, `scale`, `formula_name`, and `answer_type`; reject a scalar-only answer when the question asks for a conclusion plus a number.
4. Add general glossary entries for financial-institution metric relevance, quick-ratio interpretation, ROA, fixed-asset turnover, and fiscal-year mapping. These must define concepts, not contain benchmark answers.
5. Preserve the baseline answer as a diagnostic alternative, but do not select it using gold answers. Select routes using the validated task-type contract and semantic/unit validation only.
6. Re-run Internal and External after this selective router change. Require: Auto Mean >= 0.8563, no Critical/Request Error increase, and no FinanceBench diagnostic regression before manual adjudication.
