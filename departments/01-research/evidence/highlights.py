#!/usr/bin/env python3
"""분석가 readout 에서 **코드가** 핵심 수치를 골라낸다.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-03 실측 결과 개선.
      계측(scripts/measure_agents.py) 실측 2026-08-03:
        microstructure  재료 25개 -> LLM 인용 3개 (12%)
        geopolitical    재료 18개 -> LLM 인용 3개 (18%)
        regime          재료 20개 -> LLM 인용 2개 (10%)

▶ 왜 이런 일이 생기나 - 모델을 탓할 문제가 아니다
  서술은 "한국어 2~3문장, 600자"로 묶여 있다. 재료가 25개면 **애초에 다 못
  쓴다.** 그런데 Packet 에는 summary 와 cautions 만 실리므로, 언급되지 않은
  22개는 계산해놓고 버려진다. 프롬프트로 "더 인용하라"고 해봐야 문장 수
  제한과 싸우는 것이고, 늘리면 이번엔 서술이 장황해져 환각 표면이 넓어진다.

▶ 그래서 LLM 에게 더 시키지 않고 코드가 싣는다
  이 저장소의 규율이 이미 그렇다 - **판정은 코드, 서술만 LLM.** 무엇이 중요한
  재료인지도 판정의 일부다. 임계값을 넘었거나 방향이 뚜렷한 수치를 코드가
  골라 Packet 에 그대로 싣는다. 그러면:
    - 재료가 LLM 문장력에 의존하지 않고 Packet 에 도달한다
    - 고른 근거(왜 뽑혔는지)가 규칙으로 남아 재검산된다
    - LLM 서술은 짧게 유지돼 환각 표면이 안 넓어진다

▶ 고르는 규칙 (결정론)
  1. 명시적 임계 위반이 있으면 최우선 (flags 에 든 것)
  2. 그다음은 **0 에서 먼 순서** - 방향이 뚜렷한 수치가 정보량이 크다.
     같은 크기면 키 이름 사전순(결정론 - 같은 입력이면 같은 출력).
  3. 미확인(None)은 뽑지 않되 **몇 개가 미확인인지 함께 낸다** - 빠진 것을
     조용히 감추지 않는다.

실행: python evidence/highlights.py     # 자체 점검(네트워크 없음)
"""
from __future__ import annotations

import sys

MODULE_VERSION = "research-highlights-v1"

DEFAULT_TOP_N = 8

# 재료가 이 수 이하이면 상한을 적용하지 않고 **전부 싣는다**. 실측(2026-08-03)에서
# 고정 8 이 미시구조 13개 중 5개를 조용히 버렸다 - 계산해 놓고 버리는 낭비다.
# 사람이 한 줄로 읽을 수 있는 한계를 넘지 않는 선에서 잡았다.
ADAPTIVE_KEEP_ALL_UPTO = 14

# 지표가 아닌 bookkeeping - 뽑지 않는다(규칙표·사유·개수)
NOT_MATERIAL = frozenset({
    "regime_rules", "tilt_rules", "flag_criteria", "assessment_rule",
    "depth_imbalance_convention", "cautions", "method_keys", "label_reason",
    "driver", "days_used", "bars_used", "readout_version", "as_of",
    "latest_trade_date", "last_close_date", "ticks_date", "quotes_date",
    "first_trade", "last_trade", "lookback_used", "days", "window_requested",
})

# 단위 - 있으면 붙여 읽기 쉽게. 없으면 숫자만 낸다(지어내지 않는다).
UNIT_HINTS = (
    ("_pct", "%"), ("_bp", "bp"), ("_ratio", "배"), ("_bp_p50", "bp"),
    ("_bp_p90", "bp"), ("percentile", "%"),
)


def unit_for(key: str) -> str:
    for suffix, unit in UNIT_HINTS:
        if key.endswith(suffix):
            return unit
    return ""


