"use client";

import { RISK_QA_CONNECTION, RISK_QA_RETRY_POLICY } from "./riskQaBridge";

function compactSkills(skills: readonly string[]): string {
  return skills.join(" · ");
}

export default function RiskQaPanel() {
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
            <h2 id="risk-qa-title">Risk · QA 연결 상태</h2>
          </div>
          <span className="mini-badge mint">PROFILE WIRED</span>
        </div>
        <p className="risk-qa-note">
          Hermes Profile과 직원 코드만 연결된 읽기/검증용 Projection입니다. 주문 제출·원장 기록·Risk Limit 변경은 이 화면에
          없습니다.
        </p>
        <div className="risk-qa-policy" role="status">
          <span>재시도 상한</span>
          <b>{RISK_QA_RETRY_POLICY.maxRetries}회 (총 {RISK_QA_RETRY_POLICY.maxAttempts}회)</b>
          <span>Risk 실패</span>
          <b>{RISK_QA_RETRY_POLICY.riskFallback}</b>
          <span>QA 실패</span>
          <b>{RISK_QA_RETRY_POLICY.qaFallback}</b>
        </div>
        <div className="risk-qa-departments">
          {RISK_QA_CONNECTION.map((department) => (
            <article className="risk-qa-department" key={department.id}>
              <div className="risk-qa-department-heading">
                <div>
                  <span className="tiny-label">{department.domain}</span>
                  <h3>{department.name}</h3>
                </div>
                <span className="status-pill done">연결됨</span>
              </div>
              <p className="risk-qa-contract">
                <code>{department.runtimeContract}</code>
              </p>
              <div className="risk-qa-staff" aria-label={`${department.name} 직원 목록`}>
                {department.employees.map((employee) => (
                  <div className="risk-qa-employee" key={employee.profileId}>
                    <div>
                      <b>{employee.name}</b>
                      <span>{employee.role}</span>
                    </div>
                    <code>{employee.profileId}</code>
                    <small>{compactSkills(employee.skills)}</small>
                  </div>
                ))}
              </div>
            </article>
          ))}
        </div>
        <p className="dash-note risk-qa-source">
          Source: <code>departments/03-risk/hermes/config.yaml</code> · <code>departments/06-ai-qa-audit/hermes/config.yaml</code>
          <br />
          이 카드는 실시간 금융 상태를 의미하지 않습니다. 런타임 장애·미연결은 해당 부서 API 로그와 하네스 결과에서 확인합니다.
        </p>
      </div>
    </section>
  );
}
