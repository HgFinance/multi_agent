from pathlib import Path
import sys


API_ROOT = Path(__file__).resolve().parents[2] / "apps" / "api"
if str(API_ROOT) not in sys.path:
    sys.path.insert(0, str(API_ROOT))

from apps.api import fact_router, ls_account_stream


def test_bff_fact_symbol_parser_accepts_exact_alphanumeric_code_only() -> None:
    match = fact_router._SYMBOL.search("00088k 현재가")
    assert match is not None
    assert match.group(1).upper() == "00088K"
    assert fact_router._SYMBOL.search("prefix00088ksuffix 현재가") is None


def test_ls_account_symbol_normalizer_supports_exact_code_and_known_prefix() -> None:
    assert ls_account_stream._symbol(" 00088k ") == "00088K"
    assert ls_account_stream._symbol("A00088k") == "00088K"
    assert ls_account_stream._symbol("Samsung Electronics") is None
