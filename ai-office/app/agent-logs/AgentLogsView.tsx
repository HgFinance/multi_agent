"use client";

import { useEffect, useMemo, useState } from "react";
import {
  fetchOperations,
  readableRuntimeMessage,
  type OperationsDepartment,
  type OperationsView,
} from "../lib/operationsClient";
import DepartmentInspector from "./DepartmentInspector";

/**
 * Agent Logs — 페이지 상단(전체 부서 실행 현황).
 *
 * 데이터·판정은 main의 `ops/RiskQaPanel.tsx` 로직 그대로이고, 겉모습만
 * 우리 디자인 토큰으로 옮겼다. 부서 수·Worker 수의 출처는 각 Hermes Profile의
 * Worker Registry이고 실행 상태는 BFF `/ui/snapshot` 이다 — 화면에서 합치지 않는다.
 */

const STATUS_VIEW: Record<string, { label: string; tone: string }> = {
  RUNNING: { label: "업무 중", tone: "border-primary/30 bg-secondary-container text-primary" },
  QUEUED: { label: "실행 대기", tone: "border-outline-variant bg-surface-container text-on-surface-variant" },
  IDLE: { label: "대기", tone: "border-outline-variant bg-surface-container text-on-surface-variant" },
  WAITING_APPROVAL: { label: "승인 대기", tone: "border-primary/30 bg-secondary-container text-primary" },
  OFFLINE: { label: "미연결", tone: "border-outline-variant bg-surface-container-high text-on-surface-variant" },
  DEGRADED: { label: "안전 보류", tone: "border-error/40 bg-error-container text-on-error-container" },
  BLOCKED: { label: "실행 차단", tone: "border-error/40 bg-error-container text-on-error-container" },
  ERROR: { label: "오류", tone: "border-error/40 bg-error-container text-on-error-container" },
};

const EMPTY_DEPARTMENTS: OperationsDepartment[] = [];

function DepartmentCard({
  department,
  selected,
  onSelect,
}: {
  department: OperationsDepartment;
  selected: boolean;
  onSelect: () => void;
}) {
  const status = String(department.status).toUpperCase();
  const view = STATUS_VIEW[status] ?? { label: status, tone: "border-outline-variant bg-surface-container text-on-surface-variant" };
  const message = readableRuntimeMessage(department.status_reason);

  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={selected}
      aria-controls="department-inspector"
      className={`text-left bg-surface-container-lowest border rounded-lg p-4 flex flex-col gap-3 transition-colors hover:bg-surface-container ${
        selected ? "border-primary ring-1 ring-primary" : "border-outline-variant"
      }`}
    >
      <div className="flex justify-between items-start gap-2">
        <div className="min-w-0">
          <span className="block text-label-md font-label-md text-on-surface-variant uppercase">{department.domain}</span>
          <h3 className="text-body-lg font-body-lg font-bold text-primary mt-1">{department.name}</h3>
          <code className="text-xs text-outline">{department.department_code}</code>
        </div>
        <span className={`shrink-0 px-2 py-0.5 rounded-full border text-xs font-medium ${view.tone}`}>{view.label}</span>
      </div>

      <div className="flex flex-wrap gap-1.5 text-xs">
        <span className="px-2 py-1 rounded border border-outline-variant bg-surface text-on-surface-variant">
          <b className="font-data-mono text-on-surface">{department.active_worker_count}</b>/
          <span className="font-data-mono">{department.worker_count}</span> active
        </span>
        <span className="px-2 py-1 rounded border border-outline-variant bg-surface text-on-surface-variant">
          LLM <span className="font-data-mono">{department.llm_worker_count}</span> · Runner{" "}
          <span className="font-data-mono">{department.deterministic_worker_count}</span>
        </span>
        <span className="px-2 py-1 rounded border border-outline-variant bg-surface text-on-surface-variant">
          {department.current_stage ?? "대기"}
        </span>
      </div>

      <p className="text-body-sm font-body-sm text-on-surface-variant m-0">{message.summary}</p>
      {message.action ? <p className="text-xs text-outline m-0">{message.action}</p> : null}
      {/* executor·model·contract 줄은 카드에서 빼고 선택 상세(DepartmentInspector)로 옮겼다. */}
    </button>
  );
}

