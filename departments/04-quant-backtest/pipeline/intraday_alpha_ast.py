"""Compatibility import for the shared research/quant intraday AST contract."""

from pathlib import Path
import sys

_CONTRACTS = Path(__file__).resolve().parents[2] / "01-research" / "contracts"
if str(_CONTRACTS) not in sys.path:
    sys.path.insert(0, str(_CONTRACTS))

from intraday_ast_contract import *  # noqa: F401,F403,E402
