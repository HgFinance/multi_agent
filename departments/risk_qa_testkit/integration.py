"""Optional external integration probes for the Risk -> QA TEST boundary.

The probe is deliberately separate from the deterministic pipeline. It never
enables Production, never prints credentials, and rolls back the Supabase
event transaction after a schema round-trip. Redis uses a unique temporary
stream and removes it after the probe.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Mapping
from urllib.error import URLError
from urllib.request import Request, urlopen
from uuid import UUID, uuid4

from departments.risk_qa_testkit.pipeline import make_test_packet, run_risk_qa_pipeline
from departments.risk_qa_testkit.research_packet import packet_from_api_payload


ROOT = Path(__file__).resolve().parents[2]


def _load_module(path: Path, name: str) -> Any:
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"integration module unavailable: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _http_json(url: str, *, timeout: float = 5.0) -> dict[str, Any]:
    request = Request(url, headers={"Accept": "application/json"})
    with urlopen(request, timeout=timeout) as response:  # noqa: S310 - URL is operator-configured
        data = json.load(response)
    if not isinstance(data, dict):
        raise ValueError("Research API response must be a JSON object")
    return data


def _check_research_api(environ: Mapping[str, str]) -> dict[str, Any]:
    base_url = (environ.get("RESEARCH_API_URL") or "http://127.0.0.1:8035").rstrip("/")
    packet_url = environ.get("RISK_QA_RESEARCH_PACKET_URL", "").strip()
    result: dict[str, Any] = {
        "configured": True,
        "status": "FAILED",
        "base_url": base_url,
        "health": "FAILED",
        "packet_contract": "NOT_CONFIGURED",
    }
    try:
        health = _http_json(f"{base_url}/health")
        result["health"] = "READY"
        result["status"] = "READY"
        result["health_status"] = str(health.get("status", "unknown"))
        if packet_url:
            payload = _http_json(packet_url)
            packet = packet_from_api_payload(payload)
            result["packet_contract"] = "RESEARCH_PACKET_V2"
            result["packet_id"] = packet.packet_id
            result["input_hash"] = packet.input_hash
            pipeline = run_risk_qa_pipeline("test", packet=packet)
            result["risk_qa_pipeline"] = pipeline["pipeline_status"]
            result["status"] = "READY" if pipeline["pipeline_status"] == "COMPLETED" else "FAILED"
        return result
    except (OSError, URLError, ValueError, RuntimeError) as exc:
        result["status"] = "FAILED"
        result["error_class"] = type(exc).__name__
        return result


def _check_redis(environ: Mapping[str, str]) -> dict[str, Any]:
    url = (environ.get("RISK_QA_EVENT_REDIS_URL") or environ.get("REDIS_URL") or "").strip()
    if not url:
        return {"configured": False, "status": "SKIPPED", "reason": "REDIS_URL_MISSING"}

    stream = f"risk-qa-integration-{uuid4().hex}"
    group = f"qa-integration-{uuid4().hex}"
    client = None
    try:
        import redis

        client = redis.Redis.from_url(
            url,
            socket_connect_timeout=4,
            socket_timeout=4,
            decode_responses=False,
        )
        client.ping()
        risk_bus = _load_module(
            ROOT / "departments/03-risk/risk_events/redis_event_bus.py",
            "risk_event_bus_integration_probe",
        )
        qa_bus = _load_module(
            ROOT / "departments/06-ai-qa-audit/qa_events/redis_event_bus.py",
            "qa_event_bus_integration_probe",
        )
        event_id = uuid4()
        trace_id = uuid4()
        publisher = risk_bus.RedisEventPublisher(client, stream=stream)
        publisher.publish(
            event_id=event_id,
            trace_id=trace_id,
            payload={"decision": "HOLD", "source": "risk-qa-integration-probe"},
        )
        received: list[dict[str, Any]] = []
        consumer = qa_bus.RedisEventBus(
            client,
            stream=stream,
            group=group,
            consumer="qa-integration-probe",
            dedupe_prefix=f"risk-qa:integration:{uuid4().hex}:",
        )
        processed = consumer.consume_once(received.append, min_idle_ms=0)
        matched = bool(received) and received[0]["event_id"] == str(event_id)
        return {
            "configured": True,
            "status": "READY" if processed == 1 and matched else "FAILED",
            "processed": processed,
            "trace_preserved": bool(received) and received[0]["trace_id"] == str(trace_id),
            "dedupe_consumer_group": group,
        }
    except Exception as exc:  # noqa: BLE001 - probe reports a safe error class
        return {"configured": True, "status": "FAILED", "error_class": type(exc).__name__}
    finally:
        if client is not None:
            try:
                client.delete(stream)
                client.close()
            except Exception:  # noqa: BLE001 - cleanup must not hide probe result
                pass


def _check_supabase_event(environ: Mapping[str, str]) -> dict[str, Any]:
    dsn = environ.get("DATABASE_URL", "").strip()
    if not dsn:
        return {"configured": False, "status": "SKIPPED", "reason": "DATABASE_URL_MISSING"}
    conn = None
    try:
        import psycopg2

        conn = psycopg2.connect(dsn, connect_timeout=6, sslmode="require")
        conn.autocommit = False
        cur = conn.cursor()
        cur.execute("select to_regclass('audit.domain_events')")
        relation = cur.fetchone()[0]
        if relation != "audit.domain_events":
            conn.rollback()
            return {"configured": True, "status": "FAILED", "error_class": "EVENT_TABLE_MISSING"}
        event_id = uuid4()
        trace_id = uuid4()
        cur.execute(
            """
            insert into audit.domain_events
              (event_id, event_type, source_department, trace_id, payload, occurred_at, status)
            values (%s, %s, %s, %s, %s::jsonb, now(), 'PROCESSED')
            returning event_id, trace_id
            """,
            (
                str(event_id),
                "risk.qa.integration_probe.v1",
                "risk-management",
                str(trace_id),
                json.dumps({"source": "risk-qa-integration-probe"}),
            ),
        )
        row = cur.fetchone()
        conn.rollback()
        return {
            "configured": True,
            "status": "READY" if row and str(row[0]) == str(event_id) else "FAILED",
            "trace_preserved": bool(row) and str(row[1]) == str(trace_id),
            "transaction": "ROLLED_BACK",
        }
    except Exception as exc:  # noqa: BLE001 - probe reports a safe error class
        if conn is not None:
            conn.rollback()
        return {"configured": True, "status": "FAILED", "error_class": type(exc).__name__}
    finally:
        if conn is not None:
            conn.close()


def run_external_integration_probe(
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Probe Research API, Redis Stream and Supabase Event schema safely."""

    env = os.environ if environ is None else environ
    started = time.perf_counter()
    report = {
        "production_enabled": False,
        "secret_values_exposed": False,
        "research_api": _check_research_api(env),
        "redis": _check_redis(env),
        "supabase_event": _check_supabase_event(env),
    }
    checks = [report["research_api"], report["redis"], report["supabase_event"]]
    configured_checks = [check for check in checks if check.get("configured")]
    report["status"] = (
        "READY"
        if configured_checks and all(check.get("status") == "READY" for check in configured_checks)
        else "PARTIAL"
        if not configured_checks
        else "FAILED"
    )
    report["latency_ms"] = round((time.perf_counter() - started) * 1000, 2)
    return report


def main() -> int:
    report = run_external_integration_probe()
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    return 0 if report["status"] in {"READY", "PARTIAL"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
