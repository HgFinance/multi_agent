#!/usr/bin/env python3
"""일별 라벨 스냅샷 - 레짐·지정학 판정을 그날의 사실로 남긴다.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-02 "발생한 모든 문제들 해결해놔".
      packet_outcome_scorer 가 매번 "라벨 주장은 일별 라벨 이력 미보관으로
      채점 제외"를 남기고 있었다 - 선순환의 구멍을 메운다.
      마이그레이션 20260802001700 (research.daily_labels)

▶ 왜 LLM 이 필요 없나
  레짐 라벨(RISK_ON/BREADTH_THRUST/...)과 지정학 라벨(CALM/.../SHOCK)은
  **전부 코드가 결정론으로 정한다** - compute_regime_readout,
  compute_geo_readout. LLM 은 그 결과를 서술만 한다. 그래서 이 수집기는
  분석가를 부르지 않고 계산 함수만 호출한다: 빠르고, 싸고, 재현된다.

▶ 사후 소급 금지
  as_of 는 **계산한 날**이다. 과거 날짜 행을 지금 만들어 채우지 않는다 -
  그건 그때의 판정이 아니라 지금의 재구성이고, 그걸 섞으면 사후 채점이
  거짓말을 하게 된다. 이력은 오늘부터 쌓인다.

▶ 부분 실패 허용
  레짐과 지정학은 서로 독립이다. 하나가 실패해도 다른 하나는 남긴다 -
  둘 다 실패해야 exit 1.

사용
  python collectors/label_snapshot_collector.py            # 자체 점검
  python collectors/label_snapshot_collector.py --collect  # 오늘 라벨 적재
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_BASE / "collectors"))
sys.path.insert(0, str(_BASE / "agents"))

COLLECTOR_VERSION = "research-label-snapshot-v1"
KST = timezone(timedelta(hours=9))
EXIT_SKIP = 2

REGIME = "REGIME"
GEOPOLITICAL = "GEOPOLITICAL"
# 판정 불가는 라벨이 아니다 - 남기면 "그날 CALM 이었다"와 구분이 안 된다
NOT_A_LABEL = ("INSUFFICIENT_DATA", None, "")


def is_recordable(label: str | None) -> bool:
    """기록할 값인가. 판정 불가·빈 값은 기록하지 않는다."""
    return label not in NOT_A_LABEL


def numeric_readout(readout: dict | None, *, limit: int = 14) -> dict:
    """재검산용 근거만 추린다 - 전체 readout 을 넣으면 행이 비대해진다.

    수치와 라벨 근거만 남기고 중첩(themes 등)은 뺀다. bool 은 int 서브클래스라
    명시적으로 제외한다(walk_forward 에서 같은 함정을 겪었다).
    """
    if not readout:
        return {}
    out = {k: v for k, v in readout.items()
           if isinstance(v, (int, float)) and not isinstance(v, bool)}
    for k in ("label_reason", "regime_label", "risk_label", "driver",
              "latest_gpr_date", "latest_trade_date"):
        if readout.get(k) is not None:
            out[k] = readout[k]
    return dict(list(out.items())[:limit])


def snapshot_regime(market_api: str | None = None) -> dict | None:
    """레짐 라벨. 분석가의 LLM 서술 단계는 부르지 않는다."""
    from sector_regime_analyst import (
        MARKET_API,
        REGIME_DAYS,
        _http_get,
        compute_regime_readout,
    )

    base = (market_api or MARKET_API).rstrip("/")
    rows = _http_get(f"{base}/regime/daily?days={REGIME_DAYS}")
    readout = compute_regime_readout(rows, None)
    label = readout.get("regime_label")
    if not is_recordable(label):
        return None
    return {"label_kind": REGIME, "label_value": label, "driver": None,
            "readout": numeric_readout(readout),
            "producer": "sector_regime_analyst.compute_regime_readout"}


def snapshot_geopolitical(research_api: str | None = None) -> dict | None:
    """지정학 라벨 + driver(위협 주도 / 실제 사건 주도)."""
    import urllib.parse

    from geopolitical_analyst import (
        GPR_CODES,
        RESEARCH_API,
        WINDOW_DAYS,
        _http_get,
        _theme_codes,
        compute_geo_readout,
    )

    base = (research_api or RESEARCH_API).rstrip("/")
    codes = list(GPR_CODES) + _theme_codes()
    url = (f"{base}/macro/observations?codes={urllib.parse.quote(','.join(codes))}"
           f"&days={WINDOW_DAYS}")
    readout = compute_geo_readout(_http_get(url), as_of=datetime.now(KST).date())
    label = readout.get("risk_label")
    if not is_recordable(label):
        return None
    return {"label_kind": GEOPOLITICAL, "label_value": label,
            "driver": readout.get("driver"),
            "readout": numeric_readout(readout),
            "producer": "geopolitical_analyst.compute_geo_readout"}


def collect() -> int:
    import psycopg2
    from source_registry import load_project_env

    as_of = datetime.now(KST).date()
    env = load_project_env()
    rows, failures = [], []
    for name, fn in (("regime", snapshot_regime),
                     ("geopolitical", snapshot_geopolitical)):
        try:
            r = fn()
        except Exception as e:  # noqa: BLE001 - 하나가 죽어도 다른 하나는 남긴다
            failures.append(f"{name}: {type(e).__name__} {str(e)[:110]}")
            continue
        if r is None:
            failures.append(f"{name}: 판정 불가(INSUFFICIENT_DATA) - 기록하지 않는다")
            continue
        rows.append(r)

    if not rows:
        print(f"{COLLECTOR_VERSION}: 기록할 라벨 없음 - " + "; ".join(failures),
              flush=True)
        return 1

    conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    try:
        with conn.cursor() as cur:
            for r in rows:
                cur.execute("""
                    insert into research.daily_labels
                      (as_of, label_kind, label_value, driver, readout, producer)
                    values (%s, %s, %s, %s, %s::jsonb, %s)
                    on conflict (as_of, label_kind) do update set
                      label_value = excluded.label_value,
                      driver = excluded.driver,
                      readout = excluded.readout,
                      computed_at = now()
                """, (as_of, r["label_kind"], r["label_value"], r.get("driver"),
                      json.dumps(r["readout"], ensure_ascii=False, default=str),
                      r["producer"]))
        conn.commit()
    finally:
        conn.close()

    desc = ", ".join(f"{r['label_kind']}={r['label_value']}"
                     + (f"/{r['driver']}" if r.get("driver") else "") for r in rows)
    print(f"{COLLECTOR_VERSION}: {as_of} {desc}", flush=True)
    for f in failures:
        print(f"  ⚠ {f}", flush=True)
    return 1 if failures else 0


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _check_recordable():
    """판정 불가를 라벨로 남기면 'CALM 이었다'와 구분이 안 된다."""
    assert is_recordable("BREADTH_THRUST") and is_recordable("SHOCK")
    for bad in ("INSUFFICIENT_DATA", None, ""):
        assert not is_recordable(bad), bad
    print("  기록 가능 판정           OK")


def _check_readout_trim():
    readout = {"regime_label": "RISK_ON", "ad_ratio": 1.4, "days_used": 40,
               "above_sma20_pct": 68.0, "partial": True,      # bool 제외
               "themes": {"X": {"shock_ratio": 3.5}},          # 중첩 제외
               "label_reason": "테마 배율 3.5 >= 2.5", "regime_rules": {"a": 1}}
    out = numeric_readout(readout)
    assert out["ad_ratio"] == 1.4 and out["above_sma20_pct"] == 68.0
    assert out["regime_label"] == "RISK_ON" and "label_reason" in out
    assert "partial" not in out, "bool 이 수치로 새어 들어갔다"
    assert "themes" not in out and "regime_rules" not in out, "중첩이 들어갔다"
    assert numeric_readout(None) == {} and numeric_readout({}) == {}
    # 상한을 넘지 않는다 - 행이 비대해지면 기록층이 무거워진다
    big = {f"k{i}": float(i) for i in range(50)}
    assert len(numeric_readout(big)) <= 14
    print("  근거 추리기              OK")


def _check_no_backfill_semantics():
    """as_of 는 계산한 날이다 - 과거를 소급해 만들지 않는다는 계약 확인.

    (문자열 검사로 하면 검사문 자체가 걸린다 - 서명으로 확인한다.)
    """
    import inspect

    params = inspect.signature(collect).parameters
    assert not params, \
        f"collect() 가 인자를 받는다({list(params)}) - as_of 를 밖에서 주면 " \
        f"과거 날짜로 소급 기록이 가능해진다"
    src = inspect.getsource(collect)
    assert "datetime.now(KST).date()" in src, "as_of 가 '오늘'이 아니게 바뀌었다"
    print("  소급 기록 금지 계약      OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--collect" in sys.argv:
        raise SystemExit(collect())

    print(f"{COLLECTOR_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_recordable()
    _check_readout_trim()
    _check_no_backfill_semantics()
    print("라벨 스냅샷 3개 영역 통과. 적재는 --collect")
