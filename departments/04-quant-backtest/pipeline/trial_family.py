"""시도 Family - 같은 컨셉의 변형을 하나로 묶는다.

담당: 재일 (퀀트·백테스트본부 QNT)
근거: contracts/quant_v2 "trial_family_id 로 비슷한 실험 횟수를 누적해
      몇 번 만에 나온 결과인지 Card 에 남긴다. 12번째 시도에서 나온
      Sharpe 1.5 는 1번째와 다르다"

▶ 지금 무엇이 안 잡히나
  experiment_orchestrator 가 시도 횟수를 **edge type** 으로 센다. 그런데
  같은 컨셉으로 lookback 을 5/10/20/30... 바꿔가며 20번 돌리면 edge type 은
  하나이므로 20번이 세어지긴 한다. 문제는 반대다 - **컨셉이 다른데 edge
  type 이 같으면 남의 시도가 내 압력으로 잡힌다.**

  예: "SMA20 이탈 후 회귀" 와 "거래대금 급감 후 회귀" 는 둘 다
  mean_reversion 이지만 다른 아이디어다. 하나가 20번 돌면 다른 하나가
  첫 시도인데도 예산 초과로 막힌다.

▶ Family 를 무엇으로 보는가
  **컨셉을 정의하는 것은 넣고, 우리가 튜닝하는 것은 뺀다.**
    넣는다 : edge type, 유니버스 정의, 라벨(무엇을 맞히려는가), baseline
    뺀다   : lookback_days, top_n, rebalance - 이것들을 바꾸는 것이
             **바로 우리가 세려는 다중검정**이다

  파라미터를 Family 에 넣으면 변형마다 새 Family 가 되어 시도 압력이
  영원히 1 이 된다 - 세는 의미가 사라진다.

자체 점검: python departments/04-quant-backtest/pipeline/trial_family.py
"""

from __future__ import annotations

import hashlib
import json
import re
import sys

from strategy_templates import EDGE_VOCAB   # noqa: E402  (같은 디렉터리 모듈)

MODULE_VERSION = "quant-trial-family-v1"

# Family 를 정의하는 것. 여기 없는 것은 변형이다.
FAMILY_FIELDS = ("edge_type", "universe_key", "label", "baseline")

# ▶ **자유 서술을 Family 재료로 쓰지 않는다** (2026-08-04 실측).
#   LLM 이 낸 universe 설명은 같은 뜻도 매번 다르게 쓴다:
#     "KRX 전체 시장" vs "KRX 시장 전 종목"           -> 다른 Family 가 됐다
#     "SMA20 위에 위치한 종목" vs "20일 이동평균선 상회 종목" -> 역시
#   흩어지면 시도 압력이 안 잡힌다 - 파라미터를 넣었을 때와 같은 실패다.
#   통제 어휘로 사상하고, 못 사상하면 **Family 를 만들지 않는다**(추측보다
#   낫다 - 잘못 묶으면 남의 시도가 내 압력이 된다).
UNIVERSE_VOCAB = {
    "krx_all": (r"krx\s*(전체|시장)", r"전\s*종목", r"시장\s*전체"),
    "above_sma20": (r"sma\s*#?\s*(위|상회)", r"이동평균선?\s*(위|상회)",
                    r"#일\s*이동평균"),
    "high_turnover": (r"거래대금\s*(상위|급증)", r"유동성\s*상위"),
    "low_turnover": (r"거래대금\s*(하위|급감)",),
}

# 우리가 튜닝하는 값. **절대 Family 에 넣지 않는다** - 넣으면 변형마다
# 새 Family 가 되어 다중검정을 못 센다.
TUNED_FIELDS = ("lookback_days", "top_n", "rebalance", "holding_horizon")

# ▶ **접수와 실행면이 같은 이름공간을 써야 한다** (2026-08-11 실측).
#   Gate 0 은 label/baseline 이 비면 아래 기본값을 넣고 해시했고, 오케스트레이터는
#   가설 dict 를 그대로 넣어 빈 문자열로 해시했다. 같은 기획안 하나가
#     접수 fam_42663e9f4b0f8233  /  실행 fam_65a4c7b6f4c75999
#   두 값을 가졌다. 실험은 실행면 값으로 각인되고 Gate 0 은 접수 값으로 세니
#   **세는 계열과 각인되는 계열이 달라 계수가 영원히 0** 이었다 - 각인이
#   안 된 게 아니라 서로 다른 장부를 본 것이다. 기본값을 여기 한 곳에 두고
#   양쪽이 hypothesis_view() 를 함께 쓴다.
DEFAULT_LABEL = "forward_return"
DEFAULT_BASELINE = "equal_weight_buy_and_hold"


