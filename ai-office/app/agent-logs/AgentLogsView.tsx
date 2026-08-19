"use client";

import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import {
  fetchOperations,
  subscribeOperationsStream,
  type OperationsDepartment,
  type OperationsView,
} from "../lib/operationsClient";
import DepartmentInspector from "./DepartmentInspector";
import { KANBAN_BASE_URL, resolveKanbanUrl } from "../lib/kanbanUrl";
import { readDiscordMessages, type DiscordMessage } from "../lib/discordClient";
import HermesKanbanBoard from "./HermesKanbanBoard";
import { fetchHermesKanban, type HermesKanbanBoard as HermesKanbanBoardData } from "../lib/kanbanClient";

/**
 * Agent Logs — 페이지 상단(전체 부서 실행 현황).
 *
 * 데이터·판정은 main의 `ops/RiskQaPanel.tsx` 로직 그대로이고, 겉모습만
 * 우리 디자인 토큰으로 옮겼다. 부서 수·Worker 수의 출처는 각 Hermes Profile의
 * Worker Registry와 실행 상태는 인증된 BFF `/ui/snapshot` polling으로 받는다.
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
        {status !== "OFFLINE" ? (
          <span className={`shrink-0 px-2 py-0.5 rounded-full border text-xs font-medium ${view.tone}`}>{view.label}</span>
        ) : null}
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

      {/* executor·model·contract 줄은 카드에서 빼고 선택 상세(DepartmentInspector)로 옮겼다. */}
    </button>
  );
}

function formatMessageTime(value: string): string {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ko-KR", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function summarizeMessage(value: string, limit = 240): string {
  const compact = value.replace(/<@!?\d+>/g, "@멘션").replace(/\s+/g, " ").trim();
  if (!compact) return "첨부 또는 임베드만 있는 메시지";
  return compact.length > limit ? `${compact.slice(0, limit).trimEnd()}…` : compact;
}

function formatMessageKind(value: string): string {
  return value.replace(/[_-]+/g, " ").trim() || "runtime event";
}

function DiscordChat({ department }: { department: string }) {
  const [messages, setMessages] = useState<DiscordMessage[] | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const controller = new AbortController();
    readDiscordMessages(department, 50, controller.signal)
      .then((body) => setMessages(body.messages))
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(cause instanceof Error ? cause.message : "Discord 대화를 불러오지 못했습니다.");
      });
    return () => controller.abort();
  }, [department]);

  if (error) {
    return (
      <p
        role="alert"
        className="text-body-sm font-body-sm text-on-error-container bg-error-container border border-error/40 rounded px-3 py-2 m-0"
      >
        {error}
      </p>
    );
  }
  if (messages === null) return <p className="text-body-sm font-body-sm text-on-surface-variant m-0">Discord 대화를 불러오는 중입니다…</p>;
  if (messages.length === 0) return <p className="text-body-sm font-body-sm text-on-surface-variant m-0">아직 표시할 Discord 대화가 없습니다.</p>;

  return (
    // 카드 한 장이 한 줄을 차지한다. 2열로 쪼개면 좌우 두 카드의 시각이 뒤섞여
    // 읽는 순서가 사라진다 - 대화는 위에서 아래로 흐르는 것이 읽기 쉽다.
    <div className="flex flex-col gap-3 max-h-96 overflow-y-auto pr-1">
      {messages.map((message) => (
        <article
          key={message.id}
          className={`rounded-lg border p-3 ${
            message.is_department_bot
              ? "border-primary/30 bg-secondary-container/40"
              : "border-outline-variant bg-surface-container-low"
          }`}
        >
          {/* 봇·사용자 배지는 두지 않는다. 카드 배경색이 이미 그 구분을 하고 있어
              같은 사실을 두 번 말하면 정작 읽어야 할 작성자·시각이 묻힌다. */}
          <div className="min-w-0">
            <strong className="block truncate text-body-sm font-body-sm text-on-surface">{message.author}</strong>
            <time className="block text-xs text-outline" dateTime={message.created_at}>
              {formatMessageTime(message.created_at)}
            </time>
          </div>
          <p className="m-0 mt-3 text-body-sm font-body-sm leading-6 text-on-surface">
            {summarizeMessage(message.text)}
          </p>
        </article>
      ))}
    </div>
  );
}

