"""Compatibility import for the shared research/quant intraday AST contract."""

from pathlib import Path
import sys

_CONTRACT_CANDIDATES = (
    Path("/app/repo/departments/01-research/contracts"),
    Path(__file__).resolve().parents[2] / "01-research" / "contracts",
)
_CONTRACTS = next((p for p in _CONTRACT_CANDIDATES if p.is_dir()), _CONTRACT_CANDIDATES[0])
if str(_CONTRACTS) not in sys.path:
    sys.path.insert(0, str(_CONTRACTS))

from intraday_ast_contract import *  # noqa: F401,F403,E402
