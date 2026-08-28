"use client";

import {
  HERMES_KANBAN_COLUMNS,
  type HermesKanbanBoard as HermesKanbanBoardData,
  type HermesKanbanCard,
  type HermesKanbanColumn,
} from "../lib/kanbanClient";

const COLUMN_DEFINITIONS: Record<
  HermesKanbanColumn,
  { label: string; helper: string; tone: string; icon: string }
> = {
  todo: {
    label: "TODO",
    helper: "아직 시작하지 않은 작업",
    tone: "border-outline-variant bg-surface-container-low",
    icon: "inbox",
  },
  ready: {
    label: "READY",
    helper: "실행을 기다리는 작업",
    tone: "border-primary/30 bg-secondary-container/40",
    icon: "schedule",
  },
  inprogress: {
    label: "IN PROGRESS",
    helper: "Hermes가 처리 중인 작업",
    tone: "border-tertiary-fixed-dim/60 bg-tertiary-fixed/10",
    icon: "sync",
  },
  done: {
    label: "DONE",
    helper: "완료로 기록된 작업",
    tone: "border-tertiary-fixed-dim/60 bg-tertiary-fixed/10",
    icon: "task_alt",
  },
};

const ASSIGNEE_LABELS: Record<string, string> = {
  "ceo-agent": "CEO Office",
  "hr-department": "HR",
  "research-department": "Research",
  "quant-backtest-department": "Quant / Backtest",
  "trading-department": "Trading",
  "risk-management": "Risk",
  "accounting-portfolio-department": "Accounting / Portfolio",
  "qa-department": "AI QA / Audit",
};

function formatAssignee(value: string): string {
  return (
    ASSIGNEE_LABELS[value] ??
    value.replace(/-department$/, "").replace(/-/g, " ")
  );
}

function formatTaskTime(value: HermesKanbanCard["created_at"]): string {
  if (value === null || value === undefined || value === "") return "시각 없음";

  const numeric = typeof value === "number" ? value : Number(value);
  const date = Number.isFinite(numeric)
    ? new Date(numeric < 1_000_000_000_000 ? numeric * 1000 : numeric)
    : new Date(value);
  if (Number.isNaN(date.getTime())) return "시각 없음";

  return date.toLocaleString("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function statusTone(status: string): string {
  if (["blocked", "failed", "error"].includes(status)) {
    return "border-error/40 bg-error-container text-on-error-container";
  }
  if (["done", "completed", "archived"].includes(status)) {
    return "border-tertiary-fixed-dim/60 bg-tertiary-fixed/20 text-on-tertiary-fixed-variant";
  }
  if (status === "ready") return "border-primary/30 bg-secondary-container text-primary";
  return "border-outline-variant bg-surface-container text-on-surface-variant";
}

function HermesKanbanCardView({ card }: { card: HermesKanbanCard }) {
  return (
    <article
      className="rounded-lg border border-outline-variant bg-surface-container-lowest p-3 shadow-sm"
      data-read-only="true"
    >
      <div className="flex items-start justify-between gap-2">
        <span
          className={`rounded-full border px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide ${statusTone(card.status)}`}
        >
          Hermes · {card.status}
        </span>
        <span className="material-symbols-outlined text-[16px] text-outline" aria-hidden="true">
          lock
        </span>
      </div>
      <h4 className="mt-2 text-body-sm font-body-sm font-semibold leading-5 text-on-surface">
        {card.title}
      </h4>
      <div className="mt-3 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-on-surface-variant">
        <span className="font-medium">{formatAssignee(card.assignee)}</span>
        <span aria-hidden="true">·</span>
        <time dateTime={String(card.created_at ?? "")}>
          {formatTaskTime(card.created_at)}
        </time>
      </div>
      <code className="mt-2 block truncate text-[10px] text-outline" title={card.task_id}>
        {card.task_id}
      </code>
    </article>
  );
}

export default function HermesKanbanBoard({
  board,
  error,
  loading,
  kanbanUrl,
}: {
  board: HermesKanbanBoardData | null;
  error: string;
  loading: boolean;
  kanbanUrl: string | null;
}) {
  return (
    <section
      className="bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden shadow-sm"
      aria-label="Hermes Agent Kanban read-only board"
      data-read-only="true"
    >
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-3">
        <div className="flex min-w-0 items-center gap-2">
          <span className="material-symbols-outlined text-[18px] text-primary" aria-hidden="true">
            account_tree
          </span>
          <div className="min-w-0">
            <h2 className="truncate text-title-md font-title-md text-primary">
              Hermes Agent Kanban
            </h2>
          </div>
        </div>
        <div className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-wide">
          {kanbanUrl ? (
            <a
              href={kanbanUrl}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 rounded-full border border-outline-variant bg-surface-container-lowest px-2 py-1 text-on-surface-variant transition-colors hover:bg-surface-container"
              aria-label="Hermes Kanban을 새 창으로 열기"
            >
              보드 새 창으로 열기
              <span className="material-symbols-outlined text-[14px]" aria-hidden="true">
                open_in_new
              </span>
            </a>
          ) : null}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 px-4 py-3 text-xs text-on-surface-variant">
        <span>원본 Kanban 상태는 카드의 Hermes 배지에서 확인할 수 있습니다.</span>
        <span className="font-data-mono">
          {loading
            ? "SYNCING"
            : board
              ? `UPDATED ${formatTaskTime(board.observed_at)}`
              : "WAITING"}
        </span>
      </div>

      {error ? (
        <p role="alert" className="mx-4 mb-4 rounded border border-error/40 bg-error-container px-3 py-2 text-body-sm text-on-error-container">
          {error}
        </p>
      ) : null}

      <div className="grid grid-cols-1 gap-3 px-4 pb-4 md:grid-cols-2 xl:grid-cols-4">
        {HERMES_KANBAN_COLUMNS.map((columnKey) => {
          const definition = COLUMN_DEFINITIONS[columnKey];
          const cards = board?.columns[columnKey] ?? [];
          return (
            <section
              key={columnKey}
              className={`min-h-52 rounded-lg border p-3 ${definition.tone}`}
              aria-label={`${definition.label} Hermes tasks`}
            >
              <div className="flex items-start justify-between gap-2">
                <div>
                  <div className="flex items-center gap-1.5">
                    <span className="material-symbols-outlined text-[16px] text-on-surface-variant" aria-hidden="true">
                      {definition.icon}
                    </span>
                    <h3 className="text-label-md font-label-md text-on-surface">{definition.label}</h3>
                  </div>
                  <p className="mt-1 text-[11px] text-on-surface-variant">{definition.helper}</p>
                </div>
                <span className="rounded-full border border-outline-variant bg-surface-container-lowest px-2 py-0.5 font-data-mono text-xs text-on-surface">
                  {cards.length}
                </span>
              </div>

              <div className="mt-3 flex max-h-[28rem] flex-col gap-2 overflow-y-auto pr-1">
                {cards.length > 0 ? (
                  cards.map((card) => <HermesKanbanCardView key={card.task_id} card={card} />)
                ) : (
                  <p className="rounded border border-dashed border-outline-variant px-3 py-6 text-center text-xs text-outline">
                    {loading ? "불러오는 중" : "현재 작업 없음"}
                  </p>
                )}
              </div>
            </section>
          );
        })}
      </div>
    </section>
  );
}
