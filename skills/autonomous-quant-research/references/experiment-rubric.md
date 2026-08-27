# Experiment Review Rubric

Review evidence in this order. Do not use a single score as a substitute for the checks.

1. **Data integrity:** verify point-in-time fields, publication/availability timestamps, survivorship and selection bias, corporate actions, missingness, duplicate rows and clock alignment.
2. **Identification:** state the mechanism, treatment/control or comparison, expected direction and falsifier. Separate discovery data from validation and out-of-sample data.
3. **Implementation:** record the exact code/artifacts, random seed, feature timing, rebalance timing, sizing, capacity assumptions and all fees/slippage/borrow or market-impact costs that apply.
4. **Robustness:** test delayed execution, alternate reasonable costs, parameter perturbations, time blocks, assets, regimes and a challenge representation. Explain every failed slice.
5. **Failure analysis:** inspect trade paths, concentration, turnover, drawdown clustering, regime dependence and operational assumptions. A failed experiment is retained as knowledge and should influence the next hypothesis.
6. **Decision:** leakage means reject; missing cost or OOS evidence means pause; failed robustness means pivot/challenge; only a completed result satisfying all named gates can become a candidate report.

There are no universal metric thresholds in this rubric. Thresholds must be tied to the objective, costs, capacity and risk mandate, and must be preregistered before tuning.
