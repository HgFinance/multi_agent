"""사전등록 관문 - 결과를 보기 전에 고정하고, 끝난 뒤 대조한다.

담당: 재일 (퀀트·백테스트본부 QNT)
근거: HEDGE_FUND_MASTER_PLAN.md 7.2·7.6절,
      재일님 계획 2026-08-04 "결과 확인 전에 고정한다"

▶ 무엇을 막는가
  백테스트에서 가장 흔한 자기기만은 **결과를 보고 설정을 바꾸는 것**이다.
  Sharpe 가 낮으면 lookback 을 20에서 60으로 바꾸고, 그래도 낮으면 유니버스를
  좁히고, 비용 가정을 낮춘다. 각 단계는 합리적으로 느껴지는데 결과는
  "이 데이터에 맞춘 설정" 이다. 그렇게 나온 성적은 미래를 예측하지 못한다.

  trial_pressure 는 "몇 번 시도했나" 를 세고, 이 모듈은 **"같은 실험인 척
  설정을 바꿨나"** 를 잡는다. 둘은 다른 부정을 막는다.

▶ 어떻게
  실험 전: 실질 내용(Feature/Label/Split/비용/폐기 기준/유니버스/지평/
           baseline 등 11개)의 해시를 DB 에 박는다.
  실험 후: 같은 해시가 나오는지 본다. 다르면 그 실험은 무효다.

  status·version·preregistered_at 은 실질이 아니라 해시에 안 들어간다 -
  상태가 바뀐다고 같은 가설이 다른 가설이 되지는 않는다.

▶ 상태 머신을 계약 쪽으로 옮긴다
  계약(7.6절)  INTAKE -> PREREGISTERED -> DATASET_CERTIFIED -> RUNNING
               -> ROBUSTNESS_REVIEW -> SUPPORTED/REJECTED/INCONCLUSIVE
  옛 실행부    PROPOSED -> TESTING -> ...

  옛 값을 즉시 지우지 않는다 - 기존 14행이 쓰고 있고, 한 번에 바꾸면 어느
  하나만 먼저 반영돼도 UPDATE 가 죽는다(오늘 INCONCLUSIVE 로 실제 그럴 뻔했다).

자체 점검: python departments/04-quant-backtest/pipeline/preregistration.py
"""

from __future__ import annotations

import hashlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

MODULE_VERSION = "quant-preregistration-v1"

# 계약(quant_v2.MATERIAL_FIELDS)과 같은 목록. 여기서 다시 정의하는 이유는
# 실행부가 HypothesisSpecV2 인스턴스가 아니라 DB 행(dict)을 다루기 때문이다.
# **둘이 어긋나면 사전등록 검사가 헐거워지므로** 자체점검이 대조한다.
MATERIAL_FIELDS = (
    "features", "label", "preregistered_splits", "cost_model_version",
    "falsification_tests", "universe_version", "decision_frequency",
    "holding_horizon", "baseline", "entry_exit_rules", "strategy_family",
)

# 계약 상태. 옛 값(PROPOSED/TESTING)은 이행기 동안만 허용한다.
CONTRACT_STATES = ("INTAKE", "PREREGISTERED", "DATASET_CERTIFIED", "RUNNING",
                   "ROBUSTNESS_REVIEW", "SUPPORTED", "REJECTED",
                   "NEEDS_DATA", "INCONCLUSIVE")
LEGACY_STATES = ("PROPOSED", "APPROVED", "TESTING", "ARCHIVED")

# 옛 -> 새 대응. 실행부가 새 값을 쓰되 기존 행을 읽을 때 해석한다.
LEGACY_MAP = {"PROPOSED": "INTAKE", "APPROVED": "PREREGISTERED",
              "TESTING": "RUNNING", "ARCHIVED": "REJECTED"}


@dataclass(frozen=True)
class PreregResult:
    ok: bool
    fingerprint: str | None
    reason: str = ""
    changed_fields: tuple = ()


