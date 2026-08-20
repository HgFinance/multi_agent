import { bffFetch } from "./bffClient";

export const HERMES_KANBAN_COLUMNS = [
  "todo",
  "ready",
  "inprogress",
  "done",
] as const;

export type HermesKanbanColumn = (typeof HERMES_KANBAN_COLUMNS)[number];

export type HermesKanbanCard = {
  task_id: string;
  title: string;
  assignee: string;
  status: string;
  created_at: number | string | null;
};

export type HermesKanbanColumns = Record<
  HermesKanbanColumn,
  HermesKanbanCard[]
>;

export type HermesKanbanBoard = {
  schema_version: "hermes.agent-kanban.v1";
  source: "hermes-kanban";
  read_only: true;
  observed_at: string;
  columns: HermesKanbanColumns;
};

function explainKanbanError(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return `Hermes Kanban 연결 실패 (HTTP ${status})`;
}

/** BFF가 Hermes CLI로 읽은 보드의 최소 read-only projection만 가져온다. */
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isKanbanCard(value: unknown): value is HermesKanbanCard {
  if (!isRecord(value)) return false;

  const createdAt = value.created_at;
  return (
    typeof value.task_id === "string" &&
    typeof value.title === "string" &&
    typeof value.assignee === "string" &&
    typeof value.status === "string" &&
    (createdAt === null || typeof createdAt === "string" || typeof createdAt === "number")
  );
}

function isHermesKanbanBoard(value: unknown): value is HermesKanbanBoard {
  if (!isRecord(value)) return false;
  if (
    value.schema_version !== "hermes.agent-kanban.v1" ||
    value.source !== "hermes-kanban" ||
    value.read_only !== true ||
    typeof value.observed_at !== "string" ||
    !isRecord(value.columns)
  ) {
    return false;
  }

  return HERMES_KANBAN_COLUMNS.every((column) => {
    const cards = value.columns[column];
    return Array.isArray(cards) && cards.every(isKanbanCard);
  });
}

export async function fetchHermesKanban(): Promise<HermesKanbanBoard> {
  let response: Response;
  try {
    response = await bffFetch("/ui/ceo/kanban", {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch {
    throw new Error("Hermes Kanban(BFF)에 연결하지 못했습니다.");
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainKanbanError(body, response.status));

  if (!isHermesKanbanBoard(body)) {
    throw new Error("Hermes Kanban 응답 계약이 올바르지 않습니다.");
  }
  return body;
}
