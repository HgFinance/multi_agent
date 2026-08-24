"""2층 실행기 - 1.5층이 낸 카드에 뉴스·공시 축을 붙이고 최종 카드를 낸다.

research-mcp 안에서 돌아야 한다. 거기에만 세 가지가 다 있다:
  NAVER_CLIENT_ID/SECRET (뉴스), OPEN_DART_API_KEY (공시),
  WORKER_MODEL_BASE_URL (vLLM Qwen2.5-14B - docker-compose.model.yml 오버레이).

DART 첫 조회 주의: `_load_corp_index()` 가 corpCode.xml 을 받는 데 227초 걸린다
(실측 2026-08-24, 상장사 3,985건). 프로세스 안에서만 캐시되므로 컨테이너를
새로 띄우면 다시 문다. 아래에서 **한 번만 미리 데워두고** 그 시간을 로그로 남긴다.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, "/app/departments/01-research/api")
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import external_sources as ex
from departments.worker_model_gateway import llm_for_worker

from instrument_scoring import COMPOSITE_OK, STATUS_OK, AxisScore, abstain, blend_axes
from narrative_axes import disclosure_axis, news_axis, prefilter

IN_PATH = os.environ.get("CARDS_IN", "/tmp/cards.json")
OUT_PATH = os.environ.get("CARDS_OUT", "/tmp/cards_final.json")
NEWS_N = int(os.environ.get("NEWS_N", "10"))
DISCLOSURE_DAYS = int(os.environ.get("DISCLOSURE_DAYS", "14"))


def fetch_news(company: str) -> tuple[list[dict], str]:
    """네이버 뉴스 최신 N건. 반환 형식을 narrative_axes 가 기대하는 모양으로 맞춘다."""
    r = ex.news_search(query=company, display=NEWS_N, sort="date")
    citation = str(r.get("citation") or "")
    items = []
    for i, n in enumerate(r.get("items", [])):
        items.append({
            "ref": f"n{i + 1}",
            "title": n.get("title"),
            "description": n.get("description"),
            "published_at": str(n.get("pubDate") or ""),
            "url": n.get("originallink") or n.get("link"),
            "citation": citation,
            "evidence_id": f"news_search:{citation}:{i + 1}",
        })
    return items, citation


def fetch_disclosures(company: str) -> tuple[list[dict], str]:
    r = ex.dart_search_disclosures(corp=company, days=DISCLOSURE_DAYS, page=1)
    citation = str(r.get("citation") or "")
    items = []
    for i, d in enumerate(r.get("items", [])):
        items.append({
            "ref": f"d{i + 1}",
            "title": d.get("report_nm"),
            "published_at": str(d.get("rcept_dt") or ""),
            "url": d.get("viewer_url"),
            "citation": citation,
            "evidence_id": f"dart:{d.get('rcept_no') or citation}:{i + 1}",
        })
    return items, citation


def _rehydrate(card: dict) -> list[AxisScore]:
    """1.5층 카드의 축 목록을 AxisScore 로 되돌린다(뉴스·공시·테마는 뺀다)."""
    axes = []
    for a in card["axes"]:
        # theme 도 뺀다 - 아래에서 다시 붙인다. 안 빼면 축이 두 번 실려
        # 카드에 중복 출력되고, 그 축이 OK 였다면 가중치가 두 번 세어진다.
        if a["axis"] in {"news", "disclosure", "theme"}:
            continue
        if a["status"] == STATUS_OK:
            axes.append(AxisScore(a["axis"], STATUS_OK, a["value"],
                                  {"summary": a.get("summary", "")}))
        else:
            axes.append(AxisScore(a["axis"], a["status"], None, {}, (), a.get("reason", "")))
    return axes


def main() -> int:
    cards = json.load(open(IN_PATH, encoding="utf-8"))
    llm, binding = llm_for_worker("holdings-analyst-worker")
    print(f"model: {binding.model}  @ {binding.base_url}  timeout={binding.timeout_seconds}s")

    # DART 색인 워밍업 - 첫 종목이 이 비용을 혼자 뒤집어쓰지 않게 미리 뺀다.
    warm = time.time()
    try:
        idx = ex._load_corp_index()
        print(f"DART 기업색인 워밍업 {time.time()-warm:.0f}s (상장사 {len(idx)}건)")
    except Exception as exc:  # noqa: BLE001
        print(f"DART 기업색인 워밍업 실패 {type(exc).__name__} - 공시 축은 기권된다")

    out = []
    for card in cards:
        symbol = card["symbol"]
        company = card.get("company") or ""
        if not company:
            card["axes"] += [
                {"axis": "news", "status": "ABSTAINED", "value": None,
                 "reason": "회사명 미상 - 코드로는 뉴스 검색 불가", "summary": ""},
                {"axis": "disclosure", "status": "ABSTAINED", "value": None,
                 "reason": "회사명 미상", "summary": ""},
            ]
            out.append(card)
            continue

        axes = _rehydrate(card)

        # ── 뉴스 ──────────────────────────────────────────────────────────
        try:
            raw_news, _ = fetch_news(company)
            kept = prefilter(raw_news, company, text_keys=("title", "description"))
            print(f"{symbol} {company}: 뉴스 {len(raw_news)}건 -> 사전필터 {len(kept)}건")
            ax = news_axis(kept, company, llm)
        except Exception as exc:  # noqa: BLE001
            ax = abstain("news", f"조회 실패 {type(exc).__name__}: {str(exc)[:80]}")
        axes.append(ax)
        news_detail = ax.detail

        # ── 공시 ──────────────────────────────────────────────────────────
        try:
            raw_disc, _ = fetch_disclosures(company)
            print(f"{symbol} {company}: 공시 {len(raw_disc)}건({DISCLOSURE_DAYS}일)")
            axd = disclosure_axis(raw_disc, company, llm)
        except Exception as exc:  # noqa: BLE001
            axd = abstain("disclosure", f"조회 실패 {type(exc).__name__}: {str(exc)[:80]}")
        axes.append(axd)
        disc_detail = axd.detail

        axes.append(abstain("theme", "수집 소스 없음", no_source=True))

        comp = blend_axes(axes)
        card["axes"] = [
            {"axis": a.axis, "status": a.status, "value": a.value,
             "reason": a.reason,
             "summary": a.detail.get("summary", "") if a.axis not in {"news", "disclosure"}
             else f"{a.detail.get('호재', 0)}호재/{a.detail.get('악재', 0)}악재 "
                  f"(관련 {a.detail.get('relevant', 0)}/{a.detail.get('judged', 0)})"}
            for a in axes
        ]
        card["composite"] = {
            "status": comp.status, "value": comp.value, "display": comp.display,
            "effective_weight": comp.effective_weight,
            "contributions": comp.contributions,
            "unreported": list(comp.unreported), "reason": comp.reason,
        }
        card["narrative"] = {
            "news": news_detail.get("items", []),
            "disclosures": disc_detail.get("items", []),
        }
        out.append(card)

    out.sort(key=lambda c: (c["composite"]["value"] is None,
                            -(c["composite"]["value"] or 0)))
    with open(OUT_PATH, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=1)

    # 렌더
    for c in out:
        comp = c["composite"]
        print("=" * 78)
        print(f"{c['symbol']} {c.get('company','')}  ·  {c.get('업종') or '업종 미상'}")
        if comp["status"] == COMPOSITE_OK:
            print(f"종합 {comp['display']}/100   현재가 {c['last_close']:,.0f}   "
                  f"유효축 {comp['effective_weight']:.0%}")
        else:
            print(f"종합 산출 안 함 - {comp['reason']}")
        p = c["plan"]
        print(f"  진입 {p['entry_low']:,.0f}~{p['entry_high']:,.0f}  "
              f"목표 {p['target']:,.0f}  손절 {p['stop']:,.0f}  RR {p['reward_risk']}")
        for a in c["axes"]:
            contrib = comp.get("contributions", {}).get(a["axis"])
            if a["status"] == STATUS_OK:
                print(f"  {a['axis']:<12}{a['value']:+.3f}  기여 {contrib:+.3f}  {a['summary']}")
            else:
                print(f"  {a['axis']:<12}   —      —       {a['status']} · {a['reason']}")
        for n in c.get("narrative", {}).get("news", [])[:4]:
            print(f"    [{n['polarity']}/{n['impact']}] {n['title'][:52]} — {n['why']}")
        for d in c.get("narrative", {}).get("disclosures", [])[:3]:
            print(f"    공시[{d['polarity']}/{d['impact']}] {d['title'][:44]} — {d['why']}")
    print("=" * 78)
    print(f"-> {OUT_PATH}")
    print("budget:", ex.budget_state())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