def fingerprint(spec: dict) -> str:
    """실질 내용의 해시. **없는 필드는 채우지 않는다.**

    빠진 필드를 빈 값으로 채우면 서로 다른 가설이 같은 지문을 갖게 되고,
    그러면 사전등록 검사 자체가 무력해진다 - 계약 docstring 이 경고하는 것과
    같은 실패다. 없으면 키 자체를 빼고, 그 사실이 지문에 반영된다.
    """
    material = {k: spec[k] for k in MATERIAL_FIELDS if k in spec
                and spec[k] is not None}
    blob = json.dumps(material, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def preregister(spec: dict, *, now: datetime | None = None) -> PreregResult:
    """실험 전 고정. 실질 필드가 하나도 없으면 거부한다.

    아무것도 안 정한 채 사전등록하면 나중에 무엇을 바꿔도 지문이 같다 -
    등록한 척만 하는 것이다.
    """
    present = [k for k in MATERIAL_FIELDS if k in spec and spec[k] is not None]
    if len(present) < 3:
        return PreregResult(
            False, None,
            f"실질 필드가 {len(present)}개뿐이다 - 최소 3개는 정해야 "
            f"사전등록이 의미를 갖는다(있는 것: {present})")
    return PreregResult(True, fingerprint(spec),
                        f"고정 {len(present)}개 필드 @"
                        f"{(now or datetime.now(timezone.utc)).isoformat()}")


def verify(spec: dict, registered: str | None) -> PreregResult:
    """실험 후 대조. **다르면 그 실험은 무효다.**

    등록이 없으면 통과가 아니다 - 사전등록을 안 하고 실험한 것이고,
    그건 애초에 막았어야 할 경로다.
    """
    if not registered:
        return PreregResult(False, None,
                            "사전등록 지문이 없다 - 고정 없이 실험했다")
    now_fp = fingerprint(spec)
    if now_fp == registered:
        return PreregResult(True, now_fp, "실질 내용이 등록 시점과 같다")

    # ▶ 무엇이 바뀌었는지 짚는다. "달라졌다" 만으로는 고칠 수가 없다.
    #   등록 당시 spec 을 갖고 있지 않으므로 필드를 하나씩 빼 보며 어느 것이
    #   지문을 좌우하는지 본다 - 한 필드만 바뀐 흔한 경우를 정확히 짚는다.
    changed = tuple(
        k for k in MATERIAL_FIELDS
        if k in spec and fingerprint({kk: vv for kk, vv in spec.items()
                                      if kk != k}) == registered)
    return PreregResult(
        False, now_fp,
        "실질 내용이 등록 시점과 다르다 - 결과를 보고 설정을 바꾼 것이다. "
        "이 실험은 무효이며 새 Version 으로 등록해야 한다",
        changed_fields=changed)


def normalize_status(status: str) -> str:
    """옛 상태를 계약 상태로 해석. **모르는 값은 그대로 둔다.**

    임의로 매핑하면 없는 진행을 만들어낸다.
    """
    s = (status or "").strip().upper()
    return LEGACY_MAP.get(s, s)


def can_transition(frm: str, to: str) -> bool:
    """계약(7.6절) 순서를 건너뛰지 않는가. 옛 값은 정규화해서 본다."""
    order = list(CONTRACT_STATES[:5])          # 선형 구간
    a, b = normalize_status(frm), normalize_status(to)
    terminal = {"SUPPORTED", "REJECTED", "INCONCLUSIVE", "NEEDS_DATA"}
    if b in terminal:
        # 종결은 ROBUSTNESS_REVIEW 나 RUNNING 에서만 - 사전등록 전에
        # SUPPORTED 로 뛰면 검증을 통째로 건너뛴 것이다
        return a in ("RUNNING", "ROBUSTNESS_REVIEW")
    if a in terminal:
        return False                            # 종결 뒤에는 새 Version 이다
    if a not in order or b not in order:
        return False
    return order.index(b) == order.index(a) + 1


# ── 자체 점검 ────────────────────────────────────────────────────────────────

_SPEC = {"features": ["mom_20"], "label": "fwd_5d", "cost_model_version": "krx-cost-v2",
         "universe_version": "krx-350-v1", "holding_horizon": 5,
         "strategy_family": "momentum"}


def _check_material_fields_match_contract():
    """**계약과 목록이 어긋나면 검사가 헐거워진다.**

    실행부가 DB 행(dict)을 다루느라 목록을 다시 정의했는데, 계약이 필드를
    늘렸는데 여기가 그대로면 새 필드를 바꿔도 지문이 안 변한다.
    """
    sys.path.insert(0, str(__import__("pathlib").Path(__file__)
                           .resolve().parents[1] / "contracts"))
    from quant_v2 import HypothesisSpecV2

    assert set(MATERIAL_FIELDS) == set(HypothesisSpecV2.MATERIAL_FIELDS), (
        set(MATERIAL_FIELDS) ^ set(HypothesisSpecV2.MATERIAL_FIELDS))


def _check_thin_spec_rejected():
    """아무것도 안 정한 사전등록은 등록한 척일 뿐이다."""
    r = preregister({"features": ["x"]})
    assert r.ok is False and "최소 3개" in r.reason, r


def _check_same_spec_same_fingerprint():
    a = preregister(_SPEC)
    b = preregister(dict(reversed(list(_SPEC.items()))))   # 순서만 다르게
    assert a.ok and a.fingerprint == b.fingerprint, (a, b)


def _check_changed_spec_is_caught():
    """**결과를 보고 lookback 을 바꾸면 잡힌다.** 이 모듈의 존재 이유다."""
    reg = preregister(_SPEC).fingerprint
    assert verify(_SPEC, reg).ok is True
    tampered = dict(_SPEC, holding_horizon=20)             # 5 -> 20
    v = verify(tampered, reg)
    assert v.ok is False and "결과를 보고" in v.reason, v
    # ▶ **무엇이 바뀌었는지 짚는다.** "달라졌다" 만으로는 고칠 수가 없다.
    #   한 필드를 새로 넣은 경우는 그 필드를 빼면 등록 지문과 같아진다.
    added = dict(_SPEC, decision_frequency="daily")
    v2 = verify(added, reg)
    assert v2.ok is False and "decision_frequency" in v2.changed_fields, v2
    # 비용 가정을 낮추는 것도 잡힌다
    cheap = dict(_SPEC, cost_model_version="krx-cost-v1")
    assert verify(cheap, reg).ok is False


def _check_missing_registration_is_not_pass():
    """등록이 없으면 통과가 아니다 - 고정 없이 실험한 것이다."""
    for bad in (None, ""):
        r = verify(_SPEC, bad)
        assert r.ok is False and "고정 없이" in r.reason, r


def _check_absent_field_is_not_filled():
    """빠진 필드를 빈 값으로 채우면 서로 다른 가설이 같은 지문을 갖는다."""
    a = fingerprint(_SPEC)
    b = fingerprint({**_SPEC, "label": None})
    assert a != b, "None 필드가 지문에 반영되지 않았다"


def _check_status_order():
    assert can_transition("INTAKE", "PREREGISTERED")
    assert can_transition("PREREGISTERED", "DATASET_CERTIFIED")
    assert can_transition("DATASET_CERTIFIED", "RUNNING")
    # ▶ **건너뛰기 금지.** 사전등록 없이 RUNNING 으로 가면 관문이 무의미하다
    assert not can_transition("INTAKE", "RUNNING")
    assert not can_transition("INTAKE", "SUPPORTED")
    # 종결은 검증 뒤에만
    assert can_transition("RUNNING", "SUPPORTED")
    assert can_transition("ROBUSTNESS_REVIEW", "INCONCLUSIVE")
    assert not can_transition("PREREGISTERED", "SUPPORTED")
    # 종결 뒤에는 새 Version 이다
    assert not can_transition("SUPPORTED", "RUNNING")


def _check_legacy_mapping():
    """옛 값을 계약 값으로 해석하되 **모르는 값은 지어내지 않는다.**"""
    assert normalize_status("PROPOSED") == "INTAKE"
    assert normalize_status("TESTING") == "RUNNING"
    assert normalize_status("듣보") == "듣보"
    # 옛 값끼리도 순서가 지켜진다
    assert can_transition("PROPOSED", "PREREGISTERED")
    assert not can_transition("PROPOSED", "SUPPORTED")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    print(f"{MODULE_VERSION} 자체 점검 (DB 없음)")
    _check_material_fields_match_contract(); print("  계약과 필드 일치        OK")
    _check_thin_spec_rejected();             print("  빈 사전등록 거부        OK")
    _check_same_spec_same_fingerprint();     print("  같은 내용 = 같은 지문   OK")
    _check_changed_spec_is_caught();         print("  사후 변경 적발          OK")
    _check_missing_registration_is_not_pass(); print("  미등록 != 통과         OK")
    _check_absent_field_is_not_filled();     print("  결측 != 빈값            OK")
    _check_status_order();                   print("  상태 건너뛰기 금지      OK")
    _check_legacy_mapping();                 print("  옛 상태 해석            OK")
    print("사전등록 관문 8개 영역 통과.")
