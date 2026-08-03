#!/usr/bin/env python3
"""Sprint K2/K3 경계: P0 Agent Ops Health Monitor.

소유: 동규 (AI QA/감사본부)
근거: docs/05-teams/TEAM_DONGGYU_RISK_QA_GUIDE.md 6.1(audit.agent_runs/tool_calls),
      6.3(Finding과 Incident), 10.2(QA/감사 P0 - opentelemetry-sdk/prometheus-client/structlog),
      11.4(핵심 Metric - Unauthorized Tool Call, Incident MTTA/MTTR)
      qa-department/hermes/config.yaml의 agent-ops-monitor 페르소나(hiring_priority P0)
      supabase/migrations/20260729000500_audit_api_security.sql의 audit.incidents 컬럼·값과
      이름을 맞췄다 (severity는 SEV4..SEV1이지 LOW/HIGH가 아니다).

agent-ops-monitor 페르소나는 "에러율·지연·비용을 항상 모니터링하고, 본부 운영 상태가
나빠지면 Incident Evidence를 올린다"가 임무다. 실제 OpenTelemetry/Prometheus 수집
Infra는 아직 없으므로 여기서는 그 판정 로직만 결정론적으로 구현한다 - Threshold
초과 여부를 LLM 판단 없이 그대로 계산한다.

이 모듈은 Incident를 "감지해서 초안을 만드는" 역할까지만 한다. 실제 Incident Open,
Commander 배정과 종결은 audit.incidents/incident_events 워크플로우(K3 이후)의 몫이다.

자체 점검: python departments/06-ai-qa-audit/audit/ops_health_monitor.py
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4


class OpsHealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    CRITICAL = "critical"


class IncidentSeverity(StrEnum):
    """audit.incidents.severity와 값이 같아야 한다 (SEV1이 가장 심각)."""

    SEV4 = "SEV4"
    SEV3 = "SEV3"
    SEV2 = "SEV2"
    SEV1 = "SEV1"


class BreachKind(StrEnum):
    ERROR_RATE_SOFT = "error_rate_soft"
    ERROR_RATE_CRITICAL = "error_rate_critical"
    LATENCY_SOFT = "latency_soft"
    LATENCY_CRITICAL = "latency_critical"
    COST_OVER_BUDGET = "cost_over_budget"


@dataclass(frozen=True)
class AgentHealthMetrics:
    """Model Gateway/Queue Telemetry의 스텁 (팀 가이드 6.1 audit.agent_runs/tool_calls
    집계값). 이 모듈은 값을 직접 수집하지 않는다."""

    scope: str  # 예: "research-department" 또는 특정 persona id
    window_start: datetime
    window_end: datetime
    request_count: int
    error_count: int
    p95_latency_ms: Decimal
    cost_usd: Decimal

    @property
    def error_rate(self) -> Decimal:
        # 트래픽이 없으면 판단할 신호가 없다 - 0으로 두되 이상 신호로 취급하지 않는다.
        if self.request_count == 0:
            return Decimal(0)
        return Decimal(self.error_count) / Decimal(self.request_count)


@dataclass(frozen=True)
class OpsThresholds:
    max_error_rate: Decimal
    critical_error_rate: Decimal
    max_p95_latency_ms: Decimal
    critical_p95_latency_ms: Decimal
    max_cost_usd_per_window: Decimal


@dataclass(frozen=True)
class IncidentDraft:
    """audit.incidents 1행 초안."""

    incident_id: UUID
    incident_code: str
    severity: IncidentSeverity
    title: str
    impact: dict
    started_at: datetime
    detected_at: datetime
    trace_id: UUID


@dataclass(frozen=True)
class OpsAssessment:
    scope: str
    status: OpsHealthStatus
    breaches: tuple[BreachKind, ...]
    incident: IncidentDraft | None


class OpsHealthMonitor:
    """결정론적 Threshold 판정. 여기 없는 등급은 없다."""

    def evaluate(
        self,
        metrics: AgentHealthMetrics,
        thresholds: OpsThresholds,
        trace_id: UUID | None = None,
    ) -> OpsAssessment:
        trace_id = trace_id or uuid4()
        breaches: list[BreachKind] = []
        critical_breaches: list[BreachKind] = []

        if metrics.error_rate >= thresholds.critical_error_rate:
            breaches.append(BreachKind.ERROR_RATE_CRITICAL)
            critical_breaches.append(BreachKind.ERROR_RATE_CRITICAL)
        elif metrics.error_rate >= thresholds.max_error_rate:
            breaches.append(BreachKind.ERROR_RATE_SOFT)

        if metrics.p95_latency_ms >= thresholds.critical_p95_latency_ms:
            breaches.append(BreachKind.LATENCY_CRITICAL)
            critical_breaches.append(BreachKind.LATENCY_CRITICAL)
        elif metrics.p95_latency_ms >= thresholds.max_p95_latency_ms:
            breaches.append(BreachKind.LATENCY_SOFT)

        if metrics.cost_usd >= thresholds.max_cost_usd_per_window:
            # 비용 초과는 그 자체로 CRITICAL로 보지 않는다 - 급박한 장애가 아니라
            # 예산 통제 대상이다. 다른 신호와 겹치면 아래에서 심각도가 같이 올라간다.
            breaches.append(BreachKind.COST_OVER_BUDGET)

        if not breaches:
            return OpsAssessment(scope=metrics.scope, status=OpsHealthStatus.HEALTHY, breaches=(), incident=None)

        if critical_breaches:
            status = OpsHealthStatus.CRITICAL
            # 서로 다른 종류의 Critical 신호가 동시에 뜨면 SEV1, 하나면 SEV2.
            severity = IncidentSeverity.SEV1 if len(critical_breaches) >= 2 else IncidentSeverity.SEV2
        else:
            status = OpsHealthStatus.DEGRADED
            severity = IncidentSeverity.SEV3

        incident = IncidentDraft(
            incident_id=uuid4(),
            incident_code=f"OPS-{metrics.scope}-{metrics.window_end.strftime('%Y%m%dT%H%M%S')}",
            severity=severity,
            title=f"{metrics.scope} 운영 상태 저하 ({', '.join(b.value for b in breaches)})",
            impact={
                "scope": metrics.scope,
                "error_rate": str(metrics.error_rate),
                "p95_latency_ms": str(metrics.p95_latency_ms),
                "cost_usd": str(metrics.cost_usd),
                "breaches": [b.value for b in breaches],
            },
            started_at=metrics.window_start,
            detected_at=metrics.window_end,
            trace_id=trace_id,
        )
        return OpsAssessment(scope=metrics.scope, status=status, breaches=tuple(breaches), incident=incident)


if __name__ == "__main__":
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=5)

    def metrics(scope="research-department", requests=1000, errors=5,
                p95=Decimal(800), cost=Decimal("2.5")) -> AgentHealthMetrics:
        return AgentHealthMetrics(
            scope=scope, window_start=window_start, window_end=now,
            request_count=requests, error_count=errors, p95_latency_ms=p95, cost_usd=cost,
        )

    thresholds = OpsThresholds(
        max_error_rate=Decimal("0.02"), critical_error_rate=Decimal("0.10"),
        max_p95_latency_ms=Decimal(2000), critical_p95_latency_ms=Decimal(5000),
        max_cost_usd_per_window=Decimal(10),
    )

    monitor = OpsHealthMonitor()

    # 1. 전부 정상 범위 -> HEALTHY, Incident 없음
    a1 = monitor.evaluate(metrics(errors=5), thresholds)  # 0.5% 에러율
    assert a1.status is OpsHealthStatus.HEALTHY
    assert a1.incident is None

    # 2. 에러율 Soft 초과 (2%~10%) -> DEGRADED, SEV3
    a2 = monitor.evaluate(metrics(requests=1000, errors=30), thresholds)  # 3%
    assert a2.status is OpsHealthStatus.DEGRADED
    assert a2.incident is not None and a2.incident.severity is IncidentSeverity.SEV3
    assert BreachKind.ERROR_RATE_SOFT in a2.breaches

    # 3. 에러율 Critical 초과 (>=10%) -> CRITICAL, SEV2
    a3 = monitor.evaluate(metrics(requests=1000, errors=150), thresholds)  # 15%
    assert a3.status is OpsHealthStatus.CRITICAL
    assert a3.incident.severity is IncidentSeverity.SEV2
    assert BreachKind.ERROR_RATE_CRITICAL in a3.breaches

    # 4. 지연 Soft 초과 -> DEGRADED
    a4 = monitor.evaluate(metrics(p95=Decimal(2500)), thresholds)
    assert a4.status is OpsHealthStatus.DEGRADED
    assert BreachKind.LATENCY_SOFT in a4.breaches

    # 5. 지연 Critical 초과 -> CRITICAL, SEV2
    a5 = monitor.evaluate(metrics(p95=Decimal(6000)), thresholds)
    assert a5.status is OpsHealthStatus.CRITICAL
    assert a5.incident.severity is IncidentSeverity.SEV2
    assert BreachKind.LATENCY_CRITICAL in a5.breaches

    # 6. 비용 초과 단독 -> DEGRADED (그 자체로 Critical 취급하지 않음)
    a6 = monitor.evaluate(metrics(cost=Decimal(15)), thresholds)
    assert a6.status is OpsHealthStatus.DEGRADED
    assert BreachKind.COST_OVER_BUDGET in a6.breaches

    # 7. 에러율 + 지연이 동시에 Critical -> SEV1 (더 심각하게 격상)
    a7 = monitor.evaluate(metrics(requests=1000, errors=150, p95=Decimal(6000)), thresholds)
    assert a7.status is OpsHealthStatus.CRITICAL
    assert a7.incident.severity is IncidentSeverity.SEV1, "동시 Critical은 SEV1이어야 함"

    # 8. 트래픽 0건 -> 에러율 0으로 보고 HEALTHY (오탐 방지)
    a8 = monitor.evaluate(metrics(requests=0, errors=0), thresholds)
    assert a8.status is OpsHealthStatus.HEALTHY

    # 9. Incident Code는 scope와 시각 기반이라 같은 입력이면 재현 가능
    a9a = monitor.evaluate(metrics(requests=1000, errors=30), thresholds)
    a9b = monitor.evaluate(metrics(requests=1000, errors=30), thresholds)
    assert a9a.incident.incident_code == a9b.incident.incident_code, "같은 scope·시각인데 코드가 다름"
    assert a9a.incident.severity == a9b.incident.severity

    print("ok - Agent Ops Health Monitor 9개 시나리오 점검 통과")
