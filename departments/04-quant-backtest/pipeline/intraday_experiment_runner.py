"""Governed runtime adapter for the intraday alpha lane.

The scientific evaluator is pure (`intraday_candidate`).  This adapter only
selects a preregistered, bounded Timescale slice and writes immutable lineage and
numeric evidence to the shared quant experiment ledger.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
import hashlib
import json
from zoneinfo import ZoneInfo

from intraday_alpha_ast import parse as parse_expr
from intraday_candidate import EVALUATOR_VERSION, evaluate_candidate
from intraday_microstructure import (IntradayLaneSpec, build_samples,
                                      load_instrument_events, manifest,
                                      source_quality)


RUNNER_VERSION = "intraday-experiment-runner-v2"
COST_MODEL_VERSION = "krx-intraday-execution-v1"
# 2026 listed equities incur 20bp on the sale (KOSPI 5bp STT + 15bp rural,
# KOSDAQ 20bp STT).  A representative online commission is 1.5bp each side.
# Expressing the sale-only tax as 10bp/side keeps long/short evaluation symmetric:
# 10 + 1.5 = 11.5bp per side, 23bp for a round trip.
DEFAULT_FEE_BPS_PER_SIDE = 11.5
MIN_EQUITY_FEE_BPS_PER_SIDE = 10.0
DATASET = ("krx-intraday-events", "v1")
KST = ZoneInfo("Asia/Seoul")

_SESSION_DATES_SQL = """
select distinct (event_time at time zone 'Asia/Seoul')::date as session_date
  from market.market_quotes
 where received_at is not null
   and event_time >= %s
   and greatest(received_at, observed_at) <= %s
   and event_time < (
         date_trunc('day', %s at time zone 'Asia/Seoul')
         at time zone 'Asia/Seoul'
       )
 order by session_date desc
 limit %s
"""

_LIQUID_UNIVERSE_SQL = """
select instrument_id::text, count(*) as quote_events
  from market.market_quotes
 where event_time >= %s and event_time < %s
   and received_at is not null
   and greatest(received_at, observed_at) <= %s
   and bid_prices[1] > 0 and ask_prices[1] >= bid_prices[1]
 group by instrument_id
 order by quote_events desc, instrument_id::text
 limit %s
"""

_LINEAGE_SQL = """
select 'market_quotes' as source, count(*)::bigint,
       min(event_time), max(event_time), max(observed_at),
       max(greatest(received_at, observed_at))
  from market.market_quotes
 where instrument_id::text = any(%s) and event_time >= %s and event_time < %s
   and received_at is not null
   and greatest(received_at, observed_at) <= %s
union all
select 'market_ticks' as source, count(*)::bigint,
       min(event_time), max(event_time), max(observed_at),
       max(greatest(received_at, observed_at))
  from market.market_ticks
 where instrument_id::text = any(%s) and event_time >= %s and event_time < %s
   and received_at is not null
   and greatest(received_at, observed_at) <= %s
