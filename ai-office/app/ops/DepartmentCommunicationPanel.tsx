"use client";

import { useMemo, useState } from "react";
import { useBffFeed } from "./bffClient";
import type { OperationsCommunication, OperationsDepartment } from "./readModel";

type Scope = "all" | "internal" | "cross_domain";

const statusTone: Record<string, string> = {
  OFFLINE: "blocked",
  DEGRADED: "approval",
  RUNNING: "working",
  IMPLEMENTED: "done",
  IMPLEMENTED_INTERNAL: "done",
  PLANNED: "waiting",
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
            <code>uvicorn apps.api.main:app --port 8000</code>
          </div>
        ) : (
          <>
            <div className="operations-notice">
              <b>{operations.status}</b>
              <span>
                runtime heartbeat {operations.runtime_connected ? "연결됨" : "미연결"} · event bridge{" "}
                {operations.event_bridge_connected ? "연결됨" : "미연결"}
              </span>
              <span>마지막 BFF 응답 {timeLabel(lastUpdated)}</span>
            </div>

            <div className="department-runtime-list" aria-label="부서별 runtime 상태">
              {operations.departments.map((department) => (
                <DepartmentRow key={department.department_code} department={department} />
              ))}
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
