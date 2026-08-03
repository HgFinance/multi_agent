#!/usr/bin/env python3
"""리서치본부 에이전트 실측 계측기 - 각 분석가가 무엇을 어떻게 내는지 숫자로.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-03 "각 에이전트들이 어떻게 분석하고 대화하고
      OUTPUT 을 내는지 측정해보자".

▶ 왜 별도 계측기인가
  pipeline_runs 에는 좌표(해시·상태·판정)만 남는다. "이 분석가가 결정론
  재료를 몇 개 만들었고, LLM 이 그중 몇 개를 인용했고, 검증이 몇 개를
  걸러냈는가"는 어디에도 안 남는다. 그게 없으면 **어느 분석가가 약한지**를
  취향으로 말하게 된다.

▶ 무엇을 재는가 (분석가별)
  latency_s        총 소요 - 사람이 기다리는 시간
  readout_keys     결정론 계산이 만든 재료 수 (많을수록 서술할 게 많다)
  readout_null     그중 미확인(None) 수 - 데이터 부족의 직접 지표
  summary_chars    LLM 서술 길이
  used_metrics     LLM 이 실제 인용한 키 수
  citation_rate    used_metrics / (readout_keys - null) - **인용 밀도**
  hallucinated     readout 에 없는 키를 인용한 수 (검증이 잡아낸 것)
  flagged_numbers  readout 수치와 안 맞는 %·배 표기 수
  label_flags      라벨-수치 오서술 의심 수
  restored         LLM 이 판정을 바꿔 코드가 되돌린 횟수

  **인용 밀도가 핵심 지표다.** 결정론 재료를 20개 만들어놓고 2개만 인용하면
  그 분석가는 계산은 하는데 말은 안 하는 상태다 - Packet 이 빈약해지는
  원인이 거기 있다.

▶ LLM 을 실제로 호출한다
  자체 점검은 가짜 LLM 을 쓰지만, 계측은 실측이어야 의미가 있다.
  --measure 는 Ollama·API 가 살아 있어야 한다.

사용
  python scripts/measure_agents.py                    # 자체 점검
  python scripts/measure_agents.py --measure 000660   # 실측 1종목
  python scripts/measure_agents.py --measure 000660,005930 --json out.json
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_BASE = Path(__file__).resolve().parents[1]
for sub in ("agents", "collectors", "evidence", ""):
    sys.path.insert(0, str(_BASE / sub) if sub else str(_BASE))

MEASURE_VERSION = "research-agent-measure-v1"

# 종목 무관 분석가 - 시장/외생 국면이라 심볼을 안 받는다.
# 이걸 구분 안 하면 "왜 이 분석가는 종목을 바꿔도 같은 답이지?" 를 결함으로
# 오해한다(그게 정상이다).
MARKET_WIDE = ("regime", "geopolitical")


# 재료가 **중첩 컨테이너 안에** 있는 분석가가 있다. RES-05 는 readout 최상위가
# {verdict, scope, period_end, fields, cautions} 5개뿐이고 실제 재료는 fields
# 안에 있는데, LLM 은 그 안쪽 이름(used_fields)을 인용한다. 최상위만 세면
# 분모가 5, 분자가 10 이 되어 **인용밀도가 2.0** 으로 나온다(2026-08-03 실측
# 결함). 컨테이너를 펼쳐야 같은 단위로 비교된다.
_CONTAINER_KEYS = ("fields", "ratios", "themes", "breadth_by_market",
                   "macro_overlay", "volatility", "liquidity")
# 지표가 아닌 bookkeeping - 인용 대상이 아니므로 분모에서 뺀다
_NOT_MATERIAL = ("regime_rules", "tilt_rules", "flag_criteria", "assessment_rule",
                 "depth_imbalance_convention", "cautions", "method_keys",
                 "label_reason", "driver")


def count_readout(readout) -> tuple[int, int]:
    """(재료 수, 그중 미확인 수).

    컨테이너 키(fields·ratios 등)는 **펼쳐서** 안쪽을 센다 - LLM 이 인용하는
    단위가 그쪽이기 때문이다. 규칙표·사유 문자열은 재료가 아니라 뺀다.
    """
    if not isinstance(readout, dict):
        return 0, 0
    keys, nulls = 0, 0
    for k, v in readout.items():
        if k in _NOT_MATERIAL or k.endswith("_rules"):
            continue
        if k in _CONTAINER_KEYS and isinstance(v, dict):
            for ik, iv in v.items():
                keys += 1
                # fields 는 {이름: {value:..}} 모양이라 value 를 들여다본다
                inner = iv.get("value") if isinstance(iv, dict) and "value" in iv else iv
                if inner is None:
                    nulls += 1
            continue
        keys += 1
        if v is None:
            nulls += 1
    return keys, nulls


def measure_one(name: str, fn, **kwargs) -> dict:
    """분석가 하나 실행 + 계측. 실패해도 다른 분석가 측정을 멈추지 않는다."""
    t0 = time.time()
    try:
        out = fn(**kwargs)
    except Exception as e:  # noqa: BLE001
        return {"agent": name, "ok": False, "latency_s": round(time.time() - t0, 1),
                "error": f"{type(e).__name__}: {str(e)[:120]}"}
    dt = round(time.time() - t0, 1)

    readout = out.get("readout")
    keys, nulls = count_readout(readout)
    note = out.get("note") or {}
    if hasattr(note, "model_dump"):
        note = note.model_dump()
    summary = (note.get("summary") or out.get("summary") or "")
    used = note.get("used_metrics") or note.get("used_fields") or out.get("used_metrics") or []
    cautions = note.get("cautions") or out.get("cautions") or []
    dropped = out.get("dropped") or {}
    known = max(keys - nulls, 1)

    return {
        "agent": name, "ok": True, "latency_s": dt,
        "verdict": out.get("verdict"),
        "llm_status": out.get("llm_status", "OK"),
        "readout_keys": keys, "readout_null": nulls,
        "summary_chars": len(summary),
        "used_metrics": len(used),
        "citation_rate": round(len(used) / known, 3),
        "cautions": len(cautions),
        "hallucinated": len(dropped.get("hallucinated_metrics") or []),
        "flagged_numbers": len(dropped.get("flagged_numbers") or []),
        "label_flags": len(dropped.get("label_flags") or []),
        "restored": 1 if (dropped.get("label_restored")
                          or dropped.get("assessment_restored")
                          or dropped.get("stance_demoted")) else 0,
        "market_wide": name in MARKET_WIDE,
    }


def measure_symbol(symbol: str) -> list[dict]:
    """분석가 6인을 순차 실행한다. 병렬로 돌리면 로컬 GPU 하나를 두고
    서로를 밀어 지연 수치가 무의미해진다."""
    from fundamental_analyst import analyze as fund
    from geopolitical_analyst import analyze as geo
    from microstructure_analyst import analyze as micro
    from news_sentiment_analyst import run as senti
    from sector_regime_analyst import analyze as regime
    from technical_analyst import analyze as tech

    rows = [
        measure_one("technical", tech, symbol=symbol),
        measure_one("fundamental", fund, symbol=symbol),
        measure_one("microstructure", micro, symbol=symbol),
        measure_one("regime", regime),
        measure_one("geopolitical", geo),
    ]
    # 감성은 반환 모양이 달라(SentimentReport) 별도 처리한다
    t0 = time.time()
    try:
        r = senti(symbol, hours=24.0, read_bodies=False)
        rows.append({"agent": "sentiment", "ok": True,
                     "latency_s": round(time.time() - t0, 1),
                     "verdict": r.verdict, "llm_status": "OK",
                     "readout_keys": r.articles_used, "readout_null": 0,
                     "summary_chars": 0,
                     "used_metrics": r.articles_used,
                     "citation_rate": 1.0 if r.articles_used else 0.0,
                     "cautions": 0, "hallucinated": r.articles_dropped,
                     "flagged_numbers": 0, "label_flags": 0, "restored": 0,
                     "market_wide": False})
    except Exception as e:  # noqa: BLE001
        rows.append({"agent": "sentiment", "ok": False,
                     "latency_s": round(time.time() - t0, 1),
                     "error": f"{type(e).__name__}: {str(e)[:120]}"})
    return rows


def render(rows: list[dict]) -> str:
    """사람이 읽는 표. 약한 지점이 눈에 띄게 정렬하지 않고 순서를 고정한다
    (실행 순서가 곧 파이프라인 순서라 그대로가 정보다)."""
    out = [f"{'분석가':<16}{'판정':<16}{'초':>6}{'재료':>6}{'미확인':>7}"
           f"{'인용':>6}{'밀도':>7}{'서술자':>7}{'환각':>6}{'수치':>6}{'라벨':>6}",
           "-" * 90]
    for r in rows:
        if not r.get("ok"):
            out.append(f"{r['agent']:<16}{'실패':<16}{r['latency_s']:>6}  {r.get('error','')[:50]}")
            continue
        out.append(
            f"{r['agent']:<16}{str(r.get('verdict'))[:15]:<16}{r['latency_s']:>6}"
            f"{r['readout_keys']:>6}{r['readout_null']:>7}{r['used_metrics']:>6}"
            f"{r['citation_rate']:>7.2f}{r['summary_chars']:>7}"
            f"{r['hallucinated']:>6}{r['flagged_numbers']:>6}{r['label_flags']:>6}")
    ok = [r for r in rows if r.get("ok")]
    if ok:
        out += ["-" * 90,
                f"{'합계/평균':<16}{'':<16}{sum(r['latency_s'] for r in ok):>6.1f}"
                f"{sum(r['readout_keys'] for r in ok):>6}"
                f"{sum(r['readout_null'] for r in ok):>7}"
                f"{sum(r['used_metrics'] for r in ok):>6}"
                f"{sum(r['citation_rate'] for r in ok) / len(ok):>7.2f}"
                f"{sum(r['summary_chars'] for r in ok):>7}"
                f"{sum(r['hallucinated'] for r in ok):>6}"
                f"{sum(r['flagged_numbers'] for r in ok):>6}"
                f"{sum(r['label_flags'] for r in ok):>6}"]
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 자체 점검 - LLM·네트워크 없음
# ---------------------------------------------------------------------------

def _check_count_readout():
    keys, nulls = count_readout({"a": 1, "b": None, "c": "x", "regime_rules": {}})
    assert (keys, nulls) == (3, 1), (keys, nulls)   # _rules 는 재료가 아니다
    assert count_readout(None) == (0, 0)
    assert count_readout({}) == (0, 0)
    print("  재료 계수                OK")


def _check_measure_one():
    def fake(**kw):
        return {"verdict": "NEUTRAL", "llm_status": "OK",
                "readout": {"a": 1.0, "b": None, "c": 2.0},
                "note": {"summary": "서술 다섯자", "used_metrics": ["a"],
                         "cautions": ["주의"]},
                "dropped": {"hallucinated_metrics": ["vix"],
                            "flagged_numbers": ["88.8%"],
                            "label_flags": [{"check": "x"}],
                            "label_restored": "사유"}}
    r = measure_one("t", fake, symbol="000660")
    assert r["ok"] and r["verdict"] == "NEUTRAL"
    assert r["readout_keys"] == 3 and r["readout_null"] == 1
    assert r["used_metrics"] == 1
    assert r["citation_rate"] == round(1 / 2, 3), r["citation_rate"]  # 확인된 2개 중 1개
    assert r["hallucinated"] == 1 and r["flagged_numbers"] == 1
    assert r["label_flags"] == 1 and r["restored"] == 1

    # 실패해도 측정이 멈추지 않는다 - 한 분석가가 죽어도 나머지는 재야 한다
    def boom(**kw):
        raise RuntimeError("죽음")
    b = measure_one("x", boom)
    assert not b["ok"] and "RuntimeError" in b["error"]
    print("  분석가 계측·실패 격리    OK")


def _check_render():
    rows = [{"agent": "technical", "ok": True, "latency_s": 3.2, "verdict": "NEUTRAL",
             "readout_keys": 20, "readout_null": 2, "used_metrics": 3,
             "citation_rate": 0.167, "summary_chars": 120, "hallucinated": 0,
             "flagged_numbers": 0, "label_flags": 2, "restored": 0},
            {"agent": "x", "ok": False, "latency_s": 1.0, "error": "boom"}]
    t = render(rows)
    assert "technical" in t and "NEUTRAL" in t and "실패" in t
    assert "합계/평균" in t
    print("  표 렌더링                OK")


def _check_market_wide():
    """종목 무관 분석가를 표시한다 - 종목을 바꿔도 같은 답인 게 정상이다."""
    assert "regime" in MARKET_WIDE and "geopolitical" in MARKET_WIDE
    assert "technical" not in MARKET_WIDE
    print("  시장 무관 분석가 표시    OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--measure" in sys.argv:
        syms = sys.argv[sys.argv.index("--measure") + 1].split(",")
        all_rows = {}
        for s in syms:
            print(f"\n=== {s} ===", flush=True)
            rows = measure_symbol(s.strip())
            all_rows[s] = rows
            print(render(rows), flush=True)
        if "--json" in sys.argv:
            out = Path(sys.argv[sys.argv.index("--json") + 1])
            out.write_text(json.dumps(all_rows, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            print(f"\n기록: {out}")
        raise SystemExit(0)

    print(f"{MEASURE_VERSION} 자체 점검 (LLM·네트워크 없음)")
    _check_count_readout()
    _check_measure_one()
    _check_render()
    _check_market_wide()
    print("계측기 4개 영역 통과. 실측은 --measure <종목코드>")
