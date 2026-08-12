"""Run optional Research API/Redis/Supabase Risk -> QA integration probes."""

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from departments.risk_qa_testkit.integration import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
