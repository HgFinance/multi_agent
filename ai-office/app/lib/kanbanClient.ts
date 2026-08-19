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

  if (
    typeof body !== "object" ||
    body === null ||
    !("columns" in body) ||
    !("observed_at" in body)
  ) {
    throw new Error("Hermes Kanban 응답 계약이 올바르지 않습니다.");
  }
  return body as HermesKanbanBoard;
}
