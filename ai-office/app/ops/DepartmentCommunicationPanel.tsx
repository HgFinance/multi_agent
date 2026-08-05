"use client";

import { useMemo, useState } from "react";
import { useBffFeed } from "./bffClient";
import type { OperationsCommunication, OperationsDepartment, OperationsSnapshot } from "./readModel";

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

type AgentStatus = NonNullable<OperationsSnapshot["agent_statuses"]>[number];

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

function WorkerActivityRow({ agent, departmentName }: { agent: AgentStatus; departmentName: string }) {
  const status = String(agent.status).toUpperCase();
  return (
    <article className="worker-activity-row">
      <div>
        <strong>{agent.role || agent.worker_id || agent.agent_id}</strong>
        <small>{departmentName} · {agent.worker_id || agent.agent_id}</small>
      </div>
      <span className={`status-pill ${statusTone[status] ?? "waiting"}`}>
        {statusLabel[status] ?? status}
      </span>
      <p>{agent.reason || "최근 Worker 상태 이벤트를 수신했습니다."}</p>
    </article>
  );
}

export default function DepartmentCommunicationPanel() {
  const { snapshot, connection, error, lastUpdated, refresh } = useBffFeed();
  const [scope, setScope] = useState<Scope>("all");
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
  const internalMessages = useMemo(
    () => operations?.runtime.messages.filter((message) => message.worker_id).slice(-12).reverse() ?? [],
    [operations?.runtime.messages],
  );

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
              {workerActivity.length > 0 ? (
                <div className="worker-activity-list" aria-label="실제 직원별 작업 상태">
                  {workerActivity.map((agent) => (
                    <WorkerActivityRow
                      key={agent.agent_id}
                      agent={agent}
                      departmentName={departmentNames.get(agent.department_code) ?? agent.department_code}
                    />
                  ))}
                </div>
              ) : (
                <p className="backend-empty-state">
                  실제 Worker status event가 아직 없습니다. Registry 직원은 실행 이벤트를 받은 뒤 이곳에 표시됩니다.
                </p>
              )}
              {internalMessages.length > 0 ? (
                <div className="internal-message-list" aria-label="부서 내부 Worker 메시지">
                  <span className="tiny-label">INTERNAL WORKER MESSAGES</span>
                  {internalMessages.map((message) => (
                    <div className="internal-message-row" key={message.id}>
                      <b>{departmentNames.get(message.department_code ?? "") ?? message.department_code ?? "runtime"}</b>
                      <code>{message.worker_id}</code>
                      <span>{message.text}</span>
                    </div>
                  ))}
                </div>
              ) : null}
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
 {operations.runtime.messages.length > 0 && <div className="communication-list" aria-label="실제 LangGraph runtime 메시지">{operations.runtime.messages.slice(-12).reverse().map((message) => <article className="communication-row" key={message.id}><div className="communication-heading"><code>{message.kind}</code><span className="status-pill done">LIVE</span></div><p>{message.text}</p><small>{message.department_code ?? "runtime"} · {message.worker_id ?? "department-head"}</small></article>)}</div>}
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