def _norm_text(v) -> str:
    """유니버스 설명 같은 자유 서술을 정규화. 같은 뜻이 다른 Family 가 되면
    시도가 흩어져 압력이 안 잡힌다."""
    s = str(v or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    # 숫자는 뺀다 - "상위 20%" 와 "상위 30%" 는 같은 컨셉의 변형이다
    s = re.sub(r"\d+(\.\d+)?%?", "#", s)
    return s


def universe_key(text) -> str:
    """자유 서술 -> 통제 어휘. **못 사상하면 빈 문자열이다.**

    억지로 하나로 묶으면 무관한 컨셉이 한 Family 가 되고, 그때는 남의 시도가
    내 압력이 된다. 모르면 Family 를 안 만드는 쪽이 낫다.
    """
    t = _norm_text(text)
    if not t:
        return ""
    for key, pats in UNIVERSE_VOCAB.items():
        if any(re.search(p, t) for p in pats):
            return key
    return ""


def family_key(hyp: dict) -> dict:
    """가설 -> Family 를 정하는 재료. **튜닝 값·자유 서술은 안 들어간다.**"""
    edge = hyp.get("expected_edge") or {}
    return {
        "edge_type": str(edge.get("type") or "").strip().lower(),
        # 구조화된 키가 있으면 그것을, 없으면 서술에서 사상한다
        "universe_key": (str(edge.get("universe_key") or "").strip().lower()
                         or universe_key(edge.get("universe")
                                         or hyp.get("universe"))),
        "label": _norm_text(hyp.get("label") or edge.get("label")),
        "baseline": _norm_text(hyp.get("baseline")),
    }


def hypothesis_view(*, edge_type=None, universe_key=None, universe=None,
                    label=None, baseline=None) -> dict:
    """Family 계산 입력의 **정본**. 접수(Gate 0)와 실행면이 이 함수를 함께 쓴다.

    양쪽이 각자 dict 를 만들면 기본값이 갈리고, 갈리면 같은 컨셉이 두 개의
    Family 로 쪼개져 **한쪽이 세는 계열에 다른 쪽이 아무것도 각인하지 않는다.**
    실제로 그렇게 됐다(모듈 상단 주석). 만드는 곳을 하나로 둔다.
    """
    return {
        "expected_edge": {"type": edge_type,
                          "universe_key": universe_key,
                          "universe": universe},
        "label": label or DEFAULT_LABEL,
        "baseline": baseline or DEFAULT_BASELINE,
    }


def family_of_hypothesis_row(row: dict) -> str:
    """DB 의 quant.hypotheses 행 -> Family. **접수와 같은 값이 나와야 한다.**

    가설 표에는 label/baseline 컬럼이 없다(확인함) - 그래서 기본값을 쓰는데,
    접수도 같은 기본값을 쓰므로 값이 일치한다. 이 함수를 안 거치고 행을
    family_id() 에 그대로 넣으면 label/baseline 이 빈 문자열이 되어 어긋난다.
    """
    edge = row.get("expected_edge") or {}
    return family_id(hypothesis_view(
        edge_type=edge.get("type"),
        universe_key=edge.get("universe_key"),
        universe=edge.get("universe") or row.get("universe"),
        label=row.get("label") or edge.get("label"),
        baseline=row.get("baseline")))


def family_id(hyp: dict) -> str:
    """Family 식별자. 같은 컨셉이면 파라미터가 달라도 같은 값이다.

    ▶ 2026-08-10: edge_type 도 실행면 통제 어휘(strategy_templates.EDGE_VOCAB)에
      있어야 한다. 이전에는 자유 문자열이라 `none` 같은 자리표시자도 Family 를
      만들었다 - 실행면에 없는 유형으로 Family 가 생기면 그 압력 계수는 무엇도
      세지 않는다.
    """
    k = family_key(hyp)
    if k["edge_type"] and k["edge_type"] not in EDGE_VOCAB:
        return ""
    if not k["edge_type"] or not k["universe_key"]:
        # 유니버스를 통제 어휘로 못 사상하면 Family 를 만들지 않는다 -
        # 서술이 흩어지면 같은 컨셉이 여러 Family 로 갈려 압력이 안 잡히고,
        # 억지로 묶으면 무관한 컨셉이 한 Family 가 된다. 둘 다 나쁘다.
        return ""
    blob = json.dumps(k, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))
    return "fam_" + hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def same_family(a: dict, b: dict) -> bool:
    """두 가설이 같은 컨셉인가. 빈 Family 끼리는 같다고 보지 않는다."""
    fa, fb = family_id(a), family_id(b)
    return bool(fa) and fa == fb


