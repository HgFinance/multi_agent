#!/usr/bin/env python3
"""universe-manager - 리서치본부 결정론 직원 1호 (LLM 불필요).

담당: 재일 (리서치/퀀트)
근거: departments/01-research/hermes/config.yaml 의 universe-manager 페르소나
      ("halted, restricted, data-anomalous or illiquid symbols 제외") -
      prompt-only 였던 것을 실행 가능한 구현으로 고도화 (재일님 지시 2026-07-31).

▶ 완전한 결정론이다. LLM 이 없고, 같은 입력이면 같은 Universe 가 나온다.
  판정 근거는 LS 공식 목록(실측 2026-07-31, path /stock/market-data):
    t1404 jongchk 1/2/3 = 관리종목 / 불성실공시 / 투자유의
    t1405 jongchk 1/2/3 = 투자경고 / 매매정지 / 정리매매
  페이지당 100건, cts_shcode 연속조회.

▶ 출력은 Agent Decision 이 아니라 **Universe 판정**이다. 매매 신호가 아니고,
  트레이딩·리스크는 이 판정을 참조만 한다(구속은 Risk Engine 몫).

사용
  python agents/universe_manager.py            # 자체 점검 (호출 없음)
  python agents/universe_manager.py --run      # 바스켓에 실제 판정
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "collectors"))

PERSONA = "universe-manager"   # 부서 허용목록 키
AGENT_VERSION = "research-universe-manager-v1"
KST = timezone(timedelta(hours=9))
PATH = "/stock/market-data"
RATE = 1.0

# (tr, jongchk) -> 제외 사유. 순서가 심각도다 - 한 종목이 여러 목록에 있으면
# 앞선 사유 하나로 보고한다(전부 '제외'라는 결론은 같다).
RESTRICTION_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("t1405", "2", "HALTED"),          # 매매정지
    ("t1405", "3", "LIQUIDATION"),     # 정리매매
    ("t1404", "1", "ADMINISTERED"),    # 관리종목
    ("t1405", "1", "CAUTION_ALERT"),   # 투자경고
    ("t1404", "3", "INVESTMENT_RISK"), # 투자유의
    ("t1404", "2", "DISCLOSURE_FAULT"),# 불성실공시
)


@dataclass(frozen=True)
class UniverseDecision:
    as_of: datetime
    tradable: tuple[str, ...]
    excluded: dict            # symbol -> 사유
    list_sizes: dict          # 사유 -> 전체 목록 크기 (바스켓 밖 포함)

    def summary(self) -> str:
        return (f"{self.as_of.astimezone(KST):%m-%d %H:%M} 기준 "
                f"거래가능 {len(self.tradable)} / 제외 {len(self.excluded)}"
                + (f" ({', '.join(f'{s}:{r}' for s, r in sorted(self.excluded.items()))})"
                   if self.excluded else ""))


def fetch_restricted(client, tr: str, jongchk: str, *, max_pages: int = 30) -> set[str]:
    """제한 목록 하나를 연속조회로 끝까지 모은다. 실패는 예외 - 빈 목록으로
    위장하면 정지 종목이 거래가능으로 새는 방향이라 fail-closed 가 필수다."""
    out: set[str] = set()
    cts, tr_cont, key = "", "N", ""
    for _ in range(max_pages):
        resp, hdrs = client.call_tr(
            path=PATH, tr_cd=tr,
            in_block={f"{tr}InBlock": {"gubun": "0", "jongchk": jongchk,
                                        "cts_shcode": cts}},
            rate_limit_per_sec=RATE, tr_cont=tr_cont, tr_cont_key=key,
            return_headers=True,
        )
        rows = resp.get(f"{tr}OutBlock1") or []
        before = len(out)
        for r in rows:
            code = str(r.get("shcode") or "").strip()
            if code:
                out.add(code)
        cts = str((resp.get(f"{tr}OutBlock") or {}).get("cts_shcode") or "").strip()
        more = str(hdrs.get("tr_cont", "")).upper() == "Y" and cts
        if not rows or len(out) == before or not more:
            break
        tr_cont, key = "Y", str(hdrs.get("tr_cont_key", ""))
    return out


def decide(basket: tuple[str, ...], restricted: dict[str, set[str]],
           *, as_of: datetime) -> UniverseDecision:
    """결정론 판정. restricted 는 {사유: 종목코드 집합} - 심각도 순으로 첫 사유."""
    excluded: dict[str, str] = {}
    for _tr, _j, reason in RESTRICTION_SOURCES:
        for sym in restricted.get(reason, ()):  # 결정론 - 집합이어도 사유 순서가 우선
            if sym in basket and sym not in excluded:
                excluded[sym] = reason
    tradable = tuple(s for s in basket if s not in excluded)
    return UniverseDecision(
        as_of=as_of, tradable=tradable, excluded=excluded,
        list_sizes={r: len(v) for r, v in restricted.items()},
    )


def restrictions_from_api(payload: dict) -> dict[str, set[str]]:
    """research-api /universe/restrictions 응답 -> {사유: 종목집합}.

    사유 목록은 **우리가 아는 것만** 받는다. 모르는 사유가 오면 무시하지 않고
    드러낸다 - 목록 정의가 바뀌었는데 조용히 통과하면 정지 종목이 샌다.
    """
    known = {r for _tr, _j, r in RESTRICTION_SOURCES}
    out: dict[str, set[str]] = {r: set() for r in known}
    unknown: set[str] = set()
    for row in payload.get("restrictions") or []:
        reason = str(row.get("reason") or "").strip()
        sym = str(row.get("symbol") or "").strip()
        if not sym:
            continue
        if reason in known:
            out[reason].add(sym)
        else:
            unknown.add(reason)
    if unknown:
        raise RuntimeError(
            f"모르는 제한 사유 {sorted(unknown)} - RESTRICTION_SOURCES 와 "
            f"수집기 정의가 어긋났다(판정을 멈춘다)")
    return out


def run(client=None, basket: tuple[str, ...] = (),
        research_api: str | None = None, get=None) -> UniverseDecision:
    """거래가능 판정.

    ▶ 2026-08-02: LS REST 직접 호출을 **research-api 조회로 바꿨다.**
      판정하는 쪽이 LS 자격을 들고 있으면 파이프라인을 돌리는 컨테이너마다
      자격이 퍼진다(통합계획 6.2 위반). 이제 수집기
      (universe_restriction_collector)만 자격을 갖고, 여기는 DB 를 읽는다.
      client 인자는 자체 점검·수동 호출 호환으로 남기되, 주면 옛 경로를 쓴다.
    """
    import json as _json
    import os
    import urllib.request

    from news_watch_service import parse_watchlist_file

    if not basket:
        wl = Path(__file__).resolve().parent.parent / "config" / "news_watchlist.txt"
        basket = parse_watchlist_file(wl.read_text(encoding="utf-8"))

    if client is not None:      # 옛 경로 - LS 직접(자체 점검·긴급 수동용)
        restricted = {reason: fetch_restricted(client, tr, jong)
                      for tr, jong, reason in RESTRICTION_SOURCES}
        return decide(basket, restricted, as_of=datetime.now(timezone.utc))

    base = (research_api or os.environ.get("RESEARCH_API_URL",
                                           "http://127.0.0.1:8035")).rstrip("/")

    def _default_get(url):
        # 페르소나를 밝힌다(Tool Gateway 이행, 2026-08-02). 강제 모드에서
        # 헤더 없이 부르면 403 이고, 실제로 그렇게 걸렸다.
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "evidence"))
        from api_client import get_json

        return get_json(url, persona=PERSONA, timeout=25)

    # 조회 실패는 예외다 - 빈 목록으로 위장하면 정지 종목이 거래가능으로 샌다
    payload = (get or _default_get)(f"{base}/universe/restrictions")
    return decide(basket, restrictions_from_api(payload),
                  as_of=datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# 자체 점검 - 호출 없이
# ---------------------------------------------------------------------------

def _check_decide():
    ts = datetime(2026, 7, 31, 7, 0, tzinfo=timezone.utc)
    d = decide(
        ("005930", "000660", "999990", "999991"),
        {"HALTED": {"999990", "888888"}, "ADMINISTERED": {"999991", "999990"}},
        as_of=ts,
    )
    assert d.tradable == ("005930", "000660")
    assert d.excluded == {"999990": "HALTED", "999991": "ADMINISTERED"}, \
        "심각도 순서(정지 > 관리)가 깨졌다"
    assert d.list_sizes["HALTED"] == 2  # 바스켓 밖 종목도 크기에는 보인다
    assert "999990:HALTED" in d.summary()
    print("  판정/심각도 순서         OK")


def _check_fail_closed():
    class _Boom:
        def call_tr(self, **kw):
            raise RuntimeError("LS down")

    try:
        {r: fetch_restricted(_Boom(), t, j) for t, j, r in RESTRICTION_SOURCES[:1]}
        raise AssertionError("목록 조회 실패가 빈 목록으로 위장됐다")
    except RuntimeError:
        pass
    print("  fail-closed              OK")


def _check_pagination():
    pages = [
        {"t1404OutBlock": {"cts_shcode": "AAA"},
         "t1404OutBlock1": [{"shcode": f"{i:06d}"} for i in range(100)]},
        {"t1404OutBlock": {"cts_shcode": ""},
         "t1404OutBlock1": [{"shcode": f"{i:06d}"} for i in range(100, 130)]},
    ]
    n = {"i": 0}

    class _C:
        def call_tr(self, **kw):
            i = min(n["i"], 1)
            n["i"] += 1
            return pages[i], {"tr_cont": "Y" if i == 0 else "N", "tr_cont_key": "k"}

    got = fetch_restricted(_C(), "t1404", "1")
    assert len(got) == 130 and n["i"] == 2, (len(got), n["i"])
    print("  연속조회                 OK")



def _check_api_restrictions():
    """API 응답 -> 판정. 모르는 사유는 조용히 무시하지 않는다."""
    payload = {"restrictions": [
        {"symbol": "000660", "reason": "HALTED"},
        {"symbol": "005930", "reason": "ADMINISTERED"},
        {"symbol": "000660", "reason": "ADMINISTERED"},   # 중복 사유 - 둘 다 받는다
        {"symbol": "", "reason": "HALTED"},                # 빈 종목 - 버린다
    ]}
    got = restrictions_from_api(payload)
    assert got["HALTED"] == {"000660"} and got["ADMINISTERED"] == {"000660", "005930"}
    assert got["LIQUIDATION"] == set(), "없는 사유도 키는 있어야 한다"

    d = decide(("000660", "005930", "035720"), got, as_of=datetime.now(timezone.utc))
    assert d.tradable == ("035720",), d.tradable
    assert d.excluded["000660"] == "HALTED", "심각도 순서(정지가 관리보다 앞)"

    # 모르는 사유가 오면 판정을 멈춘다 - 조용히 통과하면 정지 종목이 샌다
    try:
        restrictions_from_api({"restrictions": [{"symbol": "000660",
                                                 "reason": "NEW_RULE"}]})
        raise AssertionError("모르는 사유가 통과했다")
    except RuntimeError:
        pass

    # run() 이 API 경로를 쓰는지 - 자격 없이 판정되는 것이 이 변경의 목적이다
    called = {}
    def fake_get(url):
        called["url"] = url
        return payload
    d2 = run(basket=("000660", "035720"), research_api="http://x", get=fake_get)
    assert "/universe/restrictions" in called["url"], called
    assert d2.tradable == ("035720",)
    print("  API 경유 판정            OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--run" in sys.argv:
        print(f"{AGENT_VERSION} 실제 판정")
        d = run()
        print(f"  {d.summary()}")
        print(f"  전체 목록 크기: {d.list_sizes}")
        raise SystemExit(0)

    print(f"{AGENT_VERSION} 자체 점검 (호출 없음)")
    _check_decide()
    _check_fail_closed()
    _check_pagination()
    _check_api_restrictions()
    print("직원 3개 영역 통과. 실제 판정은 --run")
