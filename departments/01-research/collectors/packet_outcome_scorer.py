#!/usr/bin/env python3
"""Research Packet 사후 채점 - 선순환의 되먹임 단계.

소유: 재일 (리서치본부)
근거: 재일님 지시 2026-08-01 "output 이 풍성하면서 실제 투자 판단 -> 결과에
      영향을 줄 때 선순환이 이뤄지게 만들어야 한다".
      마이그레이션 20260802001400 (packet_claims / packet_outcomes /
      analyst_calibration), scripts.py build_packet_claims

▶ 이 파일이 닫는 고리
    Packet 발행 -> 코드가 반증 가능한 주장 발행(packet_claims)
      -> [여기] 지평이 지나면 시세로 대조 -> packet_outcomes
      -> research.analyst_calibration = "이 분석가가 X 라고 했을 때 실제로
         어떻게 됐나" 누적 -> 다음 Packet 의 가중치·개선 근거

▶ 규율
  - **채점기는 판단하지 않는다.** 시세를 가져와 부등식을 계산할 뿐이다.
  - 시세가 없으면 PENDING_DATA 로 남긴다. 0 이나 추정으로 채우면 통계 전체가
    오염된다 - 다음 실행이 다시 시도한다(멱등: unique(trace_id, horizon)).
  - 가격 주장은 **터치 판정**이다(지평 안 어느 날이든 임계 도달이면 발동).
    종가 하나만 보면 "중간에 -15% 갔다가 돌아온" 사건을 놓친다.
  - 라벨 주장(REGIME_FLIP·GEO_ESCALATION)은 v1 에서 채점하지 않는다 - 일별
    레짐·지정학 **라벨 이력을 보관하지 않기 때문**이다. 없는 이력을 지금
    다시 계산해 채우면 그건 사후 재구성이지 그때의 판정이 아니다. note 에
    제외 사유와 건수를 남긴다(조용한 절단 금지). 백로그: 라벨 일별 적재.

사용
  python collectors/packet_outcome_scorer.py            # 자체 점검
  python collectors/packet_outcome_scorer.py --score    # 채점 1회
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

SCORER_VERSION = "research-packet-outcome-scorer-v1"
KST = timezone(timedelta(hours=9))
MARKET_API_DEFAULT = "http://127.0.0.1:8036"
PRICE_KINDS = ("PRICE_DRAWDOWN", "PRICE_RALLY")
LABEL_KINDS = ("REGIME_FLIP", "GEO_ESCALATION")
BAR_FETCH_LIMIT = 400


def _http_get(url: str, timeout: int = 25):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read())


# ---------------------------------------------------------------------------
# 순수 함수 - 채점 규칙
# ---------------------------------------------------------------------------

def closes_after(bars: list[dict], packet_date: date) -> list[tuple[date, float]]:
    """packet_date **다음** 거래일부터의 (일자, 종가) 오름차순.

    당일은 뺀다 - Packet 은 그날 종가를 이미 보고 썼으므로 당일을 성과에
    넣으면 자기 정보로 자기를 채점하는 꼴이 된다.
    """
    out: list[tuple[date, float]] = []
    for b in bars or []:
        raw = str(b.get("bucket_time") or "")
        try:
            d = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(KST).date()
            c = float(b.get("close"))
        except (ValueError, TypeError):
            continue
        if d > packet_date:
            out.append((d, c))
    out.sort(key=lambda t: t[0])
    return out


def evaluate_price_claim(claim: dict, series: list[tuple[date, float]]) -> dict:
    """지평 안에서 임계에 **닿았는지**(터치) 판정."""
    horizon = int(claim["horizon_days"])
    window = series[:horizon]
    op, thr = claim["op"], float(claim["threshold"])
    hit_on = None
    for d, c in window:
        if (op == "<=" and c <= thr) or (op == ">=" and c >= thr):
            hit_on = d
            break
    return {"kind": claim["kind"], "metric": claim["metric"], "op": op,
            "threshold": thr, "horizon_days": horizon,
            "triggered": hit_on is not None,
            "triggered_on": hit_on.isoformat() if hit_on else None,
            "days_observed": len(window)}


def forward_return(series: list[tuple[date, float]], baseline: float,
                   horizon: int) -> tuple[float | None, float | None, int]:
    """(수익률%, 지평 종가, 사용 거래일). 거래일이 모자라면 (None, None, n)."""
    window = series[:horizon]
    if not baseline or baseline <= 0 or len(window) < horizon:
        return None, (window[-1][1] if window else None), len(window)
    last = window[-1][1]
    return round((last / baseline - 1) * 100, 4), last, len(window)


def score_packet(claims: list[dict], series: list[tuple[date, float]],
                 horizon: int) -> dict:
    """한 (packet, horizon) 채점 결과. 시세 부족은 PENDING_DATA."""
    at_h = [c for c in claims if int(c["horizon_days"]) == horizon]
    price = [c for c in at_h if c["kind"] in PRICE_KINDS]
    labels = [c for c in at_h if c["kind"] in LABEL_KINDS]
    baseline = next((float(c["baseline"]) for c in price
                     if c.get("baseline") is not None), None)

    ret, close_h, used = forward_return(series, baseline or 0.0, horizon)
    results = [evaluate_price_claim(c, series) for c in price]
    triggered = [r for r in results if r["triggered"]]

    note_bits = []
    if labels:
        note_bits.append(f"라벨 주장 {len(labels)}건은 일별 라벨 이력 미보관으로 "
                         f"채점 제외(백로그)")
    status = "EVALUATED" if used >= horizon and ret is not None else "PENDING_DATA"
    if status == "PENDING_DATA":
        note_bits.append(f"거래일 {used}/{horizon} - 시세 부족")

    return {"horizon_days": horizon, "status": status,
            "baseline_close": baseline, "horizon_close": close_h,
            "forward_return_pct": ret, "trading_days_used": used,
            "claims_total": len(at_h), "claims_triggered": len(triggered),
            "triggered": triggered, "note": "; ".join(note_bits) or None}


# ---------------------------------------------------------------------------
# 실행
# ---------------------------------------------------------------------------

def score(*, market_api: str | None = None, get=_http_get, limit: int = 200) -> int:
    import psycopg2

    from source_registry import load_project_env

    base = (market_api or MARKET_API_DEFAULT).rstrip("/")
    env = load_project_env()
    conn = psycopg2.connect(env["DATABASE_URL"], connect_timeout=20)
    try:
        with conn.cursor() as cur:
            # 아직 채점 안 된 (trace, horizon) 만. 지평이 안 지난 것은 시세가
            # 모자라 어차피 PENDING 이 되므로 여기서 미리 거른다(달력일 여유
            # 1.6배 - 주말·휴장을 감안한 하한이며 정확 판정은 채점기가 한다).
            cur.execute("""
                select c.trace_id, c.symbol, c.horizon_days,
                       min(c.created_at) as created_at
                from research.packet_claims c
                left join research.packet_outcomes o
                  on o.trace_id = c.trace_id and o.horizon_days = c.horizon_days
                 and o.status = 'EVALUATED'
                where o.outcome_id is null
                group by c.trace_id, c.symbol, c.horizon_days
                having min(c.created_at) <= now() - make_interval(
                         days => (c.horizon_days * 1.6)::int)
                order by created_at
                limit %s
            """, (limit,))
            jobs = cur.fetchall()

        if not jobs:
            print(f"{SCORER_VERSION}: 채점 대상 없음 (지평 도달분 없음)", flush=True)
            return 0

        bars_cache: dict[str, list[dict]] = {}
        done = pending = failed = 0
        for trace_id, symbol, horizon, created_at in jobs:
            packet_date = created_at.astimezone(KST).date()
            if symbol not in bars_cache:
                try:
                    bars_cache[symbol] = get(
                        f"{base}/bars/{symbol}?interval=1D&limit={BAR_FETCH_LIMIT}")
                except Exception as e:  # noqa: BLE001
                    print(f"  ⚠ {symbol} 시세 조회 실패: {type(e).__name__} {e}",
                          flush=True)
                    bars_cache[symbol] = []
                    failed += 1
            series = closes_after(bars_cache[symbol], packet_date)

            with conn.cursor() as cur:
                cur.execute("""
                    select kind, metric, op, threshold, threshold_text,
                           baseline, baseline_text, horizon_days
                    from research.packet_claims
                    where trace_id = %s and horizon_days = %s
                """, (trace_id, horizon))
                claims = [{"kind": r[0], "metric": r[1], "op": r[2],
                           "threshold": r[3], "threshold_text": r[4],
                           "baseline": r[5], "baseline_text": r[6],
                           "horizon_days": r[7]} for r in cur.fetchall()]
                cur.execute("""
                    select metadata, evidence_quality
                    from research.pipeline_runs where trace_id = %s limit 1
                """, (trace_id,))
                row = cur.fetchone()

            res = score_packet(claims, series, int(horizon))
            # pipeline_runs.metadata 의 판정 스냅샷. 없으면 빈 dict 로 둔다 -
            # 없는 판정을 지어내면 되먹임 통계가 통째로 거짓이 된다.
            verdicts = ((row[0] or {}).get("analyst_verdicts") or {}) if row else {}

            with conn.cursor() as cur:
                cur.execute("""
                    insert into research.packet_outcomes
                      (trace_id, symbol, packet_date, horizon_days,
                       baseline_close, horizon_close, forward_return_pct,
                       trading_days_used, claims_total, claims_triggered,
                       triggered, analyst_verdicts, status, note)
                    values (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s::jsonb,%s,%s)
                    on conflict (trace_id, horizon_days) do update set
                      evaluated_at = now(),
                      baseline_close = excluded.baseline_close,
                      horizon_close = excluded.horizon_close,
                      forward_return_pct = excluded.forward_return_pct,
                      trading_days_used = excluded.trading_days_used,
                      claims_total = excluded.claims_total,
                      claims_triggered = excluded.claims_triggered,
                      triggered = excluded.triggered,
                      analyst_verdicts = excluded.analyst_verdicts,
                      status = excluded.status, note = excluded.note
                """, (trace_id, symbol, packet_date, horizon,
                      res["baseline_close"], res["horizon_close"],
                      res["forward_return_pct"], res["trading_days_used"],
                      res["claims_total"], res["claims_triggered"],
                      json.dumps(res["triggered"], ensure_ascii=False, default=str),
                      json.dumps(verdicts, ensure_ascii=False),
                      res["status"], res["note"]))
            conn.commit()
            if res["status"] == "EVALUATED":
                done += 1
                print(f"  {symbol} {packet_date} +{horizon}일: "
                      f"{res['forward_return_pct']:+.2f}% / 주장 "
                      f"{res['claims_triggered']}/{res['claims_total']} 발동",
                      flush=True)
            else:
                pending += 1

        print(f"{SCORER_VERSION}: 채점 {done} / 보류 {pending} / 시세실패 {failed}",
              flush=True)
        return 1 if failed else 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 자체 점검 - 네트워크·DB 없음
# ---------------------------------------------------------------------------

def _bars(closes: list[float], *, start: date) -> list[dict]:
    return [{"bucket_time": datetime(d.year, d.month, d.day, 15, 30,
                                     tzinfo=KST).isoformat(), "close": c}
            for c, d in ((c, start + timedelta(days=i))
                         for i, c in enumerate(closes))]


def _check_same_day_excluded():
    p = date(2026, 8, 1)
    bars = _bars([100.0, 101.0, 102.0], start=p)      # 첫 봉이 Packet 당일
    s = closes_after(bars, p)
    assert [d for d, _ in s] == [date(2026, 8, 2), date(2026, 8, 3)], s
    assert closes_after([], p) == []
    # 잡값은 버린다
    assert closes_after([{"bucket_time": "쓰레기", "close": 1}], p) == []
    print("  당일 제외·잡값 방어      OK")


def _check_touch_semantics():
    """중간에 닿았다가 돌아온 경우도 발동이다 - 종가만 보면 놓친다."""
    p = date(2026, 8, 1)
    s = closes_after(_bars([100.0, 88.0, 101.0], start=p + timedelta(days=1)), p)
    claim = {"kind": "PRICE_DRAWDOWN", "metric": "close", "op": "<=",
             "threshold": 90.0, "baseline": 100.0, "horizon_days": 3}
    r = evaluate_price_claim(claim, s)
    assert r["triggered"] and r["triggered_on"] == "2026-08-03", r
    # 지평 밖에서 닿은 것은 발동 아님
    claim2 = dict(claim, horizon_days=1)
    assert not evaluate_price_claim(claim2, s)["triggered"]
    print("  터치 판정·지평 경계      OK")


def _check_pending_when_short():
    """거래일이 모자라면 추정하지 않고 PENDING_DATA."""
    p = date(2026, 8, 1)
    s = closes_after(_bars([100.0, 105.0], start=p + timedelta(days=1)), p)
    claims = [{"kind": "PRICE_RALLY", "metric": "close", "op": ">=",
               "threshold": 110.0, "baseline": 100.0, "horizon_days": 20}]
    r = score_packet(claims, s, 20)
    assert r["status"] == "PENDING_DATA" and r["forward_return_pct"] is None, r
    assert "시세 부족" in r["note"]
    print("  시세 부족 = 보류         OK")


def _check_forward_return_and_labels():
    p = date(2026, 8, 1)
    s = closes_after(_bars([100.0, 110.0, 121.0], start=p + timedelta(days=1)), p)
    claims = [
        {"kind": "PRICE_RALLY", "metric": "close", "op": ">=", "threshold": 110.0,
         "baseline": 100.0, "horizon_days": 3},
        {"kind": "PRICE_DRAWDOWN", "metric": "close", "op": "<=", "threshold": 90.0,
         "baseline": 100.0, "horizon_days": 3},
        {"kind": "REGIME_FLIP", "metric": "regime_label", "op": "!=",
         "threshold_text": "RISK_ON", "horizon_days": 3},
    ]
    r = score_packet(claims, s, 3)
    assert r["status"] == "EVALUATED"
    assert r["forward_return_pct"] == 21.0, r["forward_return_pct"]
    assert r["claims_total"] == 3 and r["claims_triggered"] == 1, r
    # 라벨 주장은 조용히 사라지지 않는다 - 제외 사유가 남는다
    assert "라벨 주장 1건" in (r["note"] or ""), r["note"]
    print("  수익률·라벨 제외 명시    OK")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if "--score" in sys.argv:
        raise SystemExit(score())

    print(f"{SCORER_VERSION} 자체 점검 (네트워크·DB 없음)")
    _check_same_day_excluded()
    _check_touch_semantics()
    _check_pending_when_short()
    _check_forward_return_and_labels()
    print("Packet 채점 4개 영역 통과. 채점은 --score")