def family_ids_for(hyp: dict) -> tuple[str, ...]:
    """이 개념을 가리키는 **모든** Family ID. 첫 값이 지금의 정본이다.

    ▶ 왜 여럿인가 (2026-08-12 실측)
      계열 ID 를 만드는 길이 둘이다. `family_id(hyp)` 는 `family_key` 를 그대로
      해시하고(label·baseline 이 빈 문자열), `family_of_hypothesis_row(row)` 는
      `hypothesis_view` 로 기본값을 채운 뒤 해시한다. **같은 개념이 두 값을
      갖는다:**

        momentum/krx_all → fam_65a4c7b6c7… (원시)  ·  fam_42663e9f0f… (기본값 채움)

      8월 4일 실험 7건은 앞의 값으로, 오늘 실험은 뒤의 값으로 찍혔다. 그래서
      같은 개념인데 **시도 카운터가 1부터 다시 셌다.** 200번을 돌려도 매번
      "첫 시도" 로 읽히면 DSR 이 감가하지 않고, 200번째의 Sharpe 1.5 를 첫
      시도처럼 채택하게 된다 - 다중검정 방어가 통째로 무력해진다.

      **원장을 고치지 않는다.** 저장된 값은 그 실험이 실제로 찍은 것이라
      사실이고, 소급 수정하면 "시도 압력은 기록된 사실" 이라는 규칙이 깨진다.
      대신 **세는 쪽이 둘 다 인정한다.** 나중에 키 정의가 또 바뀌어도 여기에
      한 줄 늘리면 계보가 이어진다.
    """
    out: list[str] = []
    for fn in (family_of_hypothesis_row, family_id):
        try:
            v = fn(hyp)
        except Exception:      # noqa: BLE001 - 한 방식이 실패해도 나머지는 쓴다
            continue
        if v and v not in out:
            out.append(v)
    return tuple(out)


def pressure(family, cards: list[dict], *, budget: int) -> dict:
    """이 Family 에서 몇 번째 시도인가. **순수 함수.**

    edge type 으로 세던 것을 Family 로 바꾼다 - 컨셉이 다른데 type 이 같으면
    남의 시도가 내 압력으로 잡히던 문제를 없앤다.

    `family` 는 문자열 하나 또는 **동의어 묶음**이다(`family_ids_for`). 묶음이면
    전부 같은 개념으로 세고, **찍는 값은 첫 번째(정본)** 다 - 세는 것은 넓게,
    남기는 것은 하나로.
    """
    if not isinstance(family, str):
        ids = tuple(x for x in (family or ()) if x)
        canonical = ids[0] if ids else ""
    else:
        ids, canonical = ((family,) if family else ()), family
    if canonical:
        same = [c for c in cards if c.get("trial_family_id") in ids]
        used = len(same)
        return {
            "trial_family_id": canonical,
            "trials_used": used,
            "trial_number": used + 1,
            "trial_budget": budget,
            "over_budget": used >= budget,
            **({"counted_aliases": list(ids[1:])} if len(ids) > 1 else {}),
        }
    if not canonical:
        # ▶ **trial_number 를 빠뜨리지 않는다.** 호출부(DSR 감가)가 이 값을
        #   반드시 읽으므로 없으면 KeyError 로 실험 전체가 죽는다.
        #   1 은 "첫 시도" 가 아니라 **못 셌다**는 뜻이고, DSR 이 감가하지
        #   않는다는 한계가 reason 으로 함께 나간다 - 조용히 낙관하지 않는다.
        return {"trial_family_id": "", "trials_used": 0, "trial_number": 1,
                "trial_budget": budget, "over_budget": False,
                "reason": "Family 를 정할 수 없다(edge type 또는 유니버스를 "
                          "통제 어휘로 못 사상) - 압력을 세지 않는다. "
                          "DSR 이 감가되지 않으므로 Sharpe 를 그대로 믿지 않는다"}


# ── 자체 점검 ────────────────────────────────────────────────────────────────

_A = {"expected_edge": {"type": "mean_reversion",
                        "universe": "SMA20 위 상위 50% 종목",
                        "horizon_days": 5, "top_n": 20}}


