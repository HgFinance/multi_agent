"use client";

import { useMemo, useState } from "react";
import { useBffFeed } from "./bffClient";
import type { LlmPerformanceMetric, OperationsCommunication, OperationsDepartment } from "./readModel";

type Scope = "all" | "internal" | "cross_domain";

const statusTone: Record<string, string> = {
  IDLE: "waiting",
  QUEUED: "waiting",
  OFFLINE: "blocked",
  DEGRADED: "approval",
  RUNNING: "working",
  WAITING_APPROVAL: "approval",
  BLOCKED: "blocked",
  ERROR: "blocked",
  IMPLEMENTED: "done",
  IMPLEMENTED_INTERNAL: "done",
  PLANNED: "waiting",
};

const statusLabel: Record<string, string> = {
  IDLE: "대기",
  QUEUED: "실행 대기",
  RUNNING: "업무 중",
  WAITING_APPROVAL: "승인 대기",
  BLOCKED: "차단",
  ERROR: "오류",
  DEGRADED: "저하",
  OFFLINE: "미연결",
};

function timeLabel(value: string | null): string {
  if (!value) return "아직 수신하지 않음";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleTimeString("ko-KR");
}

function DepartmentRow({ department }: { department: OperationsDepartment }) {
  return (
    <article className="department-runtime-row">
      <div className="department-runtime-main">
        <strong>{department.name}</strong>
        <code>{department.department_code}</code>
      </div>
      <div className="department-runtime-meta">
        <span className={`status-pill ${statusTone[department.status] ?? "waiting"}`}>
          {department.status}
        </span>
        <span>{department.worker_count} workers</span>
<span>{department.active_worker_count} active in runtime</span>
      </div>
<p>{department.current_stage ? `${department.current_stage} · ` : ""}{department.status_reason}</p>
    </article>
  );
}

function CommunicationRow({ event }: { event: OperationsCommunication }) {
  return (
    <article className="communication-row">
      <div className="communication-heading">
        <code>{event.event_type}</code>
        <span className={`status-pill ${statusTone[event.status] ?? "waiting"}`}>
          {event.status}
        </span>
      </div>
      <p>
        <b>{event.producer}</b>
        <span aria-hidden="true"> → </span>
        {event.consumers.join(", ")}
      </p>
      <small>
        {event.layer} · {event.transport} · source: {event.source}
      </small>
    </article>
  );
}

function departmentStage(department: OperationsDepartment): string {
  return {
    governance: "ceo",
    workforce: "hr",
    accounting: "accounting",
  }[department.domain] ?? department.domain;
}

