"""전략 정찰 (QNT-01) - 웹에서 최신 기법을 찾되 그것을 검증된 것으로 다루지 않는다.

담당: 재일 (퀀트·백테스트본부 QNT)
근거: 재일님 지시 2026-08-04 "최신 퀀트전략 찾으려면 웹검색 필수"

▶ 왜 필요한가
  전략 아이디어는 시장 관측만으로 안 나온다. 논문·저널·업계 자료에서
  "무엇이 왜 먹히는가" 를 가져와야 가설의 경제적 근거가 생긴다.
  QNT-01 프로필도 "why should this edge exist and who is on the other side"
  를 요구하는데, 그 답을 내부 데이터만으로는 쓸 수 없다.

▶ 가져오는 것은 **컨셉이지 전략이 아니다** (재일님 2026-08-04 정정)
  "웹검색으로 전략의 컨셉을 가져와서 알파가 있는 전략으로 개조하라는 의미이지
   그대로 따라하라는 건 아니었음"

  이 구분이 위험의 성격을 바꾼다.

  전략을 **그대로 복제**하면 위험은 공표일이다 - 그때 몰랐던 기법을 아는 셈이라
  전략 누수가 되고, 공표 후 알파 감쇠까지 겹친다.

  컨셉만 빌려 **우리가 구현**하면 그 위험은 옅어지는 대신 **다른 위험이
  커진다**: 파라미터를 고르는 자유도가 전부 우리 것이 된다. 한 컨셉으로 변형
  20개를 돌려 제일 좋은 것을 고르면, 그 성적은 실력이 아니라 **다중검정**이다.
  12번째 시도에서 나온 Sharpe 1.5 는 1번째와 다르다.

  그래서 이 모듈이 남기는 것은 두 가지다:
    · 컨셉과 **경제적 근거** - 왜 이 엣지가 존재하고 반대편에 누가 있나
    · 공표 연도 - **참고 문맥**이다(하드 벽이 아니다). 우리 구현이 원본과
      다르면 그 날짜로 구간을 잘라낼 근거가 약하다. 다만 컨셉이 널리 퍼진
      뒤라면 감쇠를 의심할 재료는 된다.

  **진짜 가드는 시도 횟수**이고 그건 contracts/quant_v2.trial_pressure() 가
  이미 계산한다 - 호출처가 0개였을 뿐이다.

▶ 검증된 것으로 다루지 않는다
  웹에서 온 것은 SEARCH_HIT 이지 사실이 아니다. 정찰 결과는 **가설 후보**로만
  나가고, 기존 경로(feasibility -> PIT dataset -> backtest -> walk-forward)를
  그대로 통과해야 한다. 정찰이 파이프라인을 건너뛰게 하지 않는다.

자체 점검: python departments/04-quant-backtest/agents/strategy_scout.py
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

AGENT_VERSION = "quant-strategy-scout-v1"
SCOUT_PERSONA = "strategy-research-agent"      # QNT-01

# 정찰이 한 번에 낼 수 있는 후보 수. 많이 내면 검증이 못 따라가고,
# 검증 안 된 후보가 쌓이면 그 자체가 소음이다.
MAX_CANDIDATES = 3

# 이 도메인은 원출처로 우선한다. 2차 요약은 조건과 표본을 흘린다.
PREFERRED_HOSTS = (
    "arxiv.org", "ssrn.com", "papers.ssrn.com", "nber.org",
    "jstor.org", "sciencedirect.com", "cfainstitute.org",
    "quantpedia.com", "aqr.com", "robeco.com",
)

# 공표일을 못 찾으면 이 값으로 **가정하지 않는다.** None 이 정답이다.
_YEAR_RE = re.compile(r"(19|20)\d{2}")


@dataclass(frozen=True)
class StrategyLead:
    """웹에서 찾은 전략 단서. **가설이 아니라 후보다.**"""
    title: str
    url: str
    snippet: str
    engine: str
    discovered_at: str                      # ISO8601 - 우리가 언제 알았나
    source_year: Optional[int] = None       # 원출처 공표 연도 (모르면 None)
    is_primary_source: bool = False

    @property
    def host(self) -> str:
        m = re.match(r"https?://([^/]+)", self.url)
        return (m.group(1) if m else "").lower().removeprefix("www.")

    def oos_start(self) -> Optional[date]:
        """컨셉이 공표된 시점. **사양을 복제했을 때만 out-of-sample 경계다.**

        컨셉만 빌려 우리가 구현했다면 이 날짜는 문맥이지 벽이 아니다.
        모르면 None - 오늘로 가정하지 않는다.
        """
        if self.source_year is None:
            return None
        # 논문은 발표 연도 안에 유통되므로 그 해 말일을 경계로 잡는다(보수적)
        return date(self.source_year, 12, 31)


def _year_from(text: str, *, now_year: int) -> Optional[int]:
    """텍스트에서 공표 연도. **미래 연도와 터무니없는 과거는 버린다.**"""
    best = None
    for m in _YEAR_RE.finditer(text or ""):
        y = int(m.group(0))
        if 1970 <= y <= now_year and (best is None or y > best):
            best = y
    return best


def to_leads(hits: Iterable, *, now: Optional[datetime] = None) -> list[StrategyLead]:
    """SearchHit -> StrategyLead. 순수 함수.

    **원출처를 앞세운다** - 2차 요약은 표본 기간·비용 가정을 흘리고, 그걸
    근거로 가설을 쓰면 재현이 안 된다.
    """
    n = now or datetime.now(timezone.utc)
    out: list[StrategyLead] = []
    for h in hits or []:
        url = str(getattr(h, "url", "") or "")
        if not url:
            continue
        title = str(getattr(h, "title", "") or "")
        snip = str(getattr(h, "snippet", "") or "")
        lead = StrategyLead(
            title=title[:200], url=url, snippet=snip[:500],
            engine=str(getattr(h, "engine", "") or "?"),
            discovered_at=n.isoformat(),
            source_year=_year_from(f"{title} {snip} {url}", now_year=n.year),
        )
        primary = any(p in lead.host for p in PREFERRED_HOSTS)
        out.append(StrategyLead(**{**lead.__dict__, "is_primary_source": primary}))
    # 원출처 먼저, 그다음 최신 공표순
    out.sort(key=lambda x: (not x.is_primary_source, -(x.source_year or 0)))
    return out[:MAX_CANDIDATES]


def sample_boundary(leads: Iterable[StrategyLead], *,
                    backtest_start: date,
                    derived: bool = True) -> dict:
    """공표일 문맥. **derived=True(컨셉 차용)면 경고이지 차단이 아니다.**

    컨셉만 빌려 우리가 구현했다면 공표일로 구간을 잘라낼 근거가 약하다 -
    우리 구현은 원본과 다른 물건이다. 다만 컨셉이 퍼진 뒤라면 알파 감쇠를
    의심할 재료는 되므로 사실은 남긴다.

    derived=False(사양을 그대로 복제)면 얘기가 다르다 - 그때는 공표 이전
    구간이 구조적 in-sample 이고, 그 성적을 미래 기대치로 쓰면 안 된다.
    """
    leads = list(leads)
    if not leads:
        return {"has_lead": False}

    years = [ld.source_year for ld in leads if ld.source_year]
    if not years:
        return {
            "has_lead": True, "oos_start": None,
            # ▶ 모르면 **전 구간 in-sample** 이다. 유리한 쪽으로 가정하지 않는다.
            "in_sample_through": None,
            "caution": ("원출처 공표일 미상 - 컨셉 차용이므로 구간을 자르지 "
                        "않되, 시도 횟수(trial_pressure)로 과적합을 본다"
                        if derived else
                        "원출처 공표일 미상 - 사양 복제이므로 전 구간을 "
                        "in-sample 로 다룬다"),
        }

    oos = date(max(years), 12, 31)
    covered = oos <= backtest_start
    return {
        "has_lead": True,
        "oos_start": oos.isoformat(),
        "derived_from_concept": derived,
        # 컨셉 차용이면 구간을 자르지 않는다 - 우리 구현은 원본과 다른 물건이다
        "in_sample_through": None if (covered or derived) else oos.isoformat(),
        "caution": None if covered else
        (f"컨셉이 {oos.year} 에 공표됐다 - 구간을 자르지는 않되(우리 구현은 "
         f"원본과 다르다) 공표 후 알파 감쇠를 의심할 재료다. 과적합의 주된 "
         f"위험은 우리가 고른 파라미터이므로 trial_pressure 로 본다"
         if derived else
         f"백테스트 시작({backtest_start})부터 {oos} 까지는 공표 이전이라 "
         f"구조적 in-sample 이다 - 사양을 복제했다면 그 성적을 미래 기대치로 "
         f"쓰지 않는다"),
    }


def scout(question: str, *, reason: str, run_mode: str = "LIVE",
          search: Optional[Callable] = None,
          now: Optional[datetime] = None) -> dict:
    """전략 문헌 정찰 1회. **결과는 후보이지 검증된 전략이 아니다.**

    Replay 에서는 애초에 막힌다(web_search 가 거부) - 과거 재현에서 오늘의
    웹을 보면 그때 몰랐던 기법을 아는 셈이다.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                           / "01-research" / "evidence"))
    from web_search import SearchRequest, WebSearchError  # noqa: E402
    from web_search import search as web_search  # noqa: E402

    req = SearchRequest(question=question, reason=reason,
                        requester=SCOUT_PERSONA)
    try:
        hits = (search or web_search)(req, persona=SCOUT_PERSONA,
                                      run_mode=run_mode)
    except WebSearchError as e:
        # 검색 실패를 빈 결과로 위장하지 않는다 - 사유가 남아야 사람이 안다
        return {"ok": False, "reason": f"{type(e).__name__}: {e}"[:200],
                "leads": []}

    leads = to_leads(hits, now=now)
    return {
        "ok": True,
        "question": question,
        "leads": [ld.__dict__ for ld in leads],
        "primary_sources": sum(1 for ld in leads if ld.is_primary_source),
        # ▶ 정찰 결과는 그대로 전략이 되지 않는다. 기존 경로를 통과해야 한다.
        "next_step": "feasibility -> PIT dataset -> backtest -> walk-forward",
        "not_verified": "웹 결과는 SEARCH_HIT 이다. 백테스트를 통과하기 전까지 "
                        "전략으로 다루지 않는다",
    }