export default function AgentLogsView() {
  const [data, setData] = useState<OperationsView | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [selectedCode, setSelectedCode] = useState<string | null>(null);
  const [streamState, setStreamState] = useState<"connecting" | "connected" | "degraded">("connecting");
  const [lastKeepalive, setLastKeepalive] = useState<string | null>(null);
  const [kanban, setKanban] = useState<HermesKanbanBoardData | null>(null);
  const [kanbanError, setKanbanError] = useState("");
  const [kanbanLoading, setKanbanLoading] = useState(true);
  const pageHost = usePageHost();
  const kanbanUrl = useMemo(
    () => resolveKanbanUrl(KANBAN_BASE_URL, pageHost || undefined),
    [pageHost],
  );

  useEffect(() => {
    let alive = true;
    const refresh = () => {
      fetchOperations()
        .then((next) => alive && setData(next))
        .catch((cause) => alive && setError(cause instanceof Error ? cause.message : String(cause)))
        .finally(() => alive && setLoading(false));
      fetchHermesKanban()
        .then((next) => {
          if (!alive) return;
          setKanban(next);
          setKanbanError("");
        })
        .catch((cause: unknown) => {
          if (!alive) return;
          setKanbanError(cause instanceof Error ? cause.message : String(cause));
        })
        .finally(() => alive && setKanbanLoading(false));
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

  const departments = data?.departments ?? EMPTY_DEPARTMENTS;
  const registeredWorkers = departments.reduce((total, item) => total + item.worker_count, 0);
  const activeWorkers = departments.reduce((total, item) => total + item.active_worker_count, 0);
  const degraded = departments.filter((item) => ["DEGRADED", "BLOCKED", "ERROR"].includes(item.status)).length;

  // 선택한 부서. 아직 안 눌렀으면 아무것도 펼치지 않는다.
  const selected = useMemo(
    () => departments.find((item) => item.department_code === selectedCode) ?? null,
    [departments, selectedCode],
  );
  const discordDepartment = selected ?? departments[0] ?? null;

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
            {streamState === "connected" ? "BFF AUTH POLLING CONNECTED" : streamState === "connecting" ? "BFF POLLING STARTING" : "BFF POLLING DEGRADED"}
          </span>
        </div>
      </section>

      <HermesKanbanBoard board={kanban} error={kanbanError} loading={kanbanLoading} kanbanUrl={kanbanUrl} />

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
          BFF auth polling <b className="font-data-mono text-on-surface">{streamState === "connected" ? "connected" : "waiting"}</b>
        </span>
        <span className="px-4 py-2 rounded border border-outline-variant bg-surface-container-lowest text-body-sm font-body-sm text-on-surface-variant">
          BFF keepalive <b className="font-data-mono text-on-surface">{lastKeepalive ? new Date(lastKeepalive).toLocaleTimeString("ko-KR") : "waiting"}</b>
        </span>
        <span className="px-4 py-2 rounded border border-outline-variant bg-surface-container-lowest text-body-sm font-body-sm text-on-surface-variant">
          agent.status.v1 <b className="font-data-mono text-on-surface">{data ? (data.eventBridgeConnected ? "events observed" : "no live events") : "checking"}</b>
        </span>
      </section>

      {/* 접기는 native `<details>`가 한다 - 열림 상태·키보드·스크린리더가 전부 딸려
          온다. `<details>`에 flex를 주면 닫혀도 내용이 보이므로 레이아웃은 안쪽
          div가 맡는다. `<section aria-label>`은 landmark라 남겨 둔다. */}
      <section className="bg-surface-container-lowest border border-outline-variant rounded-lg p-4" aria-label="부서 내부 메시지">
       <details className="group">
        <summary className="flex justify-between items-center gap-3 flex-wrap cursor-pointer list-none [&::-webkit-details-marker]:hidden">
          <h2 className="text-title-md font-title-md text-primary m-0 flex items-center gap-1.5">
            <span
              className="material-symbols-outlined text-[18px] text-on-surface-variant transition-transform group-open:rotate-180"
              aria-hidden="true"
            >
              expand_more
            </span>
            부서 내부 메시지
          </h2>
          <span className="text-xs text-on-surface-variant">전체 {data?.messages.length ?? 0}개 메시지</span>
        </summary>
        <div className="flex flex-col gap-3 mt-3">
        {data?.messages.length ? (
          <ol className="flex flex-col gap-2 m-0 p-0 list-none">
            {[...data.messages].slice(-8).reverse().map((message) => (
              <li key={message.id} className="rounded-lg border border-outline-variant bg-surface-container-low p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <div className="flex flex-wrap items-center gap-2 text-xs text-on-surface-variant">
                    <span className="rounded-full border border-outline-variant bg-surface-container-lowest px-2 py-0.5 font-semibold">
                      {formatMessageKind(message.kind)}
                    </span>
                    <span>{message.department_code ?? "공용"}</span>
                  </div>
                  <time className="text-xs text-outline" dateTime={message.occurred_at}>
                    {formatMessageTime(message.occurred_at)}
                  </time>
                </div>
                <p className="text-body-sm font-body-sm text-on-surface m-0 mt-2">{summarizeMessage(message.text)}</p>
              </li>
            ))}
          </ol>
        ) : null}
        {discordDepartment ? (
          <div className="border-t border-outline-variant pt-3">
            <div className="flex justify-between items-center gap-3 flex-wrap mb-2">
              <h3 className="text-body-md font-body-md font-bold text-on-surface m-0">Discord 대화</h3>
              <span className="text-xs text-on-surface-variant">{discordDepartment.name}</span>
            </div>
            <DiscordChat key={discordDepartment.department_code} department={discordDepartment.department_code} />
          </div>
        ) : null}
        </div>
       </details>
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
          부서 카드를 누르면 부서 상태와 연결된 결과물이 아래에 펼쳐집니다.
        </p>
      ) : null}

      <p className="text-xs text-outline">
        BFF authenticated polling + keepalive Source: <code>/ui/snapshot</code> · WebSocket은 one-use ticket 도입 전 비활성 · 부서 수와 Worker 수 Source: 각 Hermes Profile의 Worker Registry
      </p>
    </main>
  );
}
