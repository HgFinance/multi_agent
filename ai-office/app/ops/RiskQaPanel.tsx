"use client";

import { useBffFeed } from "./bffClient";
import type { OperationsDepartment } from "./readModel";
import { readableRuntimeMessage } from "./statusLabels";

const STATUS_TONE: Record<string, string> = {
  RUNNING: "working",
  QUEUED: "waiting",
  IDLE: "waiting",
  OFFLINE: "blocked",
  DEGRADED: "approval",
  BLOCKED: "blocked",
  ERROR: "blocked",
};

const STATUS_LABEL: Record<string, string> = {
  RUNNING: "업무 중",
  QUEUED: "실행 대기",
  IDLE: "대기",
  OFFLINE: "미연결",
  DEGRADED: "안전 보류",
  BLOCKED: "실행 차단",
  ERROR: "오류",
};

function DepartmentCard({ department }: { department: OperationsDepartment }) {
  const status = String(department.status).toUpperCase();
  return (
    <article className="all-department-card">
      <div className="all-department-heading">
        <div>
          <span className="tiny-label">{department.domain}</span>
          <h3>{department.name}</h3>
          <code>{department.department_code}</code>
        </div>
        <span className={`status-pill ${STATUS_TONE[status] ?? "waiting"}`}>{STATUS_LABEL[status] ?? status}</span>
      </div>
      <div className="all-department-meta">
        <span><b>{department.active_worker_count}</b>/{department.worker_count} active</span>
        <span>LLM {department.llm_worker_count} · Runner {department.deterministic_worker_count}</span>
        <span>{department.current_stage ?? "대기"}</span>
      </div>
      <p>{readableRuntimeMessage(department.status_reason).summary}</p>
      <small>{department.executor ?? "LangGraph"} · {department.worker_model ?? "qwen3:1.7b"} · {department.output_contract ?? "worker-context.v1"}</small>
    </article>
  );
}

export default function DepartmentRuntimePanel() {
  const { snapshot } = useBffFeed();
  const operations = snapshot?.operations;
  const departments = operations?.departments ?? [];
  const activeWorkers = departments.reduce((total, item) => total + item.active_worker_count, 0);
  const registeredWorkers = departments.reduce((total, item) => total + item.worker_count, 0);
  const degradedDepartments = departments.filter((item) => ["DEGRADED", "BLOCKED", "ERROR"].includes(item.status)).length;

  return (
    <section className="win all-department-panel" aria-labelledby="all-department-title">
      <div className="win-bar">
        <span>🧩 all_departments.runtime_projection</span>
        <span className="window-controls" aria-hidden="true">— ▢ ✕</span>
      </div>
      <div className="win-body">
        <div className="section-heading">
          <div>
            <p className="eyebrow">BACKEND READ MODEL · 8 DEPARTMENTS</p>
            <h2 id="all-department-title">전체 부서 실행 현황</h2>
          </div>
          <span className={`mini-badge ${operations?.runtime_connected ? "live" : "idle"}`}>
            {operations?.runtime_connected ? "RUNTIME CONNECTED" : "DEGRADED"}
          </span>
        </div>
        <p className="all-department-note">Risk·QA 전용 화면이 아니라 CEO Office, HR, Research, Trading, Risk, Quant/Backtest, Accounting/Portfolio, AI QA/Audit의 전체 Registry와 현재 runtime 상태를 한 번만 표시합니다.</p>
        <div className="all-department-metrics" aria-label="전체 부서 요약">
          <span>부서 <b>{departments.length || 8}</b></span>
          <span>등록 직원 <b>{registeredWorkers}</b></span>
          <span>실행 중 <b>{activeWorkers}</b></span>
          <span>보류·오류 <b>{degradedDepartments}</b></span>
        </div>
        {departments.length > 0 ? (
          <div className="all-department-grid" aria-label="8개 부서 목록">
            {departments.map((department) => <DepartmentCard key={department.department_code} department={department} />)}
          </div>
        ) : (
          <div className="backend-empty-state" role="status">
            <strong>BFF 부서 Registry를 기다리는 중입니다.</strong>
            <p>연결되면 8개 부서의 Worker 수와 LangGraph 상태가 표시됩니다.</p>
          </div>
        )}
        <p className="dash-note all-department-source">실행 상태 Source: BFF `/ui/snapshot` · 부서 수와 Worker 수 Source: 각 Hermes Profile의 Worker Registry</p>
      </div>
    </section>
  );
}
