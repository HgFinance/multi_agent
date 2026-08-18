"""Small, dependency-free helpers for market-universe files."""
from __future__ import annotations


def parse_symbol_file(text: str) -> tuple[str, ...]:
    """Parse one symbol per line, ignoring blanks and ``#`` comments."""
    symbols: list[str] = []
    for line in text.splitlines():
        symbol = line.split("#", 1)[0].strip()
        if symbol:
            symbols.append(symbol)
    return tuple(symbols)
