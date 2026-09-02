"""Expand one "top-N by market cap" request into an explicit basket sentence.

``user_order_language`` deliberately refuses to infer a symbol: its basket
grammar reads exact catalog mentions and nothing else, so a theme or a ranking
can never become an order by way of the parser guessing. That boundary is the
reason "시가총액 상위 10종목 300만원씩 매수" was rejected outright rather than
resolved, and it is worth keeping.

So this module does the resolution one layer up, the way
``compound_paper_orders`` does: it recognises the ranking phrase, takes the
ranked rows the authenticated BFF already fetched, and writes the ordinary
explicit-list sentence those rows stand for. The result flows through the same
basket grammar, admission, and PAPER gates as a hand-typed list - nothing here
submits an order or resolves a catalog entry of its own.

The ranking is read once, at admission. A conditional rule that re-picks its
universe when it fires is a different feature and stays unsupported: the
membership a user approved would otherwise not be the membership that trades.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass


# PLACE_BASKET carries at most 20 legs, and a one-member "basket" is just an
# ordinary order, so a request outside that range is not expanded at all.
MIN_UNIVERSE_MEMBERS = 2
MAX_UNIVERSE_MEMBERS = 20

_SYMBOL_RE = re.compile(r"^[0-9A-Z]{6}$")
_NAME_RE = re.compile(r"^[가-힣A-Za-z][가-힣A-Za-z0-9&+._\- ]{0,72}$")

# "시가총액 상위" / "시총 상위" / "KRX 시가총액 기준 상위". This order lane
# is backed by the KRX ranking snapshot, so an omitted venue is canonically KRX;
# it is not a request for a global or US ranking. The ranking word must still be
# present: a bare "상위 10종목" names no metric and stays ambiguous.
_UNIVERSE_RE = re.compile(
    r"(?:(?:krx|한국\s*거래소)\s*)?"
    r"(?:시가\s*총액|시총)\s*(?:기준\s*)?(?:상위|top)(?![가-힣A-Za-z0-9])",
    re.IGNORECASE,
)
# A named foreign/global venue is not an omitted venue. Refuse it instead of
# finding the shorter ``시가총액 상위`` substring and silently rebinding it to
# KRX. This remains a scope guard around the same parser, not a second parser.
_NON_KRX_UNIVERSE_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9])"
    r"(?:미국|해외|글로벌|세계|일본|중국|홍콩|유럽|"
    r"nasdaq|nyse|amex|나스닥|뉴욕\s*증권\s*거래소|홍콩\s*거래소)"
    r"(?:\s*(?:주식|증권))?\s*(?:시장(?:의|에서)?\s*)?"
    r"(?=(?:시가\s*총액|시총))",
    re.IGNORECASE,
)
_TOP_N_RE = re.compile(
    r"(?<![0-9])(?P<n>[1-9][0-9]?)\s*(?:개\s*)?종목"
    r"(?:을|를|은|는)?(?![가-힣A-Za-z0-9])"
)
# Same shape as the basket grammar's "N만원씩" so an expanded sentence lands on
# the allocation form that already exists downstream.
_NOTIONAL_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9,.])(?P<token>[1-9][0-9,]{0,11})\s*만\s*원\s*씩"
    r"(?![가-힣A-Za-z0-9])"
)
# The verb carries Korean endings ("매수해줘", "매도해주세요"), so no right-hand
# boundary is asserted here. This only decides whether the sentence is a
# candidate for expansion; the basket grammar re-reads the rewritten sentence
# and is still the authority on side and shape.
_BUY_RE = re.compile(r"(?<![가-힣A-Za-z0-9])(?:매수|매입|사줘|사고)")
_SELL_RE = re.compile(r"(?<![가-힣A-Za-z0-9])(?:매도|매각|팔아|팔고)")
# An explicit share count or a limit price means the user described something
# other than an equal-KRW allocation; those sentences are left alone. "10개
# 종목" is the member count, not a share count, so it is not read as one.
_QUANTITY_RE = re.compile(
    r"(?<![가-힣A-Za-z0-9,.])[0-9][0-9,]*\s*(?:주식|주|개)(?![가-힣A-Za-z0-9])"
    r"(?!\s*종목)"
)
_LIMIT_RE = re.compile(r"(?<![가-힣A-Za-z0-9])지정가(?![가-힣A-Za-z0-9])")

RANKING_KIND = "market_cap"
MARKET_SCOPE = "KRX"


@dataclass(frozen=True)
class DynamicUniversePlan:
    """One recognised ranking request, before any symbol is known."""

    ranking_kind: str
    top_n: int
    notional_krw: int
    market_scope: str = MARKET_SCOPE


def parse_dynamic_universe_order(raw_text: str) -> DynamicUniversePlan | None:
    """Recognise "시가총액 상위 N종목 M만원씩 매수", or return None."""

    text = " ".join(str(raw_text or "").split())
    if (
        not text
        or _NON_KRX_UNIVERSE_RE.search(text) is not None
        or _UNIVERSE_RE.search(text) is None
    ):
        return None
    if _SELL_RE.search(text) is not None or _LIMIT_RE.search(text) is not None:
        return None
    if len(_BUY_RE.findall(text)) != 1:
        return None
    if _QUANTITY_RE.search(text) is not None:
        return None

    top_n_matches = _TOP_N_RE.findall(text)
    notional_matches = _NOTIONAL_RE.findall(text)
    # Two counts or two allocations mean two readings; neither is chosen here.
    if len(top_n_matches) != 1 or len(notional_matches) != 1:
        return None

    top_n = int(top_n_matches[0])
    if not MIN_UNIVERSE_MEMBERS <= top_n <= MAX_UNIVERSE_MEMBERS:
        return None
    try:
        notional_krw = int(notional_matches[0].replace(",", "")) * 10_000
    except ValueError:
        return None
    if notional_krw <= 0:
        return None
    return DynamicUniversePlan(
        ranking_kind=RANKING_KIND,
        top_n=top_n,
        notional_krw=notional_krw,
        market_scope=MARKET_SCOPE,
    )


def universe_members(
    plan: DynamicUniversePlan, rows: Sequence[Mapping[str, object]]
) -> tuple[tuple[str, str], ...]:
    """Take the first ``top_n`` usable ``(symbol, name)`` pairs, or nothing.

    Fail closed: a short or malformed ranking yields no members rather than a
    smaller basket. Buying eight names when ten were asked for is a different
    order, and the user never approved it.
    """

    members: list[tuple[str, str]] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            return ()
        symbol = str(row.get("symbol") or "").strip().upper()
        name = str(row.get("name") or "").strip()
        if _SYMBOL_RE.fullmatch(symbol) is None or _NAME_RE.fullmatch(name) is None:
            continue
        if symbol in seen:
            continue
        seen.add(symbol)
        members.append((symbol, name))
        if len(members) == plan.top_n:
            return tuple(members)
    return ()


def expand_to_basket_instruction(
    plan: DynamicUniversePlan, rows: Sequence[Mapping[str, object]]
) -> str | None:
    """Write the explicit sentence the ranking stands for, or return None.

    The code leads each member because the basket grammar treats a six-digit
    KRX code as stronger evidence than an adjacent display name, exactly as it
    does for a hand-typed list.
    """

    members = universe_members(plan, rows)
    if not members:
        return None
    listed = ", ".join(f"{symbol} {name}" for symbol, name in members)
    return f"{listed} {plan.notional_krw // 10_000}만원씩 시장가 매수"


__all__ = [
    "MAX_UNIVERSE_MEMBERS",
    "MARKET_SCOPE",
    "MIN_UNIVERSE_MEMBERS",
    "RANKING_KIND",
    "DynamicUniversePlan",
    "expand_to_basket_instruction",
    "parse_dynamic_universe_order",
    "universe_members",
]
