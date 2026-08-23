# AWQ+Hybrid A/B comparison — deterministic structured fallback removed

Runtime profile: `L4-fp8KV-v1` on NVIDIA L4. Frozen Internal-50 v2 and External-50 v1 datasets and frozen scorers were used for every stage.

FinanceBench has two separate values below:

- `diagnostic mean`: automatic partial-overlap diagnostic from the frozen scorer.
- `auto proxy`: diagnostic score `>= 0.5`, shown as an engineering signal only. It is not the official FinanceBench manual accuracy.

| Metric | Hybrid baseline | EXPR + guided + AST | Unit/scale normalization | Finance glossary + unit schema | FIFO few-shot |
|---|---:|---:|---:|---:|---:|
| Internal Quality | 82.0% (41/50) | 88.0% (44/50) | 88.0% (44/50) | 84.0% (42/50) | 86.0% (43/50) |
| Critical Failures | 1 | 1 | 1 | 1 | 1 |
| Request Errors | 3 | 4 | 3 | 6 | 6 |
| Financial Arithmetic | 60.0% (6/10) | 70.0% (7/10) | 80.0% (8/10) | 80.0% (8/10) | 80.0% (8/10) |
| Risk Reasoning | 100.0% (7/7) | 100.0% (7/7) | 100.0% (7/7) | 100.0% (7/7) | 100.0% (7/7) |
| Portfolio / Trading | 83.3% (5/6) | 100.0% (6/6) | 100.0% (6/6) | 83.3% (5/6) | 83.3% (5/6) |
| Accounting | 83.3% (5/6) | 100.0% (6/6) | 83.3% (5/6) | 83.3% (5/6) | 100.0% (6/6) |
| Quant | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | 83.3% (5/6) | 83.3% (5/6) |
| Evidence | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) | 100.0% (6/6) |
| Structured Output | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) | 40.0% (2/5) |
| Uncertainty / Fail-Closed | 100.0% (4/4) | 100.0% (4/4) | 100.0% (4/4) | 100.0% (4/4) | 100.0% (4/4) |
| FinQA | 80.0% (16/20) | 70.0% (14/20) | 85.0% (17/20) | 70.0% (14/20) | 70.0% (14/20) |
| TAT-QA | 93.3% (14/15) | 93.3% (14/15) | 93.3% (14/15) | 93.3% (14/15) | 93.3% (14/15) |
| FinanceBench diagnostic mean | 56.8% | 50.7% | 50.4% | 56.8% | 56.8% |
| FinanceBench auto proxy (diagnostic >= 0.5) | 7/15 | 7/15 | 7/15 | 7/15 | 7/15 |
| Auto Mean (FinQA + TAT-QA) | 0.8563 | 0.7984 | **0.8841** | 0.7984 | 0.7984 |
| Verdict | BASELINE | REJECT: External regression | CANDIDATE; manual FinanceBench pending | REJECT: Internal/External regression | REJECT: no recovery |

## Decision

Unit/scale normalization is the only candidate that improves the primary automated External metric while preserving Critical Failures and Request Errors. It is not promoted yet because its FinanceBench diagnostic mean falls from 56.8% to 50.4%; the official `x/15` value requires the same manual rubric for every variant.

Historical FP8 `7/15` and AWQ `8/15` FinanceBench manual values are reference values from a different historical run and are not copied into the new Hybrid column.