order by source
"""


def _bounded_int(edge: dict, key: str, default: int, lo: int, hi: int) -> int:
    try:
        value = int(edge.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be an integer") from None
    if not lo <= value <= hi:
        raise ValueError(f"{key}={value} outside [{lo}, {hi}]")
    return value


def _bounded_float(edge: dict, key: str, default: float,
                   lo: float, hi: float) -> float:
    try:
        value = float(edge.get(key, default))
    except (TypeError, ValueError):
        raise ValueError(f"{key} must be numeric") from None
    if not lo <= value <= hi:
        raise ValueError(f"{key}={value} outside [{lo}, {hi}]")
    return value


def config_from_edge(edge: dict) -> tuple[dict, IntradayLaneSpec]:
    """Bind only controlled knobs; never silently inherit daily defaults."""
    if str(edge.get("research_lane") or "").upper() != "INTRADAY_EVENT":
        raise ValueError("intraday runner requires research_lane=INTRADAY_EVENT")
    expression = parse_expr(edge.get("intraday_signal_expr"))
    horizon = _bounded_int(edge, "horizon_seconds", 5, 1, 3600)
    execution = str(edge.get("execution") or "TAKER").upper()
    if execution not in {"TAKER", "PASSIVE_FIFO_LOWER_BOUND"}:
        raise ValueError(f"unsupported execution={execution!r}")
    position_mode = str(edge.get("position_mode") or "LONG_ONLY").upper()
    if position_mode != "LONG_ONLY":
        raise ValueError(
            "position_mode must be LONG_ONLY until point-in-time borrow availability, "
            "borrow fees, and short-sale execution constraints are available")
    config = {
        "research_lane": "INTRADAY_EVENT",
        "semantic_plan": edge.get("semantic_plan") or {},
        "semantic_fingerprint": edge.get("semantic_fingerprint"),
        "intraday_signal_expr": expression,
        "horizon_seconds": horizon,
        "sample_interval_seconds": _bounded_int(
            edge, "sample_interval_seconds", 5, 1, 300),
        "feature_lookback_seconds": _bounded_int(
            edge, "feature_lookback_seconds", 30, 1, 3600),
        "order_latency_ms": _bounded_int(edge, "order_latency_ms", 250, 0, 10_000),
        "max_quote_age_seconds": _bounded_float(
            edge, "max_quote_age_seconds", 5.0, 0.001, 60.0),
        "fee_bps_per_side": _bounded_float(
            edge, "fee_bps_per_side", DEFAULT_FEE_BPS_PER_SIDE,
            MIN_EQUITY_FEE_BPS_PER_SIDE, 100.0),
        "maker_fee_bps_per_side": _bounded_float(
            edge, "maker_fee_bps_per_side", DEFAULT_FEE_BPS_PER_SIDE,
            MIN_EQUITY_FEE_BPS_PER_SIDE, 100.0),
        "execution": execution,
        "position_mode": position_mode,
        "threshold": _bounded_float(edge, "threshold", 0.0, 0.0, 1_000_000.0),
        "evaluation_days": _bounded_int(edge, "evaluation_days", 60, 10, 250),
        "instrument_count": _bounded_int(edge, "instrument_count", 2, 2, 20),
    }
    spec = IntradayLaneSpec(
        sample_interval_seconds=config["sample_interval_seconds"],
        feature_lookback_seconds=config["feature_lookback_seconds"],
        horizons_seconds=(horizon,), order_latency_ms=config["order_latency_ms"],
        max_quote_age_seconds=config["max_quote_age_seconds"],
        fee_bps_per_side=config["fee_bps_per_side"],
        maker_fee_bps_per_side=config["maker_fee_bps_per_side"],
    )
    return config, spec


def _session_bounds(day) -> tuple[datetime, datetime]:
    return (datetime.combine(day, time(9, 0), KST).astimezone(timezone.utc),
            datetime.combine(day, time(15, 20), KST).astimezone(timezone.utc))


def select_slice(market_conn, config: dict, *, cutoff: datetime) -> dict:
    """Choose a causal calibration universe strictly before the OOS slice.

    Five calibration sessions are preferred, not required.  A newly started
    live feed can therefore produce an explicitly underpowered diagnostic as
    soon as two causal sessions exist.  Statistical promotion thresholds stay
    unchanged in ``evaluate_candidate``.
    """
    calibration_days = 5
    # A LIMIT after DISTINCT/ORDER BY does not bound a hypertable scan: without
    # a lower partition-key predicate Timescale must visit every compressed
    # chunk to prove which sessions are latest. Three calendar days per desired
    # KRX session is conservative across weekends/holidays while keeping the
    # scan finite. The returned sessions, not this calendar window, determine
    # statistical sufficiency.
    oldest_possible = cutoff - timedelta(
        days=max(30, config["evaluation_days"] * 3))
    with market_conn.cursor() as cur:
        cur.execute(_SESSION_DATES_SQL,
                    (oldest_possible, cutoff, cutoff,
                     config["evaluation_days"] + calibration_days))
        days = sorted(row[0] for row in cur.fetchall())
    common = {
        "causal_sessions_available": len(days),
        "requested_evaluation_sessions": config["evaluation_days"],
    }
    if len(days) < 2:
        return {"status": "INSUFFICIENT_SESSIONS", "sessions": [],
                "instruments": [], "calibration_sessions": [],
                "statistical_readiness": "NEEDS_DATA", **common}

    # Retain at least one earlier session for a point-in-time universe.  With
    # enough history this is exactly five calibration + N requested OOS days;
    # with short live history it becomes one calibration + the remaining OOS
    # days instead of fabricating arrival timestamps for legacy backfills.
    evaluation_count = min(config["evaluation_days"], len(days) - 1)
    eval_days = days[-evaluation_count:]
    preceding = days[:-evaluation_count]
    calibration = preceding[-calibration_days:]
    calibration_start, _ = _session_bounds(calibration[0])
    _, calibration_end = _session_bounds(calibration[-1])
    with market_conn.cursor() as cur:
        cur.execute(_LIQUID_UNIVERSE_SQL,
                    (calibration_start, calibration_end + timedelta(hours=1),
                     cutoff, config["instrument_count"]))
        instruments = [row[0] for row in cur.fetchall()]
    readiness = ("FULL" if evaluation_count >= config["evaluation_days"]
                 else "SHORT_DIAGNOSTIC")
    return {"status": "PASS" if len(instruments) >= 2 else "INSUFFICIENT_INSTRUMENTS",
            "selection_rule": "top quote-event count in up to five pre-evaluation sessions",
            "calibration_sessions": [str(day) for day in calibration],
            "calibration_session_count": len(calibration),
            "evaluation_session_count": len(eval_days),
            "statistical_readiness": readiness,
            "sessions": eval_days, "instruments": instruments, **common}


def _lineage(market_conn, selected: dict, cutoff: datetime) -> list[dict]:
    if not selected["sessions"] or not selected["instruments"]:
        return []
    start, _ = _session_bounds(selected["sessions"][0])
    _, end = _session_bounds(selected["sessions"][-1])
    params = (selected["instruments"], start, end + timedelta(hours=1), cutoff)
    with market_conn.cursor() as cur:
        cur.execute(_LINEAGE_SQL, params + params)
        rows = cur.fetchall()
    return [{"source": row[0], "rows": int(row[1]),
             "min_event_time": row[2].isoformat() if row[2] else None,
             "max_event_time": row[3].isoformat() if row[3] else None,
             "max_observed_at": row[4].isoformat() if row[4] else None,
             "max_available_at": row[5].isoformat() if row[5] else None}
            for row in rows]


def _input_hash(hypothesis_id: str, config: dict) -> str:
    # Wall-clock invocation time is audit metadata, not input identity.  The
    # selected sessions/instruments and source lineage below do change whenever
    # data inside the evaluated slice changes, so retries are idempotent without
    # hiding late-arriving observations.
    identity = {key: value for key, value in config.items() if key != "cutoff"}
    payload = json.dumps({"hypothesis_id": hypothesis_id,
                          "runner_version": RUNNER_VERSION,
                          "evaluator_version": EVALUATOR_VERSION,
                          "cost_model_version": COST_MODEL_VERSION,
                          **identity},
                         sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def prepare(hyp: dict, *, market_conn,
            cutoff: datetime | None = None) -> dict:
    """Freeze the causal slice before preregistration or trial allocation."""
    edge = hyp.get("expected_edge") or {}
    config, spec = config_from_edge(edge)
    frozen_cutoff = cutoff or datetime.now(timezone.utc)
    selected = select_slice(market_conn, config, cutoff=frozen_cutoff)
    return {"config": config, "spec": spec, "cutoff": frozen_cutoff,
            "selected": selected}


def record_data_feasibility(meta_conn, hypothesis_id: str,
                            prepared: dict) -> dict:
    """Persist a coverage probe without creating an experiment/trial row."""
    selected = prepared["selected"]
    status = "PASS" if selected.get("status") == "PASS" else "NEEDS_DATA"
    details = {
        "runner_version": RUNNER_VERSION,
        "research_lane": "INTRADAY_EVENT",
        "slice": {**selected,
                  "sessions": [str(day) for day in selected.get("sessions", [])]},
    }
    blob = json.dumps(details, sort_keys=True, separators=(",", ":"),
                      default=str)
    coverage_fingerprint = hashlib.sha256(blob.encode()).hexdigest()
    cutoff = prepared["cutoff"]
    with meta_conn.cursor() as cur:
        cur.execute("""
            insert into quant.data_feasibility_checks
              (hypothesis_id, research_lane, cutoff, coverage_fingerprint,
               status, details, first_checked_at, last_checked_at)
            values (%s,'INTRADAY_EVENT',%s,%s,%s,%s::jsonb,now(),now())
            on conflict (hypothesis_id, coverage_fingerprint) do update set
              cutoff=excluded.cutoff,
              status=excluded.status,
              details=excluded.details,
              last_checked_at=now()
            returning check_id::text
        """, (hypothesis_id, cutoff, coverage_fingerprint, status, blob))
        check_id = cur.fetchone()[0]
    meta_conn.commit()
    return {"check_id": check_id, "status": status,
            "coverage_fingerprint": coverage_fingerprint,
            "details": details}


def _register(meta_conn, hypothesis_id: str, config: dict) -> tuple[str, bool]:
    digest = _input_hash(hypothesis_id, config)
    with meta_conn.cursor() as cur:
        cur.execute("select dataset_id from quant.dataset_manifests where name=%s and version=%s",
                    DATASET)
        row = cur.fetchone()
        if not row:
            raise RuntimeError(f"dataset manifest missing: {DATASET[0]}/{DATASET[1]}")
        cur.execute("""
            insert into quant.experiments
              (hypothesis_id, dataset_id, code_version, config, seed,
               split_policy, cost_model_version, status, input_hash, trace_id,
               started_at)
            values (%s,%s,%s,%s::jsonb,0,%s::jsonb,%s,'RUNNING',%s,
                    gen_random_uuid(),now())
            on conflict (input_hash) do nothing
            returning experiment_id::text
        """, (hypothesis_id, row[0], RUNNER_VERSION,
              json.dumps(config, default=str),
              json.dumps({"unit": "KRX_SESSION", "purge": "horizon+latency",
                          "selection": config["slice"].get("selection_rule",
                                                            config["slice"]["status"])}),
              COST_MODEL_VERSION, digest))
        inserted = cur.fetchone()
        if inserted:
            experiment_id, duplicate = inserted[0], False
        else:
            cur.execute(
                "select experiment_id::text, status from quant.experiments "
                "where input_hash=%s for update", (digest,))
            experiment_id, status = cur.fetchone()
            if status == "FAILED":
                # Exactly one retrying worker can reclaim a failed immutable
                # input.  Existing metrics are deterministic upserts.
                cur.execute(
                    "update quant.experiments set status='RUNNING', "
                    "started_at=now(), ended_at=null where experiment_id=%s "
                    "and status='FAILED' returning experiment_id", (experiment_id,))
                duplicate = cur.fetchone() is None
            elif status == "COMPLETED":
                duplicate = True
            else:
                raise RuntimeError(
                    f"intraday experiment input already {status}: {experiment_id}")
    meta_conn.commit()
    return experiment_id, duplicate


def _as_json(value):
    if isinstance(value, str):
        return json.loads(value)
    return value or {}


def _load_completed_report(meta_conn, experiment_id: str) -> dict:
    """Rehydrate enough immutable evidence for an idempotent orchestrator retry."""
    with meta_conn.cursor() as cur:
        cur.execute("select config from quant.experiments where experiment_id=%s",
                    (experiment_id,))
        config = _as_json(cur.fetchone()[0])
        cur.execute("""
            select metric, value, dimensions
              from quant.experiment_metrics
             where experiment_id=%s and split='WALK_FORWARD'
             order by metric, dimensions::text
        """, (experiment_id,))
        rows = cur.fetchall()
    summary, folds = {}, {}
    final_dimensions = None
    pre_dimensions = None
    for metric, value, raw_dimensions in rows:
        dimensions = _as_json(raw_dimensions)
        if dimensions.get("summary") is True:
            summary[metric] = float(value)
        if metric == "fold_mean_net_bps" and "fold" in dimensions:
            fold = int(dimensions["fold"])
            folds.setdefault(fold, {"fold": fold,
                                    "start_session": dimensions.get("start_session"),
                                    "end_session": dimensions.get("end_session")})
            folds[fold]["mean_net_bps"] = float(value)
        if metric == "intraday_pre_pbo_gate_pass":
            pre_dimensions = dimensions
        elif metric == "intraday_gate_pass":
            final_dimensions = dimensions
    gate = final_dimensions or pre_dimensions or {}
    expression = config.get("intraday_signal_expr") or {"const": 0, "unit": "RATIO"}
    parsed = parse_expr(expression)
    from intraday_alpha_ast import fields_of, fingerprint, shape_fingerprint

    return {
        "evaluator_version": EVALUATOR_VERSION,
        "ast_fingerprint": fingerprint(parsed),
        "ast_shape_fingerprint": shape_fingerprint(parsed),
        "fields": sorted(fields_of(parsed)),
        "lane_manifest": config.get("lane_manifest") or {},
        "causality": {"rehydrated": True},
        "folds": [folds[key] for key in sorted(folds)],
        "session_returns_bps": {},
        "summary": summary,
        "failed_criteria": list(gate.get("failed_criteria") or []),
        "decision": gate.get("decision") or "HOLD",
        "not_a_promotion": (
            "SUBMIT_TO_QA is a review request; Risk, QA, and CEO retain promotion authority"),
        "slice": config.get("slice") or {},
        "source_quality": [],
        "idempotent_replay": True,
    }


def _store_report(meta_conn, experiment_id: str, report: dict) -> None:
    summary = report.get("summary") or {}
    rows = [(key, value, {"summary": True}) for key, value in summary.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)]
    for fold in report.get("folds") or []:
        if isinstance(fold.get("mean_net_bps"), (int, float)):
            rows.append(("fold_mean_net_bps", fold["mean_net_bps"],
                         {"fold": fold["fold"], "start_session": fold["start_session"],
                          "end_session": fold["end_session"]}))
            rows.append(("total_return", fold["mean_net_bps"] / 10_000.0,
                         {"window": f"INTRADAY_FOLD_{fold['fold']}",
                          "start_session": fold["start_session"],
                          "end_session": fold["end_session"]}))
    rows.append(("intraday_pre_pbo_gate_pass",
                 1 if report.get("decision") == "SUBMIT_TO_QA" else 0,
                 {"decision": report.get("decision"),
                  "failed_criteria": report.get("failed_criteria") or []}))
    with meta_conn.cursor() as cur:
        for metric, value, dimensions in rows:
            cur.execute("""
                insert into quant.experiment_metrics
                  (experiment_id, split, metric, value, dimensions, cost_model_version)
                values (%s,'WALK_FORWARD',%s,%s,%s::jsonb,%s)
                on conflict (experiment_id, split, metric, dimensions)
                do update set value=excluded.value
            """, (experiment_id, metric, value, json.dumps(dimensions),
                  COST_MODEL_VERSION))
        cur.execute("update quant.experiments set status='COMPLETED', ended_at=now() where experiment_id=%s",
                    (experiment_id,))
    meta_conn.commit()


def persist_final_gate(meta_conn, experiment_id: str, report: dict) -> None:
    """Persist the post-ledger PBO decision exactly once as the release evidence."""
    dimensions = {"decision": report.get("decision"),
                  "failed_criteria": report.get("failed_criteria") or []}
    pbo = (report.get("summary") or {}).get("pbo")
    with meta_conn.cursor() as cur:
        cur.execute("""
            insert into quant.experiment_metrics
              (experiment_id, split, metric, value, dimensions, cost_model_version)
            values (%s,'WALK_FORWARD','intraday_gate_pass',%s,%s::jsonb,%s)
            on conflict (experiment_id, split, metric, dimensions)
            do update set value=excluded.value
        """, (experiment_id,
              1 if report.get("decision") == "SUBMIT_TO_QA" else 0,
              json.dumps(dimensions), COST_MODEL_VERSION))
        if pbo is not None:
            cur.execute("""
                insert into quant.experiment_metrics
                  (experiment_id, split, metric, value, dimensions, cost_model_version)
                values (%s,'WALK_FORWARD','pbo',%s,'{"summary":true}'::jsonb,%s)
                on conflict (experiment_id, split, metric, dimensions)
                do update set value=excluded.value
            """, (experiment_id, pbo, COST_MODEL_VERSION))
    meta_conn.commit()


def run(hyp: dict, hypothesis_id: str, *, meta_conn, market_conn) -> dict:
    prepared = hyp.get("_intraday_preflight") or prepare(
        hyp, market_conn=market_conn)
    config = prepared["config"]
    spec = prepared["spec"]
    cutoff = prepared["cutoff"]
    selected = prepared["selected"]
    if selected.get("status") != "PASS":
        raise RuntimeError(
            "intraday run called with non-executable feasibility slice: "
            f"{selected.get('status')}")
    lineage = _lineage(market_conn, selected, cutoff)
    persisted = {**config, "cutoff": cutoff.isoformat(),
                 "slice": {**selected,
                           "sessions": [str(day) for day in selected["sessions"]]},
                 "source_lineage": lineage, "lane_manifest": manifest(spec)}
    experiment_id, duplicate = _register(meta_conn, hypothesis_id, persisted)
    if duplicate:
        report = _load_completed_report(meta_conn, experiment_id)
        return {
            "experiment_id": experiment_id, "duplicate": True,
            "fragility": "ROBUST" if report["decision"] == "SUBMIT_TO_QA" else
                         "INSUFFICIENT" if report["decision"] == "NO_EVIDENCE" else
                         "FRAGILE",
            "backtest_metrics": {
                "turnover_total": report["summary"].get("opportunities", 0),
                "total_return": report["summary"].get(
                    "mean_net_bps_per_opportunity")},
            "intraday_report": report, "research_lane": "INTRADAY_EVENT"}

    samples: dict[str, list] = {instrument: [] for instrument in selected["instruments"]}
    quality = []
    try:
        for day in selected["sessions"]:
            start, end = _session_bounds(day)
            load_end = end + spec.purge_gap
            for instrument in selected["instruments"]:
                q = source_quality(market_conn, instrument_id=instrument,
                                   start=start, end=load_end, as_known_at=cutoff)
                quality.append({"instrument_id": instrument, "session": str(day), **q})
                quotes, trades = load_instrument_events(
                    market_conn, instrument_id=instrument, start=start,
                    end=load_end, as_known_at=cutoff)
                samples[instrument].extend(build_samples(
                    quotes, trades, spec, start=start, end=end))
        report = evaluate_candidate(
            samples, expr=config["intraday_signal_expr"], spec=spec,
            horizon_seconds=config["horizon_seconds"], execution=config["execution"],
            position_mode=config["position_mode"],
            threshold=config["threshold"], trials=int(hyp.get("_trials") or 1),
            family_pbo=None, semantic_plan=config["semantic_plan"])
        report["source_quality"] = quality
        report["slice"] = persisted["slice"]
        _store_report(meta_conn, experiment_id, report)
    except Exception:
        with meta_conn.cursor() as cur:
            cur.execute("update quant.experiments set status='FAILED', ended_at=now() where experiment_id=%s",
                        (experiment_id,))
        meta_conn.commit()
        raise
    return {"experiment_id": experiment_id,
            "fragility": "ROBUST" if report["decision"] == "SUBMIT_TO_QA" else
                         "INSUFFICIENT" if report["decision"] == "NO_EVIDENCE" else "FRAGILE",
            "backtest_metrics": {"turnover_total": report["summary"]["opportunities"],
                                 "total_return": report["summary"].get(
                                     "mean_net_bps_per_opportunity")},
            "intraday_report": report, "research_lane": "INTRADAY_EVENT"}
