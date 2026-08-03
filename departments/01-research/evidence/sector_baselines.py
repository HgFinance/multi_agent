"""업종 문맥 - 같은 숫자가 업종마다 다른 뜻이다.

담당: 재일 (리서치본부 RES)
근거: 2026-08-04 실측 (006800 미래에셋증권)

▶ 왜 필요한가 - 실측에서 드러난 오독
  RES-05 가 부채비율 1015.01% 를 냈고, 인사이트가 그것을 "외부 충격에 대한
  완충이 거의 없다" 로 읽었다. **증권사는 고객예탁금·차입이 구조적으로
  부채에 잡혀 레버리지가 원래 높다.** 제조업 기준을 그대로 들이댄 것이다.

  숫자는 맞았고 계산도 맞았다. 틀린 것은 **문맥**이다. 그리고 문맥이 없으면
  분석가도 총괄도 인사이트도 전부 같은 오독을 반복한다 - 재료가 업종을
  안 실으면 그 위에서 무엇을 해도 안 고쳐진다.

▶ 업종을 어떻게 아는가 - DB 에 컬럼이 없다
  instruments 에 sector/industry 컬럼이 없다. 수집기를 새로 만드는 것이
  정공법이지만, **재무제표 계정 구조만으로 금융업은 결정론적으로 판별된다.**

  006800 실측 계정: BS:예수부채, BS:당기손익-공정가치측정금융자산,
  BS:상각후원가측정금융자산 - 전부 금융업 전용이다. 그리고 결정적으로
  **유동자산/유동부채가 없다** - 금융회사는 유동성 배열법을 쓰지 않는다.

  이건 임시방편이 아니라 **이미 가진 데이터로 답할 수 있는 것을 먼저
  답하는 것**이다. 업종 코드가 생기면 그것을 우선하도록 바꾸면 된다.

▶ 없는 기준선을 지어내지 않는다
  금융업의 "적정 부채비율" 을 숫자로 정하지 않는다. 증권사·은행·보험이
  각각 다르고, 우리에게 그 기준을 세울 근거가 없다. **판정을 하지 않고
  판정 불가라고 말한다** - 아무 숫자나 넣으면 그 숫자가 근거처럼 인용된다.

  Altman Z 도 마찬가지다. 원 모형은 제조업 표본으로 만들어졌고 금융회사에
  적용하면 의미가 없다. 계산해서 내놓는 대신 **적용 불가로 표시한다.**

자체 점검: python departments/01-research/evidence/sector_baselines.py
"""

from __future__ import annotations

import sys
from typing import Iterable, Optional

MODULE_VERSION = "research-sector-baselines-v1"

SECTOR_FINANCIAL = "FINANCIAL"      # 증권·은행·보험·여신
SECTOR_GENERAL = "GENERAL"          # 그 외 (제조·유통·서비스)
SECTOR_UNKNOWN = "UNKNOWN"          # 계정이 부족해 판별 불가

# 금융업 전용 계정. 하나만 있어도 금융업 신호다 - 일반 기업 재무제표에
# 예수부채나 보험계약부채가 잡히는 일은 없다.
_FINANCIAL_MARKERS = (
    "예수부채", "보험계약부채", "예치금", "예수금",
    "공정가치측정금융자산", "상각후원가측정금융자산",
    "대출채권", "신용공여금", "고객예탁금",
)

# 유동성 배열법의 흔적. 금융회사는 이 구분을 쓰지 않는다.
_CURRENT_MARKERS = ("유동자산", "유동부채")


def detect_sector(account_codes: Iterable[str]) -> tuple[str, str]:
    """계정 목록 -> (업종, 판별 근거). **순수 함수, 결정론.**

    반환의 두 번째 값은 사람이 읽는 근거다 - 판별 결과만 주면 왜 그렇게
    분류됐는지 확인할 수 없고, 확인 못 하는 분류는 신뢰할 수 없다.
    """
    codes = [str(c) for c in (account_codes or []) if c]
    if not codes:
        return SECTOR_UNKNOWN, "계정이 없다"

    hits = sorted({m for m in _FINANCIAL_MARKERS
                   if any(m in c for c in codes)})
    has_current = any(any(m in c for c in codes) for m in _CURRENT_MARKERS)

    if hits:
        why = f"금융업 전용 계정 {', '.join(hits[:3])}"
        if not has_current:
            why += " · 유동/비유동 구분 없음(유동성 배열법 미적용)"
        return SECTOR_FINANCIAL, why
    if has_current:
        return SECTOR_GENERAL, "유동자산·유동부채 구분이 있다(유동성 배열법)"
    # 마커도 없고 유동 구분도 없다 - 계정이 얇은 것이지 금융업이 아니다.
    # 여기서 GENERAL 로 단정하면 조용히 틀린다.
    return SECTOR_UNKNOWN, f"판별 계정이 없다(계정 {len(codes)}종)"


