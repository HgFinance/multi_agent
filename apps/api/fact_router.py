#!/usr/bin/env python3
"""사실 조회 직행 라우터. **에이전트 앞에 선다.**

소유: 재일 (사용자 입구 배선분)
근거: CLAUDE.md 개발원칙 "LLM 은 관련성 판단·서술에만 쓴다 - PIT 필터·인용 검증·
      한도 검사는 결정론적 Python", 마스터플랜 5.6(권한 분리)

▶ 왜 (2026-08-11 실측)
  "내 계좌 잔고" 한 마디에 CEO 라우팅(90~180초) + 부서 5곳(각 3~6분) + 종합을
  태웠다. LLM 7번, 4분, 결과는 5/5 "산출 불가". **같은 입력에 같은 답이 나오는
  질문**을 에이전트에게 물어볼 이유가 없다. 여기서 0.5초에 답한다.

▶ 분류는 결정론이다
  질의 분류에 LLM 을 쓰면 그것부터 다시 몇십 초다. 단어 대조로 판정하고,
  **애매하면 사실이 아니라고 본다**(에이전트 경로로 넘긴다). 반대로 잘못
  직행시키면 해석이 필요한 질문에 숫자만 던지게 되는데, 그쪽이 더 나쁘다.

▶ 조회 실패를 답으로 위장하지 않는다
  사실 질의로 판정했는데 조회가 실패하면 **실패를 그대로 돌려준다.** 에이전트
  경로로 떠넘기지 않는다 - 그쪽도 같은 데이터를 못 보므로 4분 뒤에 똑같이
  실패한다(오늘 실측이 그것이다). 무엇이 없어서 못 했는지를 즉시 말하는 편이
  사용자에게 낫다.

▶ 숫자는 여기서 나가고, 서술은 에이전트가 한다
  로컬 모델이 500,000,000 을 "500만 원" 으로 옮겨 쓴 것을 실측했다. 그래서
  **화면에 나가는 수치는 이 경로의 값이고**, 에이전트 문장은 그 옆의 참고다.

자체 점검:
    python apps/api/fact_router.py
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
_LS_PATH = ROOT / "departments" / "03-risk" / "integrations"
if str(_LS_PATH) not in sys.path:
    sys.path.insert(0, str(_LS_PATH))


@dataclass(frozen=True)
class Fact:
    """직행으로 답한 사실 한 조각."""

    kind: str
    title: str
    data: dict[str, Any]

    def to_public(self) -> dict[str, Any]:
        return {"kind": self.kind, "title": self.title, **self.data}


class FactUnavailable(RuntimeError):
    """사실 질의로 판정했지만 조회가 안 된다. **빈 값으로 바꾸지 않는다.**"""


# ── 분류 ────────────────────────────────────────────────────────────────
# 단어는 **질문의 대상**만 본다. "어때", "괜찮아" 같은 판단 요청어가 있으면
# 아래 `_ADVICE_TERMS` 가 직행을 취소한다 - 해석이 필요한 질문이기 때문이다.
_FACT_TERMS: dict[str, tuple[str, ...]] = {
    "broker_account": ("잔고", "예수금", "계좌", "평가금액", "주문가능", "보유종목", "보유 종목"),
    "market_quote": ("현재가", "주가", "시세", "호가", "종가"),
}

# 이 말이 섞이면 사실 조회가 아니다 - 사용자는 숫자가 아니라 판단을 원한다.
# 여기 걸리면 에이전트 경로로 간다.
_ADVICE_TERMS: tuple[str, ...] = (
    "어때", "어떨까", "괜찮", "될까", "할까", "좋을까", "추천", "의견", "평가해",
    "분석", "전망", "봐줘", "봐 줘", "판단", "조언", "상담", "유지해", "팔까", "살까",
)

_SYMBOL = re.compile(
    r"(?<![0-9A-Za-z])([0-9A-Za-z]{6})(?![0-9A-Za-z])"
)


def classify(query: str) -> str | None:
    """직행할 사실 종류. 아니면 None(= 에이전트 경로)."""
    text = " ".join(str(query).split())
    if not text:
        return None
    if any(term in text for term in _ADVICE_TERMS):
        # 애매하면 사실이 아니라고 본다. 해석 요청에 숫자만 던지는 쪽이 더 나쁘다.
        return None
    for kind, terms in _FACT_TERMS.items():
        if any(term in text for term in terms):
            return kind
    return None


# ── 조회 ────────────────────────────────────────────────────────────────
def _fetch_broker_account(_query: str) -> Fact:
    try:
        from account_snapshot import broker_account_snapshot
    except ImportError:  # pragma: no cover - package import path
        from apps.api.account_snapshot import broker_account_snapshot
    try:
        snap = broker_account_snapshot()
    except Exception as exc:  # noqa: BLE001 - HTTPException 포함
        raise FactUnavailable(str(getattr(exc, "detail", exc))[:300]) from exc
    return Fact(kind="broker_account", title="브로커 계좌 현황", data=snap)


def _fetch_market_quote(query: str) -> Fact:
    match = _SYMBOL.search(query)
    if not match:
        # 종목명 → 코드 사상은 여기 없다. 지어내지 않고 못 한다고 말한다.
        raise FactUnavailable(
            "종목 코드(6자리)를 찾지 못했습니다. 종목명으로는 아직 조회하지 않습니다."
        )
    symbol = match.group(1).upper()
    try:
        import ls_openapi  # type: ignore[import-not-found]

        with ls_openapi.LSOpenAPIClient.from_env() as client:
            quote = client.get_quote(symbol)
    except Exception as exc:  # noqa: BLE001
        raise FactUnavailable(f"{type(exc).__name__}: {exc}"[:300]) from exc
    return Fact(
        kind="market_quote",
        title=f"{symbol} 시세",
        data={
            "symbol": quote.symbol,
            # 금액은 문자열이다. JSON number 는 double 이라 Decimal 이 깨진다.
            # 필드 이름은 `MarketQuote` 를 그대로 따른다 - `getattr(..., 기본값)` 로
            # 감싸면 이름이 틀렸을 때 조용히 빈 값이 나간다(실측: "(값 없음)").
            "price": str(quote.price),
            "bid": None if quote.bid is None else str(quote.bid),
            "ask": None if quote.ask is None else str(quote.ask),
            "observed_at": quote.observed_at.isoformat(),
            "source": quote.source,
            "authoritative": False,
        },
    )


FETCHERS: dict[str, Callable[[str], Fact]] = {
    "broker_account": _fetch_broker_account,
    "market_quote": _fetch_market_quote,
}


def answer(query: str) -> dict[str, Any] | None:
    """직행으로 답할 수 있으면 답을, 아니면 None(= 에이전트 경로).

    **`None` 과 "조회 실패" 는 다르다.** None 은 "이건 사실 질의가 아니다" 이고,
    조회 실패는 `unavailable: true` 로 돌려준다 - 후자를 None 으로 바꾸면
    에이전트가 4분 걸려 똑같이 실패한다.
    """
    kind = classify(query)
    if kind is None:
        return None
    try:
        fact = FETCHERS[kind](query)
    except FactUnavailable as exc:
        return {
            "route": "direct",
            "kind": kind,
            "unavailable": True,
            "reason": str(exc),
            "authoritative": False,
        }
    return {
        "route": "direct",
        "kind": kind,
        "unavailable": False,
        "fact": fact.to_public(),
        # 이 값은 조회 결과 그대로다. 요약·해석이 끼지 않았다는 뜻이다.
        "authoritative": False,
        "source_of_record": "/ui/snapshot",
    }


# ── 에이전트 경로에 붙여 보낼 사실 ──────────────────────────────────────
# 질문에 **판단이 섞여 있어도** 그 판단에 필요한 수치는 결정론으로 뽑을 수 있다.
# "이 비중 유지해도 될까" 는 직행 대상이 아니지만, 잔고·현재가는 사실이다.
_CONTEXT_TERMS: dict[str, tuple[str, ...]] = {
    "broker_account": (
        "계좌", "잔고", "포트폴리오", "보유", "비중", "예수금", "자산", "수익률", "평가",
    ),
    "market_quote": ("주가", "시세", "현재가", "종가", "가격"),
}


def gather(query: str) -> list[dict[str, Any]]:
    """질문에 관련된 **확인된 수치**를 모은다. 판단은 하지 않는다.

    ▶ 왜 (2026-08-11)
      부서 워커의 도구는 payload 를 되돌려주는 어댑터라 스스로 조회하지 못한다.
      그래서 **입구에서 붙여 보내지 않으면 부서는 영원히 숫자를 못 본다** -
      실제로 5개 본부가 전부 "작업 컨텍스트에 없어"라고 답했다.
      정량은 여기서 확정하고, 정성(해석·판단)만 에이전트에게 맡긴다.

    ▶ 실패는 실패로 싣는다. 빠뜨리면 부서가 "자료가 없다"와 "조회가 실패했다"를
      구분하지 못하고, 둘 다 침묵으로 끝난다.
    """
    text = " ".join(str(query).split())
    if not text:
        return []
    out: list[dict[str, Any]] = []
    for kind, terms in _CONTEXT_TERMS.items():
        if not any(term in text for term in terms):
            continue
        if kind == "market_quote" and not _SYMBOL.search(text):
            continue  # 종목 코드가 없으면 무엇의 시세인지 모른다
        try:
            out.append(FETCHERS[kind](text).to_public())
        except FactUnavailable as exc:
            out.append({"kind": kind, "unavailable": True, "reason": str(exc)})
    return out


def as_prompt_block(facts: list[dict[str, Any]]) -> str:
    """부서 카드·CEO 프롬프트에 실을 사실 블록. **요약하지 않는다.**"""
    if not facts:
        return ""
    lines = ["[확인된 사실 - 결정론 조회값, 다시 계산하거나 바꿔 쓰지 마십시오]"]
    for fact in facts:
        if fact.get("unavailable"):
            lines.append(f"- {fact['kind']}: 조회 실패 — {fact['reason']}")
            continue
        title = fact.get("title", fact.get("kind"))
        body = ", ".join(
            f"{k}={v}"
            for k, v in fact.items()
            if k not in {"kind", "title", "authoritative", "schema_version",
                         "official_nav_source"}
            and v is not None
        )
        lines.append(f"- {title}: {body}")
    lines.append(
        "위 수치는 브로커·시세 조회값이며 공식 원장 확정치가 아닙니다. "
        "숫자를 옮겨 적을 때 단위를 바꾸지 말고, 없는 값은 없다고 쓰십시오."
    )
    return "\n".join(lines)


__all__ = [
    "answer", "classify", "gather", "as_prompt_block",
    "Fact", "FactUnavailable", "FETCHERS",
]


if __name__ == "__main__":  # 자체 점검 - pytest 미도입(CLAUDE.md)
    # 사실 질의는 잡는다
    assert classify("내 계좌 잔고 알려줘") == "broker_account"
    assert classify("예수금 얼마야") == "broker_account"
    assert classify("005930 현재가") == "market_quote"

    # 판단을 원하는 질문은 직행하지 않는다 - 같은 단어가 들어 있어도
    assert classify("계좌 상황 어때?") is None
    assert classify("삼성전자 주가 어떨까?") is None
    assert classify("이 비중 유지해도 될까") is None
    assert classify("보유 종목 평가해줘") is None

    # 사실도 판단도 아닌 것
    assert classify("") is None
    assert classify("안녕하세요") is None
    assert classify("전략 하나 추천해줘") is None

    # 조회 실패는 None 이 아니라 unavailable 이다
    def _boom(_q: str) -> Fact:
        raise FactUnavailable("자격 없음")

    saved = FETCHERS["broker_account"]
    try:
        FETCHERS["broker_account"] = _boom
        got = answer("잔고 알려줘")
        assert got is not None and got["unavailable"] is True, got
        assert "자격 없음" in got["reason"], got
    finally:
        FETCHERS["broker_account"] = saved

    # 종목 코드가 없으면 지어내지 않는다
    try:
        _fetch_market_quote("주가 알려줘")
    except FactUnavailable as exc:
        assert "6자리" in str(exc), exc
    else:
        raise AssertionError("코드가 없는데 조회했다")

    print("fact_router 자체 점검 통과")