export default function AgentLogsView() {
  const [data, setData] = useState<OperationsView | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [selectedCode, setSelectedCode] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    fetchOperations()
      .then((next) => alive && setData(next))
      .catch((cause) => alive && setError(cause instanceof Error ? cause.message : String(cause)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, []);

  const departments = data?.departments ?? EMPTY_DEPARTMENTS;
  const registeredWorkers = departments.reduce((total, item) => total + item.worker_count, 0);
  const activeWorkers = departments.reduce((total, item) => total + item.active_worker_count, 0);
  const degraded = departments.filter((item) => ["DEGRADED", "BLOCKED", "ERROR"].includes(item.status)).length;

  // 선택한 부서. 아직 안 눌렀으면 아무것도 펼치지 않는다.
  const selected = useMemo(
    () => departments.find((item) => item.department_code === selectedCode) ?? null,
    [departments, selectedCode],
  );

  const metrics = [
    { label: "부서", value: departments.length || 8 },
    { label: "등록 직원", value: registeredWorkers },
    { label: "실행 중", value: activeWorkers },
    { label: "보류·오류", value: degraded },
  ];

  return (
    <main className="flex-1 w-full max-w-app mx-auto p-margin-mobile md:p-margin-desktop flex flex-col gap-gutter">
      <section className="flex justify-between items-start gap-gutter flex-wrap">
        <div className="min-w-0">
          <p className="text-label-md font-label-md text-on-surface-variant uppercase">
            Backend Read Model · 8 Departments
          </p>
          <h1 className="text-headline-lg font-headline-lg text-primary font-bold tracking-tight mt-2">
            전체 부서 실행 현황
          </h1>
          <p className="text-body-sm font-body-sm text-on-surface-variant mt-2 max-w-3xl">
            HgFinance 전체 부서의 Registry와 현재 runtime 상태를 표시합니다.
          </p>
        </div>
        <span
          className={`shrink-0 px-3 py-1 rounded-full border text-label-md font-label-md ${
            data?.runtime_connected
              ? "border-tertiary-fixed-dim bg-tertiary-fixed/30 text-on-tertiary-fixed-variant"
              : "border-outline text-on-surface-variant"
          }`}
        >
          {data?.runtime_connected ? "RUNTIME CONNECTED" : "DEGRADED"}
        </span>
      </section>

      <section className="flex flex-wrap gap-2" aria-label="전체 부서 요약">
        {metrics.map((metric) => (
          <span
            key={metric.label}
            className="px-4 py-2 rounded border border-outline-variant bg-surface-container-lowest text-body-sm font-body-sm text-on-surface-variant"
          >
            {metric.label} <b className="font-data-mono text-on-surface">{metric.value}</b>
          </span>
        ))}
      </section>

      {departments.length > 0 ? (
        <section className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-4" aria-label="8개 부서 목록">
          {departments.map((department) => (
            <DepartmentCard
              key={department.department_code}
              department={department}
              selected={department.department_code === selectedCode}
              onSelect={() =>
                setSelectedCode((current) =>
                  current === department.department_code ? null : department.department_code,
                )
              }
            />
          ))}
        </section>
      ) : (
        <section
          role="status"
          className="bg-surface-container-lowest border border-outline-variant rounded-lg p-8 text-center flex flex-col gap-2"
        >
          <strong className="text-body-md font-body-md text-on-surface">
            {loading ? "BFF 부서 Registry를 불러오는 중입니다." : "BFF 부서 Registry를 기다리는 중입니다."}
          </strong>
          <p className="text-body-sm font-body-sm text-on-surface-variant m-0">
            연결되면 8개 부서의 Worker 수와 LangGraph 상태가 표시됩니다.
          </p>
          {error ? <p className="text-xs text-error m-0 mt-2">⚠️ {error}</p> : null}
        </section>
      )}

      {selected && data ? (
        <DepartmentInspector department={selected} data={data} />
      ) : departments.length > 0 ? (
        <p className="text-body-sm font-body-sm text-on-surface-variant">
          부서 카드를 누르면 직원 Registry와 실시간 상태가 아래에 펼쳐집니다.
        </p>
      ) : null}

      <p className="text-xs text-outline">
        실행 상태 Source: BFF <code>/ui/snapshot</code> · 부서 수와 Worker 수 Source: 각 Hermes Profile의 Worker Registry
      </p>
    </main>
  );
}