# ── 자체 점검 ────────────────────────────────────────────────────────────────

class _Hit:
    def __init__(self, url, title="", snippet="", engine="searxng:x"):
        self.url, self.title, self.snippet, self.engine = url, title, snippet, engine


_NOW = datetime(2026, 8, 4, tzinfo=timezone.utc)


def _check_primary_source_first():
    """2차 요약보다 원출처를 앞세우는가. 요약은 표본·비용 가정을 흘린다."""
    leads = to_leads([
        _Hit("https://blog.example.com/a", "Momentum 요약", "2019 논문 정리"),
        _Hit("https://arxiv.org/abs/2401.1", "Cross-sectional momentum", "2024"),
    ], now=_NOW)
    assert leads[0].host == "arxiv.org", [x.host for x in leads]
    assert leads[0].is_primary_source is True
    assert leads[1].is_primary_source is False


def _check_year_extraction_rejects_future():
    """미래 연도를 공표일로 삼지 않는다."""
    assert _year_from("published 2024 revised 2025", now_year=2026) == 2025
    assert _year_from("forecast for 2099", now_year=2026) is None
    assert _year_from("no year here", now_year=2026) is None


def _check_derived_vs_replicated():
    """**컨셉 차용과 사양 복제는 다르게 다뤄야 한다.**

    컨셉만 빌려 우리가 구현했으면 공표일로 구간을 자를 근거가 약하다 -
    우리 구현은 원본과 다른 물건이다. 과적합의 주된 위험은 우리가 고른
    파라미터이고 그건 trial_pressure 가 본다.

    반대로 사양을 그대로 복제했으면 공표 이전 구간은 구조적 in-sample 이다.
    """
    leads = to_leads([_Hit("https://x.com/a", "no date", "")], now=_NOW)
    d = sample_boundary(leads, backtest_start=date(2024, 1, 2), derived=True)
    assert d["oos_start"] is None and "trial_pressure" in d["caution"], d
    r = sample_boundary(leads, backtest_start=date(2024, 1, 2), derived=False)
    assert "전 구간을 in-sample" in r["caution"], r