# 업종별 지표 적용 가능 여부와 문맥.
# **금융업의 '적정 부채비율' 을 숫자로 정하지 않는다** - 증권·은행·보험이
# 각각 다르고 우리에게 그 기준을 세울 근거가 없다. 아무 숫자나 넣으면
# 그 숫자가 근거처럼 인용된다.
_CONTEXT: dict[str, dict] = {
    SECTOR_FINANCIAL: {
        "부채비율_pct": {
            "applicable": False,
            "note": "금융업은 고객예탁금·차입이 구조적으로 부채에 잡혀 "
                    "레버리지가 원래 높다. 제조업 기준(통상 200% 내외)으로 "
                    "읽으면 안 된다 - 이 값만으로 재무 위험을 판정하지 않는다.",
        },
        "altman_z": {
            "applicable": False,
            "note": "Altman Z 는 제조업 표본으로 만들어진 모형이다. "
                    "금융회사에 적용하면 의미가 없다.",
        },
        "f_score": {
            "applicable": True,
            "note": "레버리지 신호(LEVERAGE_DOWN)는 금융업에서 뜻이 다르다 - "
                    "부채 감소가 곧 개선이 아니라 영업 축소일 수 있다.",
        },
    },
    SECTOR_GENERAL: {
        "부채비율_pct": {
            "applicable": True,
            # 통상 기준선. **판정선이 아니라 참고선**이다 - 업종·성장단계에
            # 따라 다르고, 이 숫자로 자동 판정하지 않는다.
            "reference_pct": 200.0,
            "note": "제조·유통 통상 참고선 200% 내외. 판정선이 아니다.",
        },
        "altman_z": {"applicable": True,
                     "note": "원 모형의 적용 대상이다. x4 는 장부가 대용이다."},
        "f_score": {"applicable": True, "note": ""},
    },
}


def context_for(sector: str, metric: str) -> dict:
    """업종·지표 -> 적용 가능 여부와 문맥.

    UNKNOWN 이면 **적용 가능으로 위장하지 않는다.** 모르면 모른다고 하고,
    호출부가 그 사실을 서술에 싣게 한다.
    """
    if sector == SECTOR_UNKNOWN:
        return {"applicable": None,
                "note": "업종을 판별하지 못해 이 지표의 해석 기준을 정할 수 없다"}
    return dict(_CONTEXT.get(sector, {}).get(metric)
                or {"applicable": True, "note": ""})


def annotate(fields: dict, sector: str, why: str) -> dict:
    """readout 의 fields 에 업종 문맥을 붙인다.

    **값을 지우지 않는다.** 부채비율 1015% 는 사실이고 지우면 정보가 준다.
    대신 applicable=False 와 사유를 함께 실어 **읽는 쪽이 오독하지 않게**
    한다 - 값을 감추는 것과 문맥을 주는 것은 다르다.
    """
    out = dict(fields)
    out["_sector"] = {"sector": sector, "detected_by": why}
    for metric in ("부채비율_pct", "altman_z", "f_score"):
        if metric not in out or not isinstance(out[metric], dict):
            continue
        ctx = context_for(sector, metric)
        entry = dict(out[metric])
        entry["sector_applicable"] = ctx.get("applicable")
        if ctx.get("note"):
            entry["sector_note"] = ctx["note"]
        if ctx.get("reference_pct") is not None:
            entry["sector_reference_pct"] = ctx["reference_pct"]
        out[metric] = entry
    return out


def cautions_for(sector: str, why: str) -> list[str]:
    """업종 때문에 서술이 조심해야 할 것. cautions 에 실린다."""
    if sector == SECTOR_FINANCIAL:
        return [f"업종 판별: 금융업({why}). 부채비율·Altman Z 는 이 업종에 "
                f"적용되지 않으므로 재무 위험 근거로 쓰지 않는다"]
    if sector == SECTOR_UNKNOWN:
        return [f"업종을 판별하지 못했다({why}) - 부채비율·Altman Z 의 해석 "
                f"기준이 없으므로 단독 근거로 쓰지 않는다"]
    return []


# ── 자체 점검 ────────────────────────────────────────────────────────────────

# 006800 미래에셋증권 실측 계정(2026-08-04)에서 발췌
_SECURITIES = ["BS:예수부채", "BS:당기손익-공정가치측정금융자산",
               "BS:상각후원가측정금융자산", "BS:자산총계", "BS:부채총계",
               "BS:자본총계", "BS:이익잉여금", "BS:자본금"]
