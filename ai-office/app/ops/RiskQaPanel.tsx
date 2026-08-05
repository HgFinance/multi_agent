"use client";

import { useEffect, useState } from "react";

import type { Agent, Snapshot } from "../game/sim";
import { useBffFeed } from "./bffClient";
import {
  fetchRiskQaProjection,
  getRiskQaActivity,
  RISK_QA_CONNECTION,
  RISK_QA_LOG_EVENTS,
  RISK_QA_RETRY_POLICY,
  type RiskQaProjection,
} from "./riskQaBridge";

function compactSkills(skills: readonly string[]): string {
  return skills.join(" · ");
}

const statusClass: Record<string, string> = {
  "진행 중": "working",
  "업무 중": "working",
  "회의 중": "working",
  "이동 중": "working",
  완료: "done",
  "승인 대기": "approval",
  "연동 대기": "blocked",
  대기: "waiting",
  "출근 대기": "waiting",
  "출근 전": "waiting",
  휴식: "waiting",
  "상태 미수신": "blocked",
  OFFLINE: "blocked",
  DEGRADED: "approval",
  RUNNING: "working",
  QUEUED: "waiting",
  IDLE: "waiting",
  BLOCKED: "blocked",
  ERROR: "blocked",
};

export default function RiskQaPanel({
  agents,
  snapshot,
}: {
  agents: readonly Agent[];
  snapshot: Snapshot;
}) {
  const { snapshot: backendSnapshot } = useBffFeed();
  const [riskQaProjection, setRiskQaProjection] = useState<RiskQaProjection | null>(null);

  useEffect(() => {
    let active = true;
    void fetchRiskQaProjection()
      .then((next) => {
        if (active) setRiskQaProjection(next);
      })
      .catch(() => {
        if (active) setRiskQaProjection(null);
      });
    return () => {
      active = false;
    };
  }, [backendSnapshot?.operations?.sequence]);

  const activities = RISK_QA_CONNECTION.map((department) => ({
    department,
    activity: getRiskQaActivity(department, agents, snapshot),
    backend: riskQaProjection?.departments.find(
      (item) => item.department_code === (department.id === "ops" ? "risk-management" : "qa-department"),
    ) ?? backendSnapshot?.operations?.departments.find(
      (item) => item.department_code === (department.id === "ops" ? "risk-management" : "qa-department"),
    ),
  }));
  const totalWorkers = activities.reduce((total, item) => total + item.department.employees.length, 0);
  const totalWorking = activities.reduce((total, item) => total + item.activity.workingCount, 0);

  return (
    <section className="win risk-qa-panel" aria-labelledby="risk-qa-title">
      <div className="win-bar">
        <span>🔐 risk_qa.department_bridge</span>
        <span className="window-controls" aria-hidden="true">
          — ▢ ✕
        </span>
      </div>
      <div className="win-body">
        <div className="section-heading">
          <div>
            <p className="eyebrow">ALLOWLISTED CONNECTION · 2 DEPARTMENTS</p>
            <h2 id="risk-qa-title">Risk · QA 현황</h2>
          </div>
          <span className={`mini-badge ${riskQaProjection?.status === "CONNECTED" ? "live" : snapshot.running ? "live" : "idle"}`}>
            {riskQaProjection?.status === "CONNECTED" ? "EVENT CONNECTED" : snapshot.running ? "SIMULATION" : "DEGRADED"}
          </span>
        </div>

        <p className="risk-qa-note">
          Hermes Head가 부서 단위로 조율하고, 각 Worker는 독립 LangGraph + Ollama qwen3:1.7b로
          non-binding context를 제공합니다. Worker 상태는 BFF의 agent.status.v1 projection을 우선 표시하며,
          상태 event가 없을 때만 로컬 Office 상태를 보조 표시합니다. 주문·원장·Risk Limit 변경의 증거는 아닙니다.
        </p>

        <div className="risk-qa-metrics" aria-label="Risk와 QA Worker">
          <span>
            Worker <b>{totalWorkers}명</b>
          </span>
          <span>
            Simulation working <b>{totalWorking}명</b>
          </span>
          <span>
            Retry 상한 <b>{RISK_QA_RETRY_POLICY.maxRetries}회</b> (총 {RISK_QA_RETRY_POLICY.maxAttempts}회)
          </span>
        </div>

        <div className="risk-qa-policy">
          <span>
            Risk 실패 <b>{RISK_QA_RETRY_POLICY.riskFallback}</b>
          </span>
          <span>
            QA 실패 <b>{RISK_QA_RETRY_POLICY.qaFallback}</b>
          </span>
        </div>

        <div className="risk-qa-log-flow" aria-label="로그 및 리플레이 이벤트">
          <span>LOG/REPLAY</span>
          {RISK_QA_LOG_EVENTS.map((eventType) => (
            <code key={eventType}>{eventType}</code>
          ))}
        </div>

        <div className="risk-qa-departments">
          {activities.map(({ department, activity, backend }) => (
            <article className="risk-qa-department" key={department.id}>
              <div className="risk-qa-department-heading">
                <div>
                  <span className="tiny-label">{department.domain}</span>
                  <h3>{department.name}</h3>
                </div>
                <span className={`status-pill ${statusClass[backend?.status ?? activity.statusLabel] ?? "waiting"}`}>
                  {backend?.status ?? activity.statusLabel}
                </span>
              </div>

              <div className="risk-qa-contract">
                <b>
                  Head: {department.orchestrator} · {department.headProvider}/{department.headModel}
                </b>
                <span>
                  Workers: {department.employeeExecutor} · {department.workerModel}
                </span>
                <code>{department.runtimeContract}</code>
              </div>

              <div className="risk-qa-live-summary">
                <strong>
                  {backend ? `${backend.active_worker_count}/${backend.worker_count}명 registry active` : "BFF 상태 대기"}
                </strong>
                <span>{backend?.status_reason ?? activity.taskLabel}</span>
              </div>

              <div className="risk-qa-staff" aria-label={`${department.name} Worker 목록`}>
                {activity.employees.map(({ employee, agent }) => {
                  const runtimeAgent = riskQaProjection?.agents.find(
                    (item) => item.agent_id === employee.profileId || item.worker_id === employee.profileId,
                  );
                  const displayedStatus = runtimeAgent?.status ?? agent?.status ?? "상태 미수신";
                  return (
                  <div className="risk-qa-employee" key={employee.profileId}>
                    <div>
                      <b>{employee.name}</b>
                      <span>{employee.role}</span>
                    </div>
                    <code>{employee.profileId}</code>
                    <span className={`risk-qa-agent-status ${statusClass[displayedStatus] ?? "waiting"}`}>
                      {runtimeAgent?.status ?? (agent ? agent.status : "runtime 미수신")}
                    </span>
                    <small>{compactSkills(employee.skills)}</small>
                    {runtimeAgent?.reason || agent?.taskLabel ? (
                      <small className="risk-qa-agent-task">{runtimeAgent?.reason ?? agent?.taskLabel}</small>
                    ) : null}
                  </div>
                  );
                })}
              </div>
            </article>
          ))}
        </div>

        <div className="dash-note risk-qa-source">
          Source: <code>departments/03-risk/hermes/config.yaml</code> ·{" "}
          <code>departments/06-ai-qa-audit/hermes/config.yaml</code>
          <br />
          Head/Worker 수와 역할은 현재 Worker Registry를 기준으로 한다. 실제 실행 상태는 각 부서 API,
          Hermes 세션, LangGraph run log에서 별도로 확인해야 한다.
        </div>
      </div>
    </section>
  );
}