def _check_publication_before_backtest_is_clean():
    """공표가 백테스트 시작보다 앞서면 경고가 없다 - 가드가 과하면 무시된다."""
    leads = to_leads([_Hit("https://arxiv.org/abs/1", "Momentum", "2019")], now=_NOW)
    b = sample_boundary(leads, backtest_start=date(2024, 1, 2))
    assert b["oos_start"] == "2019-12-31" and b["caution"] is None, b
    assert b["in_sample_through"] is None


def _check_publication_inside_window_is_flagged():
    """구간 안에서 공표됐으면 사실은 남기되, 차용 여부로 처분이 갈린다."""
    leads = to_leads([_Hit("https://ssrn.com/abs/2", "New factor", "2025")], now=_NOW)
    d = sample_boundary(leads, backtest_start=date(2024, 1, 2), derived=True)
    # 컨셉 차용 - 구간을 자르지 않되 감쇠는 의심 재료로 남긴다
    assert d["in_sample_through"] is None and "감쇠" in d["caution"], d
    r = sample_boundary(leads, backtest_start=date(2024, 1, 2), derived=False)
    # 사양 복제 - 공표 이전은 구조적 in-sample
    assert r["in_sample_through"] == "2025-12-31", r
    assert "구조적 in-sample" in r["caution"], r