_MANUFACTURER = ["BS:유동자산", "BS:유동부채", "BS:비유동부채", "BS:자산총계",
                 "BS:부채총계", "BS:자본총계", "IS:매출액", "IS:영업이익"]


def _check_detects_securities_firm():
    """실측 계정으로 증권사를 잡는가. 못 잡으면 이 모듈은 무의미하다."""
    sector, why = detect_sector(_SECURITIES)
    assert sector == SECTOR_FINANCIAL, (sector, why)
    assert "예수부채" in why, why
    # 유동 구분이 없다는 것도 근거로 남아야 한다
    assert "유동성 배열법" in why, why


def _check_detects_manufacturer():
    sector, why = detect_sector(_MANUFACTURER)
    assert sector == SECTOR_GENERAL, (sector, why)


def _check_unknown_is_not_general():
    """판별 못 한 것을 일반업으로 단정하면 조용히 틀린다."""
    for thin in ([], ["BS:자산총계"], ["IS:매출액", "BS:부채총계"]):
        sector, why = detect_sector(thin)
        assert sector == SECTOR_UNKNOWN, (thin, sector)
    # UNKNOWN 이면 적용 가능으로 위장하지 않는다
    ctx = context_for(SECTOR_UNKNOWN, "부채비율_pct")
    assert ctx["applicable"] is None, ctx


def _check_financial_metrics_marked_inapplicable():
    """부채비율·Altman Z 가 금융업에서 적용 불가로 표시되는가.

    이것이 이 모듈의 존재 이유다 - 실측에서 부채비율 1015% 가 "완충이 거의
    없다" 로 읽혔고, 그건 증권사에서 정상 구조다.
    """
    for m in ("부채비율_pct", "altman_z"):
        assert context_for(SECTOR_FINANCIAL, m)["applicable"] is False, m
        assert context_for(SECTOR_GENERAL, m)["applicable"] is True, m
    # F-Score 는 쓰되 레버리지 신호의 뜻이 다르다는 것을 남긴다
    f = context_for(SECTOR_FINANCIAL, "f_score")
    assert f["applicable"] is True and "LEVERAGE_DOWN" in f["note"], f


def _check_no_invented_threshold():
    """금융업의 '적정 부채비율' 을 지어내지 않는다.

    아무 숫자나 넣으면 그 숫자가 근거처럼 인용된다. 우리에게 증권·은행·보험별
    기준을 세울 근거가 없다.
    """
    fin = context_for(SECTOR_FINANCIAL, "부채비율_pct")
    assert "reference_pct" not in fin, f"없는 기준선을 만들었다: {fin}"
    # 일반업 참고선은 있되 '판정선이 아니다' 를 명시한다
    gen = context_for(SECTOR_GENERAL, "부채비율_pct")
    assert gen["reference_pct"] == 200.0
    assert "판정선이 아니다" in gen["note"], gen


def _check_annotate_keeps_value():
    """**값을 지우지 않는다.** 부채비율 1015% 는 사실이고 지우면 정보가 준다."""
    fields = {"부채비율_pct": {"value": 1015.01, "period_end": "2025-12-31"},
              "altman_z": {"value": 2.1},
              "f_score": {"value": 4, "available": 6},
              "매출액": {"value": 1.0}}
    out = annotate(fields, SECTOR_FINANCIAL, "예수부채")
    assert out["부채비율_pct"]["value"] == 1015.01, "값이 사라졌다"
    assert out["부채비율_pct"]["sector_applicable"] is False
    assert "구조적으로" in out["부채비율_pct"]["sector_note"]
    assert out["altman_z"]["sector_applicable"] is False
    assert out["f_score"]["sector_applicable"] is True
    # 업종과 무관한 필드는 안 건드린다
    assert out["매출액"] == {"value": 1.0}
    assert out["_sector"]["sector"] == SECTOR_FINANCIAL


def _check_cautions():
    c = cautions_for(SECTOR_FINANCIAL, "예수부채")
    assert c and "재무 위험 근거로 쓰지 않는다" in c[0], c
    assert cautions_for(SECTOR_GENERAL, "x") == []
    assert cautions_for(SECTOR_UNKNOWN, "얇음"), "미판별인데 경고가 없다"


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_detects_securities_firm();          print("  증권사 판별(실측 계정)   OK")
    _check_detects_manufacturer();             print("  일반업 판별             OK")
    _check_unknown_is_not_general();           print("  미판별 != 일반업        OK")
    _check_financial_metrics_marked_inapplicable(); print("  금융업 적용불가 표시     OK")
    _check_no_invented_threshold();            print("  기준선 날조 안 함        OK")
    _check_annotate_keeps_value();             print("  값 보존(문맥만 추가)     OK")
    _check_cautions();                         print("  업종 경고               OK")
    print("업종 문맥 7개 영역 통과.")