def _check_tuning_does_not_change_family():
    """**파라미터를 바꿔도 같은 Family 여야 한다.**

    안 그러면 변형마다 새 Family 가 되어 시도 압력이 영원히 1 이고,
    다중검정을 세는 의미가 사라진다.
    """
    b = {"expected_edge": dict(_A["expected_edge"], horizon_days=60, top_n=90)}
    assert same_family(_A, b), (family_id(_A), family_id(b))
    # 유니버스의 숫자만 다른 것도 같은 컨셉의 변형이다
    c = {"expected_edge": dict(_A["expected_edge"],
                               universe="SMA20 위 상위 30% 종목")}
    assert same_family(_A, c), (family_id(_A), family_id(c))


def _check_different_concept_is_different_family():
    """**컨셉이 다르면 다른 Family** - 남의 시도가 내 압력이 되면 안 된다.

    "SMA20 이탈 후 회귀" 와 "거래대금 급감 후 회귀" 는 둘 다 mean_reversion
    이지만 다른 아이디어다. 하나가 예산을 다 써도 다른 하나는 첫 시도다.
    """
    d = {"expected_edge": {"type": "mean_reversion",
                           "universe": "거래대금 급감 종목", "horizon_days": 5}}
    assert not same_family(_A, d), family_id(d)
    # edge type 이 다르면 당연히 다르다
    e = {"expected_edge": dict(_A["expected_edge"], type="momentum")}
    assert not same_family(_A, e)


def _check_synonyms_are_one_family():
    """**같은 뜻을 다르게 쓴 서술이 흩어지면 안 된다** (2026-08-04 실측).

    LLM 이 낸 유니버스 설명은 매번 표현이 다르다. 흩어지면 같은 컨셉이
    여러 Family 로 갈려 시도 압력이 영원히 1 이 된다.
    """
    pairs = [("KRX 전체 시장", "KRX 시장 전 종목"),
             ("SMA20 위에 위치한 종목", "20일 이동평균선 상회 종목")]
    for x, y in pairs:
        a = {"expected_edge": {"type": "mean_reversion", "universe": x}}
        b = {"expected_edge": {"type": "mean_reversion", "universe": y}}
        assert same_family(a, b), (x, y, family_id(a), family_id(b))


def _check_unmappable_universe_makes_no_family():
    """**못 사상하면 Family 를 만들지 않는다.**

    억지로 하나로 묶으면 무관한 컨셉이 한 Family 가 되어 남의 시도가 내
    압력이 된다 - 안 묶는 쪽이 낫다.
    """
    u = {"expected_edge": {"type": "momentum", "universe": "뭔가 새로운 조건"}}
    assert family_id(u) == "", family_id(u)
    # 구조화된 키를 주면 만들어진다 - QNT-01 이 낼 수 있는 탈출구다
    v = {"expected_edge": {"type": "momentum", "universe_key": "krx_all"}}
    assert family_id(v).startswith("fam_"), family_id(v)


def _check_no_edge_type_makes_no_family():
    """빈 값으로 묶으면 무관한 가설이 한 Family 가 된다."""
    assert family_id({"expected_edge": {}}) == ""
    assert family_id({}) == ""
    # 빈 Family 끼리도 같다고 보지 않는다
    assert not same_family({}, {})


def _check_pressure_counts_family_only():
    fam = family_id(_A)
    cards = [{"trial_family_id": fam} for _ in range(4)] + \
            [{"trial_family_id": "fam_other"} for _ in range(9)]
    p = pressure(fam, cards, budget=5)
    assert p["trials_used"] == 4 and p["trial_number"] == 5, p
    assert p["over_budget"] is False, p
    # 예산에 닿으면 초과다 - 5번 쓴 뒤 6번째를 막는다
    p2 = pressure(fam, cards + [{"trial_family_id": fam}], budget=5)
    assert p2["trials_used"] == 5 and p2["over_budget"] is True, p2


def _check_unknown_family_does_not_block():
    """Family 를 못 정하면 **압력을 지어내지 않는다** - 막지도 않는다."""
    p = pressure("", [{"trial_family_id": "x"}] * 99, budget=1)
    assert p["over_budget"] is False and p["trials_used"] == 0, p
    assert "세지 않는다" in p["reason"], p
    # 한계를 숨기지 않는다 - DSR 이 감가 안 된다는 사실이 함께 나간다
    assert "DSR" in p["reason"], p


