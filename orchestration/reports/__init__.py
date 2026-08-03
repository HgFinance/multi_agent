"""Cross-department paper-case reporting helpers.

Reports in this package are projections of workflow contracts. They do not
own orders, risk limits, accounting state, or external database writes.

The implementation is intentionally not imported eagerly so
``python -m orchestration.reports.paper_case`` does not trigger a runpy
module-already-loaded warning.
"""

__all__ = [
    "PaperCaseInput",
    "PaperCaseReport",
    "build_paper_case_report",
    "write_paper_case_report",
]
