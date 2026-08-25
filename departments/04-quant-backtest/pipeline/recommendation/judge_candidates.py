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
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, "/app/departments/01-research/api")
sys.path.insert(0, "/app")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import external_sources as ex
from departments.worker_model_gateway import llm_for_worker

from instrument_scoring import COMPOSITE_OK, STATUS_OK, AxisScore, abstain, blend_axes
from narrative_axes import judge_combined, prefilter

IN_PATH = os.environ.get("CARDS_IN", "/tmp/cards.json")
OUT_PATH = os.environ.get("CARDS_OUT", "/tmp/cards_final.json")
NEWS_N = int(os.environ.get("NEWS_N", "10"))
DISCLOSURE_DAYS = int(os.environ.get("DISCLOSURE_DAYS", "14"))
# vLLM 을 독점하지 않는 선. 같은 서버를 부서 워커들이 같이 쓴다.
# 실측: 4종목 기준 동시성 3 -> 83초, 4 -> 47초, 8 -> 46초.
# 종목 수를 넘으면 이득이 없고 vLLM 만 점유한다.
CONCURRENCY = int(os.environ.get("JUDGE_CONCURRENCY", "6"))


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


def _rehydrate(card: dict) -> tuple[list[AxisScore], list[dict]]:
    """카드의 축을 **채점용**과 **관측용**으로 가른다.

    `AxisScore` 는 status=OK 면 숫자 value 를 요구한다 - 그 불변식이
    `blend_axes` 의 가중합을 지킨다. 관측 기반 추천 카드의 축(ownership·
    theme 등)은 점수가 아니라 사실이라 value 가 없고, 가중치표에도 없다.
    그대로 밀어넣으면 "ownership: OK 인데 value 가 없다" 로 죽는다(실측).

    돌려주는 것: (채점 가능한 축, 그대로 실어 보낼 관측 축)
    """
    scored: list[AxisScore] = []
    passthrough: list[dict] = []
    for a in card["axes"]:
        # 뉴스·공시·테마는 아래에서 다시 붙인다 - 안 빼면 축이 두 번 실린다.
        if a["axis"] in {"news", "disclosure", "theme"}:
            continue
        if a["status"] == STATUS_OK and a.get("value") is None:
            passthrough.append(a)          # 관측 - 채점하지 않는다
        elif a["status"] == STATUS_OK:
            scored.append(AxisScore(a["axis"], STATUS_OK, a["value"],
                                    {"summary": a.get("summary", "")}))
        else:
            scored.append(AxisScore(a["axis"], a["status"], None, {}, (),
                                    a.get("reason", "")))
    return scored, passthrough


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

    def process(card: dict) -> dict:
        symbol = card["symbol"]
        company = card.get("company") or ""
        if not company:
            card["axes"] += [
                {"axis": "news", "status": "ABSTAINED", "value": None,
                 "reason": "회사명 미상 - 코드로는 뉴스 검색 불가", "summary": ""},
                {"axis": "disclosure", "status": "ABSTAINED", "value": None,
                 "reason": "회사명 미상", "summary": ""},
            ]
            return card

        axes, observed = _rehydrate(card)

        # ── 뉴스·공시 (LLM 한 번) ────────────────────────────────────────
        # 둘은 같은 종목의 같은 시점 재료이고 판정 기준도 같다. 따로 물으면
        # 호출이 두 배이고, 모델이 "증자 공시"와 "증자 우려 기사"를 각각 세어
        # 같은 사건을 중복 계산한다.
        kept, raw_disc = [], []
        try:
            raw_news, _ = fetch_news(company)
            kept = prefilter(raw_news, company, text_keys=("title", "description"))
        except Exception as exc:  # noqa: BLE001
            print(f"{symbol} 뉴스 조회 실패: {type(exc).__name__}")
        try:
            raw_disc, _ = fetch_disclosures(company)
        except Exception as exc:  # noqa: BLE001
            print(f"{symbol} 공시 조회 실패: {type(exc).__name__}")
        print(f"{symbol} {company}: 뉴스 {len(kept)}건 · 공시 {len(raw_disc)}건",
              flush=True)
        ax, axd = judge_combined(kept, raw_disc, company, llm)
        axes.append(ax)
        axes.append(axd)
        news_detail, disc_detail = ax.detail, axd.detail

        # 테마는 이제 소스가 있다(ls:t1532). 카드에 실려 왔으면 그대로 쓰고,
        # 없으면 그때만 기권이다 - 예전처럼 무조건 NO_SOURCE 로 덮지 않는다.
        theme_axis = next((a for a in card["axes"] if a["axis"] == "theme"), None)
        if theme_axis and theme_axis.get("status") == STATUS_OK:
            observed.append(theme_axis)
        elif theme_axis:
            axes.append(abstain("theme", theme_axis.get("reason") or "테마 없음"))
        else:
            axes.append(abstain("theme", "조회하지 않음"))

        comp = blend_axes(axes)
        # 관측 축은 채점 뒤에 다시 얹는다 - 답변에는 나가야 하지만
        # 가중합에는 들어가지 않는다.
        card["axes"] = observed + [
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
        return card

    t_judge = time.time()
    with ThreadPoolExecutor(
            max_workers=max(1, min(CONCURRENCY, len(cards)))) as pool:
        out = list(pool.map(process, cards))
    print(f"판정 {len(out)}종목 {time.time()-t_judge:.0f}초 "
          f"(동시성 {CONCURRENCY})", flush=True)

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
            close = c.get("last_close")
            close_txt = f"{close:,.0f}" if isinstance(close, (int, float)) else "—"
            print(f"종합 {comp['display']}/100   현재가 {close_txt}   "
                  f"유효축 {comp['effective_weight']:.0%}")
        else:
            print(f"종합 산출 안 함 - {comp['reason']}")
        # 계획이 기각되면 진입·목표·손절이 전부 None 이다. 미리보기가 죽으면
        # 산출 파일은 이미 저장됐는데도 파이프라인이 멈춘다(set -e).
        p = c.get("plan") or {}

        def _n(key: str) -> str:
            v = p.get(key)
            return f"{v:,.0f}" if isinstance(v, (int, float)) else "—"

        if p.get("target") is None:
            print(f"  가격계획 없음 — {p.get('reason') or p.get('status') or '사유 미상'}")
        else:
            print(f"  진입 {_n('entry_low')}~{_n('entry_high')}  "
                  f"목표 {_n('target')}  손절 {_n('stop')}  RR {p.get('reward_risk')}")
        for a in c["axes"]:
            contrib = comp.get("contributions", {}).get(a["axis"])
            if a["status"] != STATUS_OK:
                print(f"  {a['axis']:<12}   —      —       {a['status']} · {a['reason']}")
            elif a.get("value") is None:
                # 관측 축은 점수가 없다 - 값 자리에 숫자를 찍으면 안 된다.
                print(f"  {a['axis']:<12} 관측     —       {a.get('summary','')}")
            else:
                cs = f"{contrib:+.3f}" if isinstance(contrib, (int, float)) else "  —  "
                print(f"  {a['axis']:<12}{a['value']:+.3f}  기여 {cs}  {a.get('summary','')}")
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