def _check_pressure_always_has_trial_number():
    """**모든 경로가 trial_number 를 낸다.** 호출부(DSR 감가)가 반드시 읽으므로
    한 경로라도 빠뜨리면 KeyError 로 실험 전체가 죽는다 - 실제로 죽었다."""
    for p in (pressure("", [], budget=5),
              pressure("fam_x", [], budget=5),
              pressure("fam_x", [{"trial_family_id": "fam_x"}] * 9, budget=5)):
        assert isinstance(p.get("trial_number"), int) and p["trial_number"] >= 1, p


def _check_intake_and_runtime_agree():
    """**접수와 실행면이 같은 Family 를 내야 한다** (2026-08-11 실측 회귀).

    이게 깨지면 조용히 망가진다: 실험은 실행면 Family 로 각인되고 Gate 0 은
    접수 Family 로 세므로, 계수가 항상 0 이 되어 시도 예산·DSR 감가·기각 교훈
    대응이 **전부 통과**한다. 아무 에러도 안 난다 - 그래서 여기서 잡는다.
    """
    # 접수: 기획안이 낸 통제 어휘 (label/baseline 은 기획안에 없다)
    intake = hypothesis_view(edge_type="momentum", universe_key="krx_all")
    # 실행면: 그 기획안으로 만든 가설 행 (튜닝값이 edge 에 함께 들어 있다)
    row = {"expected_edge": {"type": "momentum", "universe_key": "krx_all",
                             "horizon_days": 20, "top_n": 20}}
    assert family_id(intake) == family_of_hypothesis_row(row), \
        (family_id(intake), family_of_hypothesis_row(row))
    assert family_id(intake).startswith("fam_")


def _check_alias_keeps_the_lineage():
    """**계열 ID 가 갈려도 시도 카운터가 1로 안 돌아간다.** (2026-08-12 실측)

    momentum/krx_all 이 옛 방식으로 7건 찍혀 있는데 오늘 것이 새 방식으로
    찍혀 `시도1` 이 됐다. 200번을 돌려도 매번 첫 시도로 읽히면 DSR 이
    감가하지 않고, 200번째의 Sharpe 를 첫 시도처럼 채택하게 된다.
    """
    row = {"expected_edge": {"type": "momentum", "universe_key": "krx_all"}}
    ids = family_ids_for(row)
    assert len(ids) == 2, ids
    new, old = ids[0], ids[1]
    cards = [{"trial_family_id": old} for _ in range(7)]
    assert pressure(new, cards, budget=5)["trial_number"] == 1   # 이게 버그였다
    p = pressure(ids, cards, budget=5)
    assert p["trial_number"] == 8 and p["trials_used"] == 7, p
    assert p["over_budget"] is True, p
    assert p["trial_family_id"] == new, p          # 찍는 값은 정본 하나
    assert p["counted_aliases"] == [old], p


def _check_row_without_view_would_have_drifted():
    """정본을 안 거치면 어긋난다는 것 자체를 고정한다 - 왜 이 함수가 있는지의 근거."""
    row = {"expected_edge": {"type": "momentum", "universe_key": "krx_all"}}
    assert family_id(row) != family_of_hypothesis_row(row), \
        "기본값이 사라졌다면 이 회귀 검사를 지워도 된다"


def _check_tuned_fields_never_in_key():
    """**튜닝 값이 Family 재료에 없는지** 구조로 확인한다."""
    k = family_key({"expected_edge": dict(_A["expected_edge"]),
                    "lookback_days": 5, "top_n": 20})
    for f in TUNED_FIELDS:
        assert f not in k, f
    assert set(k) == set(FAMILY_FIELDS), set(k)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_tuning_does_not_change_family();      print("  튜닝 != 새 Family       OK")
    _check_different_concept_is_different_family(); print("  컨셉 다르면 다른 Family OK")
    _check_synonyms_are_one_family();            print("  동의어 -> 한 Family     OK")
    _check_unmappable_universe_makes_no_family(); print("  미사상 -> Family 안 만듦 OK")
    _check_no_edge_type_makes_no_family();       print("  빈 Family 안 만듦       OK")
    _check_pressure_counts_family_only();        print("  Family 단위 계수        OK")
    _check_unknown_family_does_not_block();      print("  미상 Family 차단 안 함  OK")
    _check_pressure_always_has_trial_number();   print("  trial_number 항상 존재  OK")
    _check_tuned_fields_never_in_key();          print("  튜닝값 미포함(구조)     OK")
    _check_intake_and_runtime_agree();           print("  접수==실행면 Family     OK")
    _check_row_without_view_would_have_drifted(); print("  정본 미경유는 어긋남    OK")
    _check_alias_keeps_the_lineage();             print("  동의어 계열 합산        OK")
    print("시도 Family 12개 영역 통과.")