def _check_search_failure_is_not_empty_result():
    """검색 실패를 '결과 0건' 으로 위장하지 않는다."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                           / "01-research" / "evidence"))
    from web_search import WebSearchError

    def boom(req, **kw):
        raise WebSearchError("백엔드 없음")

    r = scout("q", reason="내부 근거 없음", search=boom)
    assert r["ok"] is False and "백엔드" in r["reason"], r
    assert r["leads"] == []


def _check_scout_does_not_bypass_pipeline():
    """정찰 결과가 파이프라인을 건너뛰지 않는다는 것이 출력에 남는가."""
    r = scout("q", reason="r", now=_NOW,
              search=lambda req, **kw: [_Hit("https://arxiv.org/abs/3", "M", "2020")])
    assert r["ok"] is True and len(r["leads"]) == 1
    assert "backtest" in r["next_step"] and "walk-forward" in r["next_step"]
    assert "전략으로 다루지 않는다" in r["not_verified"]


def _check_candidate_cap():
    hits = [_Hit(f"https://arxiv.org/abs/{i}", f"T{i}", "2024") for i in range(9)]
    assert len(to_leads(hits, now=_NOW)) == MAX_CANDIDATES


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{AGENT_VERSION} 자체 점검 (네트워크 없음)")
    _check_primary_source_first();            print("  원출처 우선            OK")
    _check_year_extraction_rejects_future();  print("  미래 연도 거부          OK")
    _check_derived_vs_replicated();           print("  컨셉차용 != 사양복제    OK")
    _check_publication_before_backtest_is_clean(); print("  공표 선행=경고 없음     OK")
    _check_publication_inside_window_is_flagged(); print("  구간 내 공표=경고       OK")
    _check_search_failure_is_not_empty_result(); print("  검색 실패 != 0건        OK")
    _check_scout_does_not_bypass_pipeline();  print("  파이프라인 우회 금지     OK")
    _check_candidate_cap();                   print("  후보 상한              OK")
    print("전략 정찰 8개 영역 통과.")