export default function DepartmentCommunicationPanel() {
  const { snapshot, connection, error, lastUpdated, refresh } = useBffFeed();
  const [scope, setScope] = useState<Scope>("all");
  const [selectedDepartmentCode, setSelectedDepartmentCode] = useState("research-department");
  const operations = snapshot?.operations;
  const events = useMemo(
    () =>
      operations?.communications.filter((event) =>
        scope === "all" ? true : scope === "internal" ? event.layer === "internal" : event.layer !== "internal",
      ) ?? [],
    [operations?.communications, scope],
  );
  const departmentNames = useMemo(
    () => new Map((operations?.departments ?? []).map((department) => [department.department_code, department.name])),
    [operations?.departments],
  );
  const workerActivity = useMemo(
    () => [...(operations?.agent_statuses ?? [])].sort((left, right) => {
      const active = new Set(["RUNNING", "QUEUED", "WAITING_APPROVAL"]);
      return Number(active.has(String(right.status))) - Number(active.has(String(left.status)));
    }),
    [operations?.agent_statuses],
  );
  const performanceMetrics = operations?.runtime.performance_metrics ?? [];
  const selectedDepartment =
    operations?.departments.find((department) => department.department_code === selectedDepartmentCode) ??
    operations?.departments[0];
  const selectedDepartmentCodeResolved = selectedDepartment?.department_code ?? selectedDepartmentCode;
  const selectedLiveAgents = useMemo(
    () => new Map(
      (operations?.agent_statuses ?? [])
        .filter((agent) => agent.department_code === selectedDepartmentCodeResolved)
        .map((agent) => [agent.worker_id ?? agent.agent_id, agent]),
    ),
    [operations?.agent_statuses, selectedDepartmentCodeResolved],
  );
  const selectedWorkers = selectedDepartment?.workers.map((worker) => {
    const live = selectedLiveAgents.get(worker.worker_id);
    const active = operations?.runtime.active_workers.some(
      (item) => item.worker_id === worker.worker_id && item.department_code === selectedDepartmentCodeResolved,
    );
    return {
      ...worker,
      role: live?.role ?? worker.worker_id,
      status: live?.status ?? (active ? "RUNNING" : "REGISTERED"),
      reason: live?.reason ?? (active ? "LangGraph runtime에서 실행 중입니다." : "실행 상태 이벤트 대기 중입니다."),
    };
  }) ?? [];
  const selectedMessages = operations?.runtime.messages
    .filter((message) => message.department_code === selectedDepartmentCodeResolved && message.worker_id)
    .slice(-8)
    .reverse() ?? [];
  const selectedMetrics = performanceMetrics
    .filter((metric) => metric.stage === (selectedDepartment ? departmentStage(selectedDepartment) : ""))
    .slice(-8)
    .reverse();

  return (
    <section className="win department-operations" aria-labelledby="department-operations-title">
      <div className="win-bar">
        <span>🛰 operator_bff.department_runtime</span>
        <span className="window-controls" aria-hidden="true">
          — ▢ ✕
        </span>
      </div>
      <div className="win-body">
        <div className="section-heading">
          <div>
            <p className="eyebrow">BACKEND READ MODEL · 5초 주기</p>
            <h2 id="department-operations-title">부서 상태와 통신 계약</h2>
          </div>
          <div className="filter-tabs" aria-label="BFF 연결 상태">
            <span className={`status-pill ${connection === "connected" ? "done" : "approval"}`}>
              {connection.toUpperCase()}
            </span>
            <button type="button" className="btn-small" onClick={() => void refresh()}>
              새로고침
            </button>
          </div>
        </div>

        {!operations ? (
          <div className="backend-empty-state" role="status">
            <strong>{connection === "offline" ? "BFF에 연결되지 않았습니다" : "BFF 상태를 확인하는 중입니다"}</strong>
            <p>{error || "GET /ui/snapshot 응답을 기다리고 있습니다."}</p>
            <code>uvicorn apps.api.main:app --port 8001</code>
          </div>
        ) : (
          <>
            <div className="operations-notice">
              <b>{operations.status}</b>
              <span>
                runtime heartbeat {operations.runtime_connected ? "연결됨" : "미연결"} · event bridge{" "}
                {operations.event_bridge_connected ? "연결됨" : "미연결"}
              </span>
              <span>LangSmith {operations.runtime.observability?.langsmith?.status ?? "UNKNOWN"}</span>
              <span>마지막 BFF 응답 {timeLabel(lastUpdated)}</span>
            </div>

            <div className="department-runtime-list" aria-label="부서별 runtime 상태">
              {operations.departments.map((department) => (
                <DepartmentRow key={department.department_code} department={department} />
              ))}
            </div>

            <div className="internal-runtime-section" aria-labelledby="internal-runtime-title">
              <div className="communication-toolbar">
                <div>
                  <p className="eyebrow">LIVE AGENT STATUS · agent.status.v1</p>
                  <h3 id="internal-runtime-title">부서 내부 실행 추적 <span>{workerActivity.length}명 관찰됨</span></h3>
                </div>
                <span className="status-pill working">
                  {workerActivity.filter((agent) => agent.status === "RUNNING").length}명 업무 중
                </span>
              </div>
              <div className="department-selector" role="tablist" aria-label="내부 실행을 볼 부서 선택">
                {operations.departments.map((department) => (
                  <button
                    type="button"
                    role="tab"
                    aria-selected={department.department_code === selectedDepartmentCodeResolved}
                    className={department.department_code === selectedDepartmentCodeResolved ? "active" : ""}
                    key={department.department_code}
                    onClick={() => setSelectedDepartmentCode(department.department_code)}
                  >
                    <span>{department.domain.toUpperCase()}</span>
                    <b>{department.name}</b>
                    <small>{department.active_worker_count}/{department.worker_count}</small>
                  </button>
                ))}
              </div>
              {selectedDepartment ? (
                <div className="department-inspector">
                  <div className="department-inspector-heading">
                    <div>
                      <span className="tiny-label">SELECTED DEPARTMENT</span>
                      <h4>{selectedDepartment.name}</h4>
                      <code>{selectedDepartment.department_code}</code>
                    </div>
                    <span className={`status-pill ${statusTone[selectedDepartment.status] ?? "waiting"}`}>
                      {statusLabel[selectedDepartment.status] ?? selectedDepartment.status}
                    </span>
                  </div>
                  <div className="department-inspector-meta">
                    <span>등록 Worker <b>{selectedWorkers.length}</b></span>
                    <span>업무 중 <b>{selectedWorkers.filter((worker) => worker.status === "RUNNING").length}</b></span>
                    <span>내부 메시지 <b>{selectedMessages.length}</b></span>
                    <span>LLM 성과 <b>{selectedMetrics.length}</b></span>
                  </div>
                  <div className="department-inspector-grid">
                    <div>
                      <span className="tiny-label">WORKER REGISTRY + LIVE STATUS</span>
                      <div className="worker-activity-list" aria-label={`실제 직원별 작업 상태 · ${selectedDepartment.name}`}>
                        {selectedWorkers.length > 0 ? selectedWorkers.map((worker) => (
                          <article className="worker-activity-row" key={worker.worker_id}>
                            <div>
                              <strong>{worker.role}</strong>
                              <small>{worker.worker_id} · {worker.trigger ?? "always"}</small>
                            </div>
                            <span className={`status-pill worker-status-pill ${statusTone[worker.status] ?? "waiting"}`}>
                              {statusLabel[worker.status] ?? (worker.status === "REGISTERED" ? "등록됨" : worker.status)}
                            </span>
                            <p>{worker.reason}</p>
                          </article>
                        )) : <p className="backend-empty-state">이 부서의 Worker Registry를 기다리는 중입니다.</p>}
                      </div>
                    </div>
                    <div>
                      <span className="tiny-label">INTERNAL MESSAGES</span>
                      {selectedMessages.length > 0 ? (
                        <div className="internal-message-list">
                          {selectedMessages.map((message) => (
                            <div className="internal-message-row" key={message.id}>
                              <code>{message.worker_id}</code>
                              <span>{message.text}</span>
                            </div>
                          ))}
                        </div>
                      ) : <p className="backend-empty-state">실제 내부 메시지가 아직 없습니다.</p>}
                      <span className="tiny-label department-metric-label">LLM PERFORMANCE · REDACTED</span>
                      {selectedMetrics.length > 0 ? (
                        <div className="llm-metric-list">
                          {selectedMetrics.map((metric: LlmPerformanceMetric) => (
                            <div className="llm-metric-row" key={`${metric.worker_id}-${metric.latency_ms}-${metric.attempts}`}>
                              <b>{metric.worker_id}</b>
                              <span>{metric.model_name}</span>
                              <span>{metric.latency_ms}ms</span>
                              <span>eval {metric.eval_score == null ? "—" : metric.eval_score.toFixed(2)}</span>
                            </div>
                          ))}
                        </div>
                      ) : <p className="backend-empty-state">Worker 실행 후 정량 성과가 표시됩니다.</p>}
                    </div>
                  </div>
                  <p className="dash-note">LangSmith Input/Output 원문은 정책상 비활성화되어 있으며, 정량 메타데이터와 해시 식별자만 추적합니다.</p>
                </div>
              ) : (
                <p className="backend-empty-state">부서 Registry를 기다리는 중입니다.</p>
              )}
            </div>

            <div className="communication-toolbar">
              <div>
                <p className="eyebrow">EVENT REGISTRY</p>
                <h3>
                  부서 통신 <span>{operations.message_count} live messages</span>
                </h3>
              </div>
              <div className="filter-tabs" role="group" aria-label="통신 범위">
                {(
                  [
                    ["all", "전체"],
                    ["internal", "부서 내부"],
                    ["cross_domain", "부서 간"],
                  ] as const
                ).map(([value, label]) => (
                  <button
                    type="button"
                    key={value}
                    className={scope === value ? "active" : ""}
                    onClick={() => setScope(value)}
                  >
                    {label}
                  </button>
                ))}
              </div>
            </div>
<p className="dash-note">
 등록된 Event Contract {operations.implemented_event_contracts}개 · 계획 {operations.planned_event_contracts}개.
 Registry 항목은 실시간 message가 아니며, live event는 연결 후에만 표시됩니다.
 </p>
 {operations.runtime.messages.length > 0 && <div className="communication-list" aria-label="실제 LangGraph runtime 메시지">{operations.runtime.messages.slice(-12).reverse().map((message) => <article className="communication-row" key={message.id}><div className="communication-heading"><code>{message.kind}</code><span className="status-pill done">LIVE</span></div><p>{message.text}</p><small>{departmentNames.get(message.department_code ?? "") ?? message.department_code ?? "runtime"} · {message.worker_id ?? "department-head"}</small></article>)}</div>}
            <div className="communication-list" aria-label="부서간 Event Contract">
              {events.map((event) => (
                <CommunicationRow key={event.event_type} event={event} />
              ))}
            </div>
            {operations.warnings.map((warning) => (
              <p className="dash-note" key={warning}>
                ⚠️ {warning}
              </p>
            ))}
          </>
        )}
      </div>
    </section>
  );
}