def flatten_metrics(readout, *, containers=("fields", "ratios", "volatility",
                                            "liquidity", "macro_overlay")) -> dict:
    """readout -> {키: 수치}. 컨테이너 한 겹은 펼친다.

    두 겹 이상은 파고들지 않는다 - 깊은 곳의 수치는 맥락 없이 뽑으면 의미가
    흐려진다(예: tilt_rules 안의 임계값을 '지표'로 내면 오독된다).
    """
    out: dict[str, float] = {}
    if not isinstance(readout, dict):
        return out
    for k, v in readout.items():
        if k in NOT_MATERIAL or k.endswith("_rules"):
            continue
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = float(v)
        elif k in containers and isinstance(v, dict):
            for ik, iv in v.items():
                if ik in NOT_MATERIAL:
                    continue
                # fields 는 {이름: {value: ...}} 모양이다
                val = iv.get("value") if isinstance(iv, dict) and "value" in iv else iv
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    out[f"{k}.{ik}" if k != "fields" else ik] = float(val)
    return out


def pick_highlights(readout, *, top_n: int | None = None,
                    flags=()) -> dict:
    """readout -> Packet 에 실을 핵심 수치.

    반환: {"items": [{key, value, unit}], "flags": [...],
           "total_metrics": n, "unknown": m, "coverage": 0~1}
    coverage 는 **뽑힌 수 / 확인된 재료 수** 다 - 이 값이 곧 "계산한 것 중
    얼마가 Packet 에 도달했는가"이고, 계측이 재던 인용밀도를 대체한다.
    """
    metrics = flatten_metrics(readout)
    unknown = 0
    if isinstance(readout, dict):
        for k, v in readout.items():
            if k in NOT_MATERIAL or k.endswith("_rules"):
                continue
            if v is None:
                unknown += 1
            elif isinstance(v, dict) and k in ("fields", "ratios"):
                unknown += sum(
                    1 for iv in v.values()
                    if (iv.get("value") if isinstance(iv, dict) and "value" in iv
                        else iv) is None)

    # 0 에서 먼 순 -> 같으면 이름순 (결정론)
    ordered = sorted(metrics.items(), key=lambda kv: (-abs(kv[1]), kv[0]))
    known = len(metrics)

    # ▶ 상한을 적응시킨다 (2026-08-03 실측)
    #   고정 8 이면 재료가 13개인 미시구조에서 5개가 조용히 버려졌다. 계산해 놓고
    #   버리는 것은 계측이 지목한 바로 그 낭비다. 재료가 적으면 전부 싣는다 -
    #   상한은 '너무 길어져 사람이 못 읽는 것' 을 막으려는 것이지 재료를 줄이려는
    #   것이 아니다.
    #
    #   **호출자가 top_n 을 명시하면 그 값을 존중한다.** 적응은 기본값(None)일
    #   때만이다 - 안 그러면 호출자가 건 상한을 코드가 조용히 무시하게 된다.
    if top_n is not None:
        effective_n = top_n
    else:
        effective_n = known if known <= ADAPTIVE_KEEP_ALL_UPTO else DEFAULT_TOP_N
    items = [{"key": k, "value": round(v, 4), "unit": unit_for(k)}
             for k, v in ordered[:effective_n]]
    dropped = [k for k, _ in ordered[effective_n:]]
    return {
        "items": items,
        "flags": list(flags or []),
        "total_metrics": known,
        "unknown": unknown,
        "coverage": round(len(items) / known, 3) if known else 0.0,
        # **버린 것을 숨기지 않는다.** coverage 만 보면 무엇이 빠졌는지 알 수 없다.
        "dropped": dropped,
        "rule": (f"임계 위반 우선, 그다음 |값| 큰 순 상위 {effective_n} "
                 f"(같으면 키 이름순). 재료 {ADAPTIVE_KEEP_ALL_UPTO}개 이하면 "
                 f"전부 싣는다. 미확인은 뽑지 않고 개수로 보고한다."),
    }


def render_line(h: dict) -> str:
    """사람이 읽는 한 줄 - Packet 리포트에 그대로 들어간다."""
    if not h.get("items"):
        return f"핵심 수치 없음 (재료 {h.get('total_metrics', 0)}, 미확인 {h.get('unknown', 0)})"
    body = ", ".join(f"{i['key']} {i['value']}{i['unit']}" for i in h["items"])
    tail = f" | 미확인 {h['unknown']}" if h.get("unknown") else ""
    return f"{body} (재료 {h['total_metrics']} 중 {len(h['items'])}){tail}"


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크 없음
# ---------------------------------------------------------------------------

