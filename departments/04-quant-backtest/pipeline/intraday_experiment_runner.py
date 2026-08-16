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

from intraday_alpha_ast import (clocks_of, count_nodes, fingerprint,
                                parse as parse_expr, structural_similarity,
                                unit_of)
from alpha_semantics import validate as validate_semantic_plan
from intraday_candidate import (CandidateAccumulator,
                                CandidatePopulationAccumulator,
                                EVALUATOR_VERSION)
from intraday_microstructure import (IntradayLaneSpec, build_samples,
                                      load_instrument_events_batch, manifest,
                                      source_quality_batch)
from intraday_ablation import INTRADAY_SCREENING_COHORT_VERSION


RUNNER_VERSION = "intraday-experiment-runner-v6"
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
with causal_quotes as (
  select instrument_id::text as instrument_id, count(*) as quote_events
    from market.market_quotes
   where event_time >= %s and event_time < %s
     and received_at is not null
     and greatest(received_at, observed_at) <= %s
     and bid_prices[1] > 0 and ask_prices[1] > 0
     and ask_prices[1] >= bid_prices[1]
   group by instrument_id
), causal_trades as (
  select distinct instrument_id::text as instrument_id
    from market.market_ticks
   where event_time >= %s and event_time < %s
     and received_at is not null
     and greatest(received_at, observed_at) <= %s
)
select q.instrument_id, q.quote_events
  from causal_quotes q
  join causal_trades t using (instrument_id)
 order by q.instrument_id
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
    if str(edge.get("universe_key") or "krx_all").lower() != "krx_all":
        raise ValueError("intraday runner currently requires universe_key=krx_all")
    expression = parse_expr(edge.get("intraday_signal_expr"))
    semantic_plan = validate_semantic_plan(edge.get("semantic_plan") or {})
    output = str(semantic_plan.get("output") or "").upper()
    entry_policy = str(edge.get("entry_policy") or "").upper()
    if output in {"TAKER_NET_PNL", "PASSIVE_FILL_ADJUSTED_PNL"}:
        if unit_of(expression) != "BPS":
            raise ValueError(
                "net-PnL intraday formulas must predict markout in BPS before "
                "the execution-cost hurdle is applied")
        if entry_policy != "PREDICTED_MARKOUT_CLEARS_COST":
            raise ValueError(
                "net-PnL intraday formulas require "
                "entry_policy=PREDICTED_MARKOUT_CLEARS_COST")
    elif not entry_policy:
        entry_policy = "POSITIVE_SCORE"
    horizon = _bounded_int(edge, "horizon_seconds", 5, 1, 3600)
    if int(semantic_plan["horizon_seconds"]) != horizon:
        raise ValueError("semantic_plan horizon must match horizon_seconds")
    execution = str(edge.get("execution") or "TAKER").upper()
    if execution not in {"TAKER", "PASSIVE_FIFO_LOWER_BOUND"}:
        raise ValueError(f"unsupported execution={execution!r}")
    if str(semantic_plan["execution"]).upper() != execution:
        raise ValueError("semantic_plan execution must match execution")
    position_mode = str(edge.get("position_mode") or "LONG_ONLY").upper()
    if position_mode != "LONG_ONLY":
        raise ValueError(
            "position_mode must be LONG_ONLY until point-in-time borrow availability, "
            "borrow fees, and short-sale execution constraints are available")
    sample_interval = _bounded_int(
        edge, "sample_interval_seconds", 5, 1, 300)
    requested_lookback = _bounded_int(
        edge, "feature_lookback_seconds", 30, 1, 3600)
    screening = edge.get("screening_population") or []
    if not isinstance(screening, list) or len(screening) > 7:
        raise ValueError("screening_population must contain at most seven candidates")
    cohort_version = str(edge.get("screening_cohort_version") or "")
    if screening and cohort_version != INTRADAY_SCREENING_COHORT_VERSION:
        raise ValueError(
            "populated intraday screening cohort must use "
            f"{INTRADAY_SCREENING_COHORT_VERSION}; got "
            f"{cohort_version or '(missing)'}. Reassemble it under the current "
            "formula and structural-ablation contract before replay")
    parsed_screening = []
    known = {fingerprint(expression)}
    all_clocks = set(clocks_of(expression))
    all_horizons = {horizon}
    executions = {execution}
    for index, raw in enumerate(screening):
        if not isinstance(raw, dict):
            raise ValueError(f"screening_population[{index}] must be an object")
        candidate_expr = parse_expr(raw.get("intraday_signal_expr"))
        candidate_fp = fingerprint(candidate_expr)
        if candidate_fp in known:
            raise ValueError(
                f"screening_population[{index}] duplicates another candidate")
        if raw.get("ast_fingerprint") not in (None, "", candidate_fp):
            raise ValueError(
                f"screening_population[{index}] fingerprint does not match AST")
        known.add(candidate_fp)
        plan = validate_semantic_plan(raw.get("semantic_plan") or {})
        candidate_horizon = int(plan["horizon_seconds"])
        candidate_execution = str(plan["execution"]).upper()
        candidate_output = str(plan["output"]).upper()
        policy = str(raw.get("entry_policy") or "").upper()
        if candidate_output in {
                "TAKER_NET_PNL", "PASSIVE_FILL_ADJUSTED_PNL"}:
            if unit_of(candidate_expr) != "BPS":
                raise ValueError(
                    f"screening_population[{index}] net-PnL AST must output BPS")
            if policy != "PREDICTED_MARKOUT_CLEARS_COST":
                raise ValueError(
                    f"screening_population[{index}] lacks the cost hurdle")
        elif not policy:
            policy = "POSITIVE_SCORE"
        all_horizons.add(candidate_horizon)
        executions.add(candidate_execution)
        all_clocks.update(clocks_of(candidate_expr))
        parsed_screening.append({
            **raw,
            "ast_fingerprint": candidate_fp,
            "intraday_signal_expr": candidate_expr,
            "semantic_plan": plan,
            "horizon_seconds": candidate_horizon,
            "execution": candidate_execution,
            "entry_policy": policy,
            "screening_only": True,
        })
    feature_lookback = max([requested_lookback, *all_clocks])
    if feature_lookback > 3600:
        raise ValueError("population feature lookback exceeds 3600 seconds")
    population_execution = (
        "PASSIVE_FIFO_LOWER_BOUND"
        if "PASSIVE_FIFO_LOWER_BOUND" in executions else "TAKER")
    config = {
        "research_lane": "INTRADAY_EVENT",
        "semantic_plan": semantic_plan,
        "semantic_fingerprint": edge.get("semantic_fingerprint"),
        "intraday_signal_expr": expression,
        "horizon_seconds": horizon,
        "sample_interval_seconds": sample_interval,
        "feature_lookback_seconds": feature_lookback,
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
        "entry_policy": entry_policy,
        "minimum_predicted_edge_bps": _bounded_float(
            edge, "minimum_predicted_edge_bps", 0.0, 0.0, 10_000.0),
        "evaluation_days": _bounded_int(edge, "evaluation_days", 60, 10, 250),
        # A shard is only a bounded execution unit. The scientific universe is
        # every causally observed calibration instrument, never a top-N sample.
        "universe_mode": "ALL_CAUSALLY_COLLECTED",
        "instrument_shard_size": _bounded_int(
            edge, "instrument_shard_size", 8, 2, 64),
        "screening_population": parsed_screening,
        "screening_cohort_version": cohort_version or None,
        "screening_trial_exposure": len(parsed_screening),
        "population_execution_model": population_execution,
    }
    if edge.get("instrument_count") is not None:
        config["legacy_instrument_count_ignored"] = int(edge["instrument_count"])
    spec = IntradayLaneSpec(
        sample_interval_seconds=config["sample_interval_seconds"],
        feature_lookback_seconds=config["feature_lookback_seconds"],
        horizons_seconds=tuple(sorted(all_horizons)),
        order_latency_ms=config["order_latency_ms"],
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
        "universe_mode": "ALL_CAUSALLY_COLLECTED",
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
                     cutoff,
                     calibration_start, calibration_end + timedelta(hours=1),
                     cutoff))
        instruments = [row[0] for row in cur.fetchall()]
    readiness = ("FULL" if evaluation_count >= config["evaluation_days"]
                 else "SHORT_DIAGNOSTIC")
    return {"status": "PASS" if len(instruments) >= 2 else "INSUFFICIENT_INSTRUMENTS",
            "selection_rule": (
                "all instruments with valid causally available quotes and trades "
                "in up to five pre-evaluation sessions"),
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
    identity = {key: value for key, value in config.items()
                if key not in {"cutoff", "instrument_shard_size",
                               "legacy_instrument_count_ignored"}}
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


def _candidate_accumulators(config: dict, spec: IntradayLaneSpec, *, trials: int
                            ) -> dict[str, CandidateAccumulator]:
    """Build independent statistics engines for one shared replay cohort."""
    effective_trials = max(1, int(trials)) + len(config["screening_population"])

    def build(row: dict) -> CandidateAccumulator:
        return CandidateAccumulator(
            expr=row["intraday_signal_expr"], spec=spec,
            horizon_seconds=row["horizon_seconds"], execution=row["execution"],
            position_mode=config["position_mode"], threshold=config["threshold"],
            entry_policy=row["entry_policy"],
            minimum_predicted_edge_bps=config["minimum_predicted_edge_bps"],
            trials=effective_trials, family_pbo=None,
            semantic_plan=row["semantic_plan"])

    primary = {
        "intraday_signal_expr": config["intraday_signal_expr"],
        "horizon_seconds": config["horizon_seconds"],
        "execution": config["execution"],
        "entry_policy": config["entry_policy"],
        "semantic_plan": config["semantic_plan"],
    }
    out = {"PRIMARY": build(primary)}
    for row in config["screening_population"]:
        out[row["ast_fingerprint"]] = build(row)
    return out


def _pareto_ranks(rows: list[dict]) -> dict[str, int]:
    """Non-dominated ranks over net/gross/coverage/novelty and complexity."""
    remaining = {row["key"]: row for row in rows}
    ranks: dict[str, int] = {}
    rank = 1

    def vector(row):
        summary = row["report"].get("summary") or {}
        missing = float("-inf")
        return (
            summary.get("mean_net_bps_per_opportunity")
            if summary.get("mean_net_bps_per_opportunity") is not None else missing,
            summary.get("mean_mid_markout_bps")
            if summary.get("mean_mid_markout_bps") is not None else missing,
            summary.get("instrument_coverage")
            if summary.get("instrument_coverage") is not None else missing,
            row["novelty"],
            -row["complexity"],
        )

    while remaining:
        frontier = []
        for key, row in remaining.items():
            values = vector(row)
            dominated = False
            for other_key, other in remaining.items():
                if other_key == key:
                    continue
                rival = vector(other)
                if all(left >= right for left, right in zip(rival, values)) and \
                        any(left > right for left, right in zip(rival, values)):
                    dominated = True
                    break
            if not dominated:
                frontier.append(key)
        for key in frontier:
            ranks[key] = rank
            remaining.pop(key)
        rank += 1
    return ranks


def _annotate_population(config: dict, reports: dict[str, dict]) -> dict:
    """Label screen evidence without granting it promotion authority."""
    primary = reports["PRIMARY"]
    primary_summary = primary.get("summary") or {}
    primary_expr = config["intraday_signal_expr"]
    metadata = {row["ast_fingerprint"]: row
                for row in config["screening_population"]}
    ranking_rows = [{
        "key": "PRIMARY", "report": primary, "novelty": 0.0,
        "complexity": count_nodes(primary_expr),
    }]
    for key, report in reports.items():
        if key == "PRIMARY":
            continue
        expression = metadata[key]["intraday_signal_expr"]
        ranking_rows.append({
            "key": key, "report": report,
            "novelty": 1.0 - structural_similarity(primary_expr, expression),
            "complexity": count_nodes(expression),
        })
    ranks = _pareto_ranks(ranking_rows)
    screening_reports = []
    for row in ranking_rows[1:]:
        key, report = row["key"], row["report"]
        source = metadata[key]
        gate_decision = report.get("decision")
        empirical_influence = None
        if source.get("candidate_role") == "STRUCTURAL_ABLATION":
            ablation_summary = report.get("summary") or {}

            def difference(metric: str):
                left, right = primary_summary.get(metric), ablation_summary.get(metric)
                if (not isinstance(left, (int, float)) or isinstance(left, bool)
                        or not isinstance(right, (int, float))
                        or isinstance(right, bool)):
                    return None
                return float(left) - float(right)

            net_increment = difference("mean_net_bps_per_opportunity")
            empirical_influence = {
                "comparison": "PRIMARY_MINUS_STRUCTURAL_ABLATION",
                "ablation_operator": source.get("ablation_operator"),
                "ablation_path": source.get("ablation_path"),
                "ablation_of_ast_fingerprint": source.get(
                    "ablation_of_ast_fingerprint"),
                "net_increment_bps": net_increment,
                "gross_increment_bps": difference("mean_mid_markout_bps"),
                "implementation_drag_increment_bps": difference(
                    "mean_implementation_drag_bps"),
                "coverage_increment": difference("instrument_coverage"),
                "interpretation": (
                    "POSITIVE_POINT_ESTIMATE" if net_increment is not None
                    and net_increment > 0 else
                    "NON_POSITIVE_POINT_ESTIMATE" if net_increment is not None
                    else "NOT_MEASURED"),
                "evidence_warning": (
                    "same-replay screening contrast; descriptive, not causal or "
                    "promotion evidence"),
            }
        report.update({
            "screening_only": True,
            "evidence_tier": "SCREENING_ONLY",
            "screening_gate_decision": gate_decision,
            "decision": "SCREENING_ONLY",
            "candidate_role": source.get("candidate_role"),
            "source_lead_ids": list(source.get("source_lead_ids") or []),
            "title": source.get("title"),
            "evolution_role": source.get("evolution_role"),
            "parent_ast_fingerprint": source.get("parent_ast_fingerprint"),
            "parent_of_ast_fingerprint": source.get(
                "parent_of_ast_fingerprint"),
            "ablation_operator": source.get("ablation_operator"),
            "ablation_path": source.get("ablation_path"),
            "ablation_of_ast_fingerprint": source.get(
                "ablation_of_ast_fingerprint"),
            "ablation_version": source.get("ablation_version"),
            "empirical_influence": empirical_influence,
            "novelty_vs_primary": row["novelty"],
            "complexity_nodes": row["complexity"],
            "pareto_rank": ranks[key],
            "pareto_front": ranks[key] == 1,
            "not_a_promotion": (
                "SCREENING_ONLY evidence may nominate an independent "
                "confirmatory primary experiment; it cannot promote alpha"),
        })
        screening_reports.append(report)
    primary["screening_population"] = screening_reports
    primary["population_evaluation"] = {
        "shared_raw_replay": True,
        "candidate_count": 1 + len(screening_reports),
        "selection_adjusted_trials": primary["summary"].get("trials"),
        "selection_rule": (
            "cost-net/coverage/novelty/complexity Pareto screen plus "
            "same-replay structural-ablation influence"),
        "promotion_authority": "PRIMARY_ONLY",
    }
    primary["summary"].update({
        "screening_candidates": len(screening_reports),
        "screening_pareto_survivors": sum(
            bool(row["pareto_front"]) for row in screening_reports),
        "screening_positive_net": sum(
            ((row.get("summary") or {}).get("mean_net_bps_per_opportunity")
             or 0.0) > 0 for row in screening_reports),
    })
    return primary


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
    screening_summaries: dict[str, dict] = {}
    screening_folds: dict[str, dict[int, dict]] = {}
    screening_meta: dict[str, dict] = {}
    final_dimensions = None
    pre_dimensions = None
    for metric, value, raw_dimensions in rows:
        dimensions = _as_json(raw_dimensions)
        screening_key = dimensions.get("screening_candidate")
        if dimensions.get("summary") is True and not screening_key:
            summary[metric] = float(value)
        elif dimensions.get("summary") is True and screening_key:
            screening_summaries.setdefault(str(screening_key), {})[metric] = \
                float(value)
        if (metric == "fold_mean_net_bps" and "fold" in dimensions
                and not screening_key):
            fold = int(dimensions["fold"])
            folds.setdefault(fold, {"fold": fold,
                                    "start_session": dimensions.get("start_session"),
                                    "end_session": dimensions.get("end_session")})
            folds[fold]["mean_net_bps"] = float(value)
        elif (metric == "fold_mean_net_bps" and "fold" in dimensions
              and screening_key):
            fold = int(dimensions["fold"])
            target = screening_folds.setdefault(str(screening_key), {})
            target.setdefault(fold, {
                "fold": fold,
                "start_session": dimensions.get("start_session"),
                "end_session": dimensions.get("end_session"),
            })["mean_net_bps"] = float(value)
        if metric == "intraday_screening_result" and screening_key:
            screening_meta[str(screening_key)] = dimensions
        if metric == "intraday_pre_pbo_gate_pass":
            pre_dimensions = dimensions
        elif metric == "intraday_gate_pass":
            final_dimensions = dimensions
    gate = final_dimensions or pre_dimensions or {}
    expression = config.get("intraday_signal_expr") or {"const": 0, "unit": "RATIO"}
    parsed = parse_expr(expression)
    from intraday_alpha_ast import fields_of, fingerprint, shape_fingerprint

    screening_reports = []
    for candidate in config.get("screening_population") or []:
        key = str(candidate.get("ast_fingerprint") or "")
        meta = screening_meta.get(key, {})
        screening_reports.append({
            "evaluator_version": EVALUATOR_VERSION,
            "ast_fingerprint": key,
            "summary": screening_summaries.get(key, {}),
            "folds": [screening_folds.get(key, {})[fold]
                      for fold in sorted(screening_folds.get(key, {}))],
            "decision": "SCREENING_ONLY",
            "screening_only": True,
            "evidence_tier": "SCREENING_ONLY",
            "screening_gate_decision": meta.get("screening_gate_decision"),
            "failed_criteria": list(meta.get("failed_criteria") or []),
            "candidate_role": candidate.get("candidate_role"),
            "source_lead_ids": list(candidate.get("source_lead_ids") or []),
            "ablation_operator": candidate.get("ablation_operator"),
            "ablation_path": candidate.get("ablation_path"),
            "ablation_of_ast_fingerprint": candidate.get(
                "ablation_of_ast_fingerprint"),
            "ablation_version": candidate.get("ablation_version"),
            "empirical_influence": meta.get("empirical_influence"),
            "pareto_rank": meta.get("pareto_rank"),
            "pareto_front": meta.get("pareto_front"),
            "novelty_vs_primary": meta.get("novelty_vs_primary"),
            "complexity_nodes": meta.get("complexity_nodes"),
            "idempotent_replay": True,
        })
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
        "screening_population": screening_reports,
        "population_evaluation": {
            "shared_raw_replay": True,
            "candidate_count": 1 + len(screening_reports),
            "promotion_authority": "PRIMARY_ONLY",
        },
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
    for candidate in report.get("screening_population") or []:
        key = candidate["ast_fingerprint"]
        candidate_summary = candidate.get("summary") or {}
        for metric, value in candidate_summary.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                rows.append((metric, value, {
                    "summary": True, "screening_candidate": key}))
        for fold in candidate.get("folds") or []:
            if isinstance(fold.get("mean_net_bps"), (int, float)):
                rows.append(("fold_mean_net_bps", fold["mean_net_bps"], {
                    "screening_candidate": key,
                    "fold": fold["fold"],
                    "start_session": fold["start_session"],
                    "end_session": fold["end_session"],
                }))
        rows.append(("intraday_screening_result",
                     1 if candidate.get("pareto_front") else 0, {
                         "screening_candidate": key,
                         "screening_only": True,
                         "screening_gate_decision": candidate.get(
                             "screening_gate_decision"),
                         "failed_criteria": candidate.get(
                             "failed_criteria") or [],
                         "pareto_rank": candidate.get("pareto_rank"),
                         "pareto_front": bool(candidate.get("pareto_front")),
                         "novelty_vs_primary": candidate.get(
                             "novelty_vs_primary"),
                         "complexity_nodes": candidate.get("complexity_nodes"),
                         "source_lead_ids": candidate.get(
                             "source_lead_ids") or [],
                         "candidate_role": candidate.get("candidate_role"),
                         "empirical_influence": candidate.get(
                             "empirical_influence"),
                     }))
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

    population = CandidatePopulationAccumulator(_candidate_accumulators(
        config, spec, trials=int(hyp.get("_trials") or 1)))
    shard_size = config["instrument_shard_size"]
    shards = [selected["instruments"][index:index + shard_size]
              for index in range(0, len(selected["instruments"]), shard_size)]
    quality_counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "NO_DATA": 0}
    quality_totals = {
        "total_quotes": 0, "eligible_quotes": 0,
        "quotes_without_received_at": 0, "nonpositive_quotes": 0,
        "crossed_quotes": 0,
    }
    quality_examples = []
    shard_reports = []
    try:
        for shard_number, instruments in enumerate(shards, 1):
            shard_samples = 0
            for day in selected["sessions"]:
                start, end = _session_bounds(day)
                load_end = end + spec.purge_gap
                quality = source_quality_batch(
                    market_conn, instrument_ids=instruments, start=start,
                    end=load_end, as_known_at=cutoff)
                events = load_instrument_events_batch(
                    market_conn, instrument_ids=instruments, start=start,
                    end=load_end, as_known_at=cutoff)
                for instrument in instruments:
                    q = quality[instrument]
                    quality_counts[q["status"]] += 1
                    for key in quality_totals:
                        quality_totals[key] += q[key]
                    if q["status"] != "PASS" and len(quality_examples) < 50:
                        quality_examples.append({
                            "instrument_id": instrument, "session": str(day), **q})
                    quotes, trades = events[instrument]
                    samples = build_samples(
                        quotes, trades, spec, start=start, end=end,
                        execution_model=config["population_execution_model"])
                    shard_samples += len(samples)
                    population.add(instrument, samples)
            shard_reports.append({
                "shard": shard_number,
                "instrument_count": len(instruments),
                "sample_count": shard_samples,
                "instrument_fingerprint": hashlib.sha256(
                    "|".join(instruments).encode()).hexdigest()[:16],
            })
        report = _annotate_population(config, population.finish())
        report["source_quality"] = {
            "counts_by_status": quality_counts,
            "totals": quality_totals,
            "non_pass_examples": quality_examples,
        }
        report["universe_shards"] = shard_reports
        report["summary"]["universe_shards"] = len(shards)
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
