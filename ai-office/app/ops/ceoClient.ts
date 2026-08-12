import { BFF } from "./readModel";

export type CeoPlanning = {
  selected_departments: string[];
  steps: string[];
  qa_required: boolean;
  summary: string | null;
};

export type CeoQueryResult = {
  // v1 is the PR #224 default; v2 is an additive planning projection.
  schema_version: "ceo.query-accepted.v1" | "ceo.query-accepted.v2";
  department: "ceo-agent";
  binding: false;
  task_id: string;
  status?: "planned" | "accepted";
  answer: string;
  planning?: CeoPlanning | null;
  session_id: string | null;
  task: {
    task_id: string | null;
    status: string;
    source: "hermes-kanban";
  } | null;
};

function explainError(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return `CEO Hermes 연결 실패 (HTTP ${status})`;
}

/** 카드 한 장의 결말. Kanban의 실행 상태와 일부러 분리한다. */
export type CardOutcome =
  | "QUEUED"
  | "RUNNING"
  | "ANSWERED"
  | "NO_ANSWER"
  | "BLOCKED"
  | "FAILED"
  | "STALE"
  | "NO_ASSIGNEE";

export type CeoQueryCard = {
  task_id: string;
  title: string;
  department: string;
  outcome: CardOutcome;
  summary: string;
  has_result: boolean;
  depends_on: string[];
  is_root: boolean;
};

export type CeoQueryProgress = {
  schema_version: "ceo.query-progress.v1";
  root_task_id: string;
  total: number;
  finished: number;
  all_terminal: boolean;
  answer_grounded: boolean;
  unusable: CeoQueryCard[];
  stalled: CeoQueryCard[];
  cards: CeoQueryCard[];
};

type TaskStatusResponse = {
  task_id: string;
  root_task_id: string;
  status: string;
  progress: {
    primary_total: number;
    primary_done: number;
    qa: string;
    synthesis: string;
  };
};

type TaskGraphResponse = {
  root: string;
  nodes: Array<{
    id: string;
    department: string;
    status: string;
    role: string;
    title: string;
  }>;
  edges: Array<[string, string]>;
};

type TaskResultResponse = {
  status: "processing" | "completed";
  result: { summary: string } | null;
  departments: Record<string, string>;
  qa_verdict: string | null;
  block_reason: string | null;
};

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${BFF}${path}`, {
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  return body as T;
}

export async function askCeo(query: string, requestId?: string): Promise<CeoQueryResult> {
  const response = await fetch(`${BFF}/ui/ceo/ask`, {
    method: "POST",
    cache: "no-store",
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      query,
      request_id: requestId ?? crypto.randomUUID(),
    }),
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  if (typeof body !== "object" || body === null) {
    throw new Error("CEO Hermes 응답 계약이 올바르지 않습니다.");
  }
  const result = body as Partial<CeoQueryResult>;
  if (
    (result.schema_version !== "ceo.query-accepted.v1" &&
      result.schema_version !== "ceo.query-accepted.v2") ||
    typeof result.answer !== "string" ||
    typeof result.task_id !== "string"
  ) {
    throw new Error("CEO Hermes 응답 계약이 올바르지 않습니다.");
  }
  return result as CeoQueryResult;
}

function outcomeFor(
  node: TaskGraphResponse["nodes"][number],
  summary: string,
  isRoot: boolean,
): CardOutcome {
  if (isRoot) return "QUEUED";
  const status = node.status.toLowerCase();
  if (status === "failed" || status === "error") return "FAILED";
  if (status === "blocked") return "BLOCKED";
  if (["queued", "ready", "todo", "created", "claimed"].includes(status)) {
    return "QUEUED";
  }
  if (status === "running" || status === "retrying") return "RUNNING";
  if (["done", "completed", "archived"].includes(status)) {
    return summary.trim() ? "ANSWERED" : "NO_ANSWER";
  }
  return node.department ? "STALE" : "NO_ASSIGNEE";
}

/** Read the PR #224 graph/result contracts and retain the old card projection. */
export async function ceoProgress(rootTaskId: string): Promise<CeoQueryProgress> {
  const encoded = encodeURIComponent(rootTaskId);
  const [status, graph, result] = await Promise.all([
    getJson<TaskStatusResponse>(`/ui/ceo/tasks/${encoded}`),
    getJson<TaskGraphResponse>(`/ui/ceo/tasks/${encoded}/graph`),
    getJson<TaskResultResponse>(`/ui/ceo/tasks/${encoded}/result`),
  ]);

  const summaryFor = (node: TaskGraphResponse["nodes"][number]): string => {
    if (node.id === graph.root && result.result?.summary) return result.result.summary;
    return result.departments[node.department] ?? "";
  };
  const cards = graph.nodes.map((node) => {
    const summary = summaryFor(node);
    const isRoot = node.id === graph.root || node.role === "root";
    return {
      task_id: node.id,
      title: node.title || node.department,
      department: node.department,
      outcome: outcomeFor(node, summary, isRoot),
      summary,
      has_result: Boolean(summary.trim()),
      depends_on: graph.edges.filter(([, child]) => child === node.id).map(([parent]) => parent),
      is_root: isRoot,
    } satisfies CeoQueryCard;
  });
  const workerCards = cards.filter((card) => !card.is_root);
  const terminal = new Set<CardOutcome>([
    "ANSWERED",
    "NO_ANSWER",
    "BLOCKED",
    "FAILED",
    "STALE",
    "NO_ASSIGNEE",
  ]);
  const unusable = workerCards.filter((card) =>
    ["NO_ANSWER", "FAILED", "BLOCKED", "STALE", "NO_ASSIGNEE"].includes(card.outcome),
  );
  const stalled = workerCards.filter((card) => ["QUEUED", "RUNNING"].includes(card.outcome));
  return {
    schema_version: "ceo.query-progress.v1",
    root_task_id: status.root_task_id || graph.root,
    total: workerCards.length,
    finished: workerCards.filter((card) => terminal.has(card.outcome)).length,
    all_terminal:
      workerCards.length > 0
        ? workerCards.every((card) => terminal.has(card.outcome))
        : status.status === "completed",
    answer_grounded: Boolean(result.result?.summary) && result.qa_verdict !== "FAIL",
    unusable,
    stalled,
    cards,
  };
}
