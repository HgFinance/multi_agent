"use client";

import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import {
  fetchOperations,
  readableRuntimeMessage,
  subscribeOperationsStream,
  type OperationsDepartment,
  type OperationsView,
} from "../lib/operationsClient";
import DepartmentInspector from "./DepartmentInspector";
import { KANBAN_BASE_URL, resolveKanbanUrl } from "../lib/kanbanUrl";

/**
 * Agent Logs — 페이지 상단(전체 부서 실행 현황).
 *
 * 데이터·판정은 main의 `ops/RiskQaPanel.tsx` 로직 그대로이고, 겉모습만
 * 우리 디자인 토큰으로 옮겼다. 부서 수·Worker 수의 출처는 각 Hermes Profile의
 * Worker Registry이고 실행 상태는 BFF `/ui/snapshot`과 `/ws/operations`에서 받는다.
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
const NO_SUBSCRIBE = () => () => {};

function usePageHost(): string {
  return useSyncExternalStore(
    NO_SUBSCRIBE,
    () => window.location.hostname,
    () => "",
  );
}

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
  const [streamState, setStreamState] = useState<"connecting" | "connected" | "degraded">("connecting");
  const [lastKeepalive, setLastKeepalive] = useState<string | null>(null);
  const [kanbanState, setKanbanState] = useState<"loading" | "ready" | "error">("loading");

  const pageHost = usePageHost();
  const kanbanUrl = useMemo(() => resolveKanbanUrl(KANBAN_BASE_URL, pageHost || undefined), [pageHost]);
  const kanbanFailed = !kanbanUrl || kanbanState === "error";

  useEffect(() => {
    let alive = true;
    const refresh = () => {
      fetchOperations()
        .then((next) => alive && setData(next))
        .catch((cause) => alive && setError(cause instanceof Error ? cause.message : String(cause)))
        .finally(() => alive && setLoading(false));
    };

    refresh();
    const unsubscribe = subscribeOperationsStream({
      onOpen: () => alive && setStreamState("connected"),
      onSnapshotRequired: refresh,
      onStatus: refresh,
      onKeepalive: (observedAt) => {
        if (!alive) return;
        setLastKeepalive(observedAt || new Date().toISOString());
        setStreamState("connected");
      },
      onError: () => alive && setStreamState("degraded"),
    });
    return () => {
      alive = false;
      unsubscribe();
    };
  }, []);

  useEffect(() => {
    if (!kanbanUrl || kanbanState !== "loading") return undefined;
    const timer = window.setTimeout(() => setKanbanState("error"), 8000);
    return () => window.clearTimeout(timer);
  }, [kanbanState, kanbanUrl]);

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
            Agent Logs
          </h1>
          <p className="text-body-sm font-body-sm text-on-surface-variant mt-2 max-w-3xl">
            BFF snapshot과 수신된 runtime event를 기준으로 부서 Registry와 최근 로그를 표시합니다.
          </p>
        </div>
        <div className="flex items-center gap-2 flex-wrap">
          <span
            className={`shrink-0 px-3 py-1 rounded-full border text-label-md font-label-md ${
              data?.runtime_connected
                ? "border-tertiary-fixed-dim bg-tertiary-fixed/30 text-on-tertiary-fixed-variant"
                : "border-outline text-on-surface-variant"
            }`}
          >
            {data ? (data.runtime_connected ? "RUNTIME CONNECTED" : "RUNTIME NOT OBSERVED") : "RUNTIME CHECKING"}
          </span>
          <span
            className={`shrink-0 px-3 py-1 rounded-full border text-label-md font-label-md ${
              streamState === "connected"
                ? "border-tertiary-fixed-dim bg-tertiary-fixed/30 text-on-tertiary-fixed-variant"
                : "border-outline text-on-surface-variant"
            }`}
          >
            {streamState === "connected" ? "BFF EVENT STREAM CONNECTED" : streamState === "connecting" ? "BFF STREAM CONNECTING" : "BFF STREAM DEGRADED"}
          </span>
        </div>
      </section>

      <section className="bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden shadow-sm flex flex-col">
        <div className="bg-surface-container-low border-b border-outline-variant px-4 py-2.5 flex items-center justify-between gap-2">
          <span className="flex items-center gap-2 text-label-md font-label-md text-on-surface-variant">
            <span className="material-symbols-outlined text-[16px]" aria-hidden="true">dashboard</span>
            Hermes Kanban Dashboard
          </span>
          <span className="flex gap-1.5" aria-hidden="true">
            <span className="w-2.5 h-2.5 rounded-full bg-outline-variant" />
            <span className="w-2.5 h-2.5 rounded-full bg-outline-variant" />
            <span className="w-2.5 h-2.5 rounded-full bg-outline-variant" />
          </span>
        </div>

        <div className="p-6 pb-4 flex justify-between items-start gap-4 flex-wrap">
          <div className="min-w-0">
            <div className="flex items-center gap-2 mb-2 flex-wrap">
              <span className="bg-primary text-on-primary px-2 py-1 rounded text-label-md font-label-md">SOURCE OF TRUTH</span>
              <span className="flex items-center gap-1.5 text-xs text-on-surface-variant">
                <span className="w-2 h-2 rounded-full bg-tertiary-fixed-dim" aria-hidden="true" />
                Hermes
              </span>
            </div>
            <h2 className="text-headline-md font-headline-md text-primary">공용 Task Graph / Kanban</h2>
            <p className="text-body-sm font-body-sm text-on-surface-variant mt-1">
              Agent Logs에서 확인할 업무 배정과 부서별 Task 상태를 이 보드에서 확인합니다.
            </p>
          </div>
          {kanbanUrl ? (
            <a
              href={kanbanUrl}
              target="_blank"
              rel="noreferrer"
              className="px-4 py-2 border border-outline-variant bg-surface-container-lowest rounded font-bold text-label-md font-label-md text-primary hover:bg-surface-container transition-colors inline-flex items-center gap-1 shrink-0"
            >
              보드 새 창으로 열기
              <span className="material-symbols-outlined text-[16px]" aria-hidden="true">open_in_new</span>
            </a>
          ) : null}
        </div>

        <div className="mx-6 mb-6 flex-1 min-h-80 bg-surface-container-low border border-outline-variant rounded relative overflow-auto">
          {kanbanUrl ? (
            <iframe
              title="Hermes Kanban 화면"
              src={kanbanUrl}
              onLoad={() => setKanbanState("ready")}
              onError={() => setKanbanState("error")}
              className="w-full h-[560px] border-0 bg-white"
            />
          ) : null}
          <div className="absolute top-3 right-3 rounded border border-outline-variant bg-surface-container-lowest/95 px-2 py-1 text-xs text-on-surface-variant">
            {kanbanFailed ? "보드를 불러오지 못함" : kanbanState === "loading" ? "보드 불러오는 중…" : "Hermes 화면 표시됨"}
          </div>
          {kanbanFailed ? (
            <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 bg-surface-container-low p-6 text-center">
              <span className="material-symbols-outlined text-[40px] text-outline-variant" aria-hidden="true">account_tree</span>
              <p className="text-body-sm font-body-sm text-on-surface-variant m-0 max-w-lg">
                {kanbanUrl
                  ? "Hermes 보드를 불러오지 못했습니다. 새 창으로 열어 인증 상태와 Hermes 실행 여부를 확인하세요."
                  : "Hermes Kanban 주소 설정이 올바르지 않습니다. 관리자 설정을 확인하세요."}
              </p>
              {kanbanUrl ? <code className="text-xs text-outline bg-surface-container px-2 py-1 rounded">{kanbanUrl}</code> : null}
            </div>
          ) : null}
        </div>
        {kanbanUrl && pageHost && new URL(kanbanUrl).hostname !== pageHost ? (
          <p role="status" className="mx-6 mb-6 -mt-4 text-xs text-error">
            이 페이지({pageHost})와 보드({new URL(kanbanUrl).hostname})의 호스트가 달라 iframe 안에서 로그인 세션이 유지되지 않습니다.
            주소창을 <code className="bg-surface-container px-1 rounded">{new URL(kanbanUrl).hostname}</code>으로 맞춰 접속하거나,
            보드를 새 창으로 여세요.
          </p>
        ) : null}
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
      <section className="flex flex-wrap gap-2" aria-label="Runtime event status">
        <span className="px-4 py-2 rounded border border-outline-variant bg-surface-container-lowest text-body-sm font-body-sm text-on-surface-variant">
          BFF event stream <b className="font-data-mono text-on-surface">{streamState === "connected" ? "connected" : "waiting"}</b>
        </span>
        <span className="px-4 py-2 rounded border border-outline-variant bg-surface-container-lowest text-body-sm font-body-sm text-on-surface-variant">
          BFF keepalive <b className="font-data-mono text-on-surface">{lastKeepalive ? new Date(lastKeepalive).toLocaleTimeString("ko-KR") : "waiting"}</b>
        </span>
        <span className="px-4 py-2 rounded border border-outline-variant bg-surface-container-lowest text-body-sm font-body-sm text-on-surface-variant">
          agent.status.v1 <b className="font-data-mono text-on-surface">{data ? (data.eventBridgeConnected ? "events observed" : "no live events") : "checking"}</b>
        </span>
      </section>

      <section className="bg-surface-container-lowest border border-outline-variant rounded-lg p-4 flex flex-col gap-3" aria-label="Recent agent logs">
        <div className="flex justify-between items-center gap-3 flex-wrap">
          <h2 className="text-title-md font-title-md text-primary m-0">Recent agent logs</h2>
          <span className="text-xs text-on-surface-variant">{data?.messages.length ?? 0} runtime events</span>
        </div>
        {data?.messages.length ? (
          <ol className="flex flex-col gap-2 m-0 p-0 list-none">
            {[...data.messages].slice(-8).reverse().map((message) => (
              <li key={message.id} className="border border-outline-variant rounded-md px-3 py-2">
                <div className="flex flex-wrap gap-x-3 gap-y-1 text-xs text-on-surface-variant">
                  <span className="font-data-mono">{new Date(message.occurred_at).toLocaleTimeString("ko-KR")}</span>
                  <span>{message.kind}</span>
                  <span>{message.department_code ?? "system"}</span>
                </div>
                <p className="text-body-sm font-body-sm text-on-surface m-0 mt-1">{message.text}</p>
              </li>
            ))}
          </ol>
        ) : (
          <p className="text-body-sm font-body-sm text-on-surface-variant m-0">
            현재 수신된 runtime message가 없습니다. Registry snapshot만 표시 중입니다.
          </p>
        )}
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
        BFF event stream + keepalive Source: <code>/ui/snapshot</code> + <code>/ws/operations</code> · 부서 수와 Worker 수 Source: 각 Hermes Profile의 Worker Registry
      </p>
    </main>
  );
}
