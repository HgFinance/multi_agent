"""ops_health_monitor.py의 __main__ 자체 점검을 pytest로 옮긴 것.

소유: 동규 (AI QA/감사본부). 시나리오 번호와 내용은 원본과 동일하게 유지한다.

실행: python -m pytest departments/06-ai-qa-audit/tests/test_ops_health_monitor.py -v
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "audit"))

from ops_health_monitor import (
    AgentHealthMetrics,
    BreachKind,
    IncidentSeverity,
    OpsHealthMonitor,
    OpsHealthStatus,
    OpsThresholds,
)

now = datetime.now(timezone.utc)
window_start = now - timedelta(minutes=5)
monitor = OpsHealthMonitor()
thresholds = OpsThresholds(
    max_error_rate=Decimal("0.02"),
    critical_error_rate=Decimal("0.10"),
    max_p95_latency_ms=Decimal(2000),
    critical_p95_latency_ms=Decimal(5000),
    max_cost_usd_per_window=Decimal(10),
)


def metrics(
    scope="research-department",
    requests=1000,
    errors=5,
    p95=Decimal(800),
    cost=Decimal("2.5"),
) -> AgentHealthMetrics:
    return AgentHealthMetrics(
        scope=scope,
        window_start=window_start,
        window_end=now,
        request_count=requests,
        error_count=errors,
        p95_latency_ms=p95,
        cost_usd=cost,
    )


def test_01_all_healthy_no_incident():
    a1 = monitor.evaluate(metrics(errors=5), thresholds)
    assert a1.status is OpsHealthStatus.HEALTHY
    assert a1.incident is None


def test_02_error_rate_soft_breach_degraded_sev3():
    a2 = monitor.evaluate(metrics(requests=1000, errors=30), thresholds)
    assert a2.status is OpsHealthStatus.DEGRADED
    assert a2.incident is not None and a2.incident.severity is IncidentSeverity.SEV3
    assert BreachKind.ERROR_RATE_SOFT in a2.breaches


def test_03_error_rate_critical_breach_critical_sev2():
    a3 = monitor.evaluate(metrics(requests=1000, errors=150), thresholds)
    assert a3.status is OpsHealthStatus.CRITICAL
    assert a3.incident.severity is IncidentSeverity.SEV2
    assert BreachKind.ERROR_RATE_CRITICAL in a3.breaches


def test_04_latency_soft_breach_degraded():
    a4 = monitor.evaluate(metrics(p95=Decimal(2500)), thresholds)
    assert a4.status is OpsHealthStatus.DEGRADED
    assert BreachKind.LATENCY_SOFT in a4.breaches


def test_05_latency_critical_breach_critical_sev2():
    a5 = monitor.evaluate(metrics(p95=Decimal(6000)), thresholds)
    assert a5.status is OpsHealthStatus.CRITICAL
    assert a5.incident.severity is IncidentSeverity.SEV2
    assert BreachKind.LATENCY_CRITICAL in a5.breaches


def test_06_cost_over_budget_alone_degraded():
    a6 = monitor.evaluate(metrics(cost=Decimal(15)), thresholds)
    assert a6.status is OpsHealthStatus.DEGRADED
    assert BreachKind.COST_OVER_BUDGET in a6.breaches


def test_07_simultaneous_critical_breaches_escalate_to_sev1():
    a7 = monitor.evaluate(
        metrics(requests=1000, errors=150, p95=Decimal(6000)), thresholds
    )
    assert a7.status is OpsHealthStatus.CRITICAL
    assert a7.incident.severity is IncidentSeverity.SEV1, (
        "동시 Critical은 SEV1이어야 함"
    )


def test_08_zero_traffic_avoids_false_positive():
    a8 = monitor.evaluate(metrics(requests=0, errors=0), thresholds)
    assert a8.status is OpsHealthStatus.HEALTHY


def test_09_incident_code_reproducible_for_same_input():
    a9a = monitor.evaluate(metrics(requests=1000, errors=30), thresholds)
    a9b = monitor.evaluate(metrics(requests=1000, errors=30), thresholds)
    assert a9a.incident.incident_code == a9b.incident.incident_code, (
        "같은 scope·시각인데 코드가 다름"
    )
    assert a9a.incident.severity == a9b.incident.severity
