"use client";

import type { Agent, Snapshot } from "../game/sim";
import {
  getRiskQaActivity,
  RISK_QA_CONNECTION,
  RISK_QA_LOG_EVENTS,
  RISK_QA_RETRY_POLICY,
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
};

export default function RiskQaPanel({
  agents,
  snapshot,
}: {
  agents: readonly Agent[];
  snapshot: Snapshot;
}) {
  const activities = RISK_QA_CONNECTION.map((department) => ({
    department,
    activity: getRiskQaActivity(department, agents, snapshot),
  }));
  const totalEmployees = activities.reduce((total, item) => total + item.department.employees.length, 0);
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
            <h2 id="risk-qa-title">Risk · QA 작업 현황</h2>
          </div>
          <span className={`mini-badge ${snapshot.running ? "yellow" : "lav"}`}>
            {snapshot.running ? "작업 신호 수신" : "출근 대기"}
          </span>
        </div>
        <p className="risk-qa-note">
          직원 수는 Hermes Profile 매핑을 기준으로 표시하고, 작업 수는 현재 AI Office 엔진의 Agent 상태에서 계산합니다. 실제 Risk/QA API 실행 여부는 별도 Runtime Bridge가 연결된 뒤에만 실시간으로 표시합니다.
        </p>
        <div className="risk-qa-metrics" role="status" aria-label="Risk와 QA 작업 요약">
          <div>
            <span>프로필 직원</span>
            <b>{totalEmployees}명</b>
          </div>
          <div>
            <span>현재 작업 중</span>
            <b>{totalWorking}명</b>
          </div>
          <div>
            <span>오피스 상태</span>
            <b>{snapshot.running ? "실행 중" : "대기"}</b>
          </div>
        </div>
        <div className="risk-qa-policy" role="status">
          <span>재시도 상한</span>
          <b>{RISK_QA_RETRY_POLICY.maxRetries}회 (총 {RISK_QA_RETRY_POLICY.maxAttempts}회)</b>
          <span>Risk 실패</span>
          <b>{RISK_QA_RETRY_POLICY.riskFallback}</b>
          <span>QA 실패</span>
          <b>{RISK_QA_RETRY_POLICY.qaFallback}</b>
        </div>
        <div className="risk-qa-log-flow" aria-label="로그 이벤트 흐름">
          <span>LOG/REPLAY</span>
          {RISK_QA_LOG_EVENTS.map((eventType) => (
            <code key={eventType}>{eventType}</code>
          ))}
        </div>
        <div className="risk-qa-departments">
          {activities.map(({ department, activity }) => (
            <article className="risk-qa-department" key={department.id}>
              <div className="risk-qa-department-heading">
                <div>
                  <span className="tiny-label">{department.domain}</span>
                  <h3>{department.name}</h3>
                </div>
                <span className={`status-pill ${statusClass[activity.statusLabel] ?? "waiting"}`}>
                  {activity.statusLabel}
                </span>
              </div>
              <p className="risk-qa-contract">
                <b>{department.orchestrator} → {department.employeeExecutor}</b>
                <br />
                <code>{department.runtimeContract}</code>
              </p>
              <div className="risk-qa-live-summary">
                <strong>{activity.workingCount}/{department.employees.length}명 작업 중 · {activity.onDutyCount}명 출근</strong>
                <span>{activity.taskLabel}</span>
              </div>
              <div className="risk-qa-staff" aria-label={`${department.name} 직원 목록`}>
                {activity.employees.map(({ employee, agent }) => (
                  <div className="risk-qa-employee" key={employee.profileId}>
                    <div>
                      <b>{employee.name}</b>
                      <span>{employee.role}</span>
                    </div>
                    <code>{employee.profileId}</code>
                    <span className={`risk-qa-agent-status ${statusClass[agent?.status ?? "상태 미수신"] ?? "waiting"}`}>
                      {agent?.status ?? "상태 미수신"}
                    </span>
                    <small>{compactSkills(employee.skills)}</small>
                    <small className="risk-qa-agent-task">
                      {agent?.taskLabel ?? "현재 Office Agent 상태를 받지 못했습니다."}
                    </small>
                  </div>
                ))}
              </div>
            </article>
          ))}
        </div>
        <p className="dash-note risk-qa-source">
          Source: <code>departments/03-risk/hermes/config.yaml</code> · <code>departments/06-ai-qa-audit/hermes/config.yaml</code>
          <br />
          작업 중 표시는 AI Office 시뮬레이션 상태입니다. 실제 금융 상태와 주문·원장·Risk Limit 변경은 각 Domain API와 결정론적 엔진이 소유합니다.
        </p>
      </div>
    </section>
  );
}