def _check_flatten():
    r = {"a": 1.5, "b": None, "c": "문자열", "days_used": 40,
         "regime_rules": {"x": 1}, "flag_criteria": "...",
         "fields": {"매출액": {"value": 100.0}, "영업이익": {"value": None}},
         "ratios": {"BOND_FUT": {"ratio": 1.05, "change_pct": 5.0}}}
    m = flatten_metrics(r)
    assert m["a"] == 1.5
    assert "b" not in m and "c" not in m, "None·문자열은 수치가 아니다"
    assert "days_used" not in m, "개수는 지표가 아니다"
    assert "regime_rules" not in m and "flag_criteria" not in m
    assert m["매출액"] == 100.0, "fields 는 접두사 없이 펼친다"
    assert "영업이익" not in m, "value 가 None 이면 뽑지 않는다"
    # 두 겹 이상은 안 판다 - ratios.BOND_FUT 는 dict 라 그 안은 안 본다
    assert not any(k.startswith("ratios.BOND_FUT.") for k in m), m
    print("  재료 평탄화              OK")


def _check_pick_order():
    r = {"small": 0.1, "big": -50.0, "mid": 3.0}
    h = pick_highlights(r, top_n=2)
    assert [i["key"] for i in h["items"]] == ["big", "mid"], h["items"]
    assert h["items"][0]["value"] == -50.0, "부호를 지우지 않는다"
    # 같은 크기면 이름순 - 결정론이어야 재검산된다
    h2 = pick_highlights({"z": 5.0, "a": -5.0}, top_n=2)
    assert [i["key"] for i in h2["items"]] == ["a", "z"], h2["items"]
    print("  중요도 정렬(결정론)      OK")


def _check_unit_and_coverage():
    r = {"above_sma20_pct": 27.4, "spread_bp_p50": 12.0, "ad_ratio": 5.8,
         "x": None, "y": None}
    h = pick_highlights(r, top_n=3)
    units = {i["key"]: i["unit"] for i in h["items"]}
    assert units["above_sma20_pct"] == "%" and units["ad_ratio"] == "배"
    assert units["spread_bp_p50"] == "bp"
    assert h["total_metrics"] == 3 and h["unknown"] == 2
    assert h["coverage"] == 1.0, "3개 중 3개를 뽑았으면 100%"
    # 미확인을 조용히 감추지 않는다
    assert "미확인 2" in render_line(h), render_line(h)
    print("  단위·커버리지 보고       OK")


def _check_adaptive_cap():
    """재료가 적으면 전부 싣는다 - 계산해 놓고 버리지 않는다."""
    small = {f"m{i}": float(i + 1) for i in range(13)}      # 13개
    h = pick_highlights(small)                              # top_n 미지정 = 적응
    assert len(h["items"]) == 13, len(h["items"])
    assert h["coverage"] == 1.0 and h["dropped"] == []

    big = {f"m{i}": float(i + 1) for i in range(30)}        # 30개
    hb = pick_highlights(big)
    assert len(hb["items"]) == DEFAULT_TOP_N, len(hb["items"])
    # 버린 것을 숨기지 않는다
    assert len(hb["dropped"]) == 30 - DEFAULT_TOP_N, len(hb["dropped"])
    assert set(hb["dropped"]) & set(big), "버린 키 이름이 실제 재료여야 한다"

    # 호출자가 명시하면 그 값을 존중한다 - 조용히 무시하지 않는다
    hx = pick_highlights(small, top_n=2)
    assert len(hx["items"]) == 2 and len(hx["dropped"]) == 11
    print("  적응 상한·버린 것 보고   OK")


def _check_empty_is_honest():
    h = pick_highlights({}, top_n=5)
    assert h["items"] == [] and h["coverage"] == 0.0
    assert "핵심 수치 없음" in render_line(h)
    # None 만 있어도 0 으로 위장하지 않는다
    h2 = pick_highlights({"a": None, "b": None})
    assert h2["total_metrics"] == 0 and h2["unknown"] == 2
    print("  빈 결과 정직 처리        OK")


def _check_flags_first():
    h = pick_highlights({"a": 1.0}, flags=["SPREAD_WIDE", "DEPTH_SKEW"])
    assert h["flags"] == ["SPREAD_WIDE", "DEPTH_SKEW"]
    print("  임계 플래그 동반         OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크 없음)")
    _check_flatten()
    _check_pick_order()
    _check_unit_and_coverage()
    _check_adaptive_cap()
    _check_empty_is_honest()
    _check_flags_first()
    print("Highlights 5개 영역 통과.")
