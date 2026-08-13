/**
 * CEO Hermes 질의 클라이언트 — FastAPI BFF `/ui/ceo/ask`.
 *
 * CEO 응답은 advisory projection이다. `binding: false`를 유지하며,
 * 주문·원장·리스크 한도 같은 결정론적 경계를 프론트가 대신하지 않는다.
 *
 * PR #224의 v1 계약과 PR #226의 v2 planning projection을 함께 지원한다.
 * v2 필드는 모두 additive라서 BFF와 프론트의 배포 순서가 바뀌어도 v1
 * 응답을 읽을 수 있다.
 */

import { currentFundId, withAccountHeaders } from "./currentAccount";

/** FastAPI BFF 주소. 배포 Origin이 정해지면 환경변수로 주입한다. */
const configuredBff = process.env.NEXT_PUBLIC_BFF_URL?.trim();
export const BFF = (configuredBff || "http://127.0.0.1:8001").replace(/\/+$/, "");

/** CEO 질의 응답에서 허용하는 하위 호환 스키마. */
export const ACCEPTED_QUERY_VERSIONS = [
  "ceo.query-accepted.v1",
  "ceo.query-accepted.v2",
] as const;

export type CeoQueryPlanning = {
  selected_departments: string[];
  steps: string[];
  qa_required: boolean;
  summary: string | null;
};

/** 기존 호출자의 이름을 보존한다. */
export type CeoPlanning = CeoQueryPlanning;

export type CeoQueryResult = {
  schema_version: (typeof ACCEPTED_QUERY_VERSIONS)[number];
  department: "ceo-agent";
  binding: false;
  task_id: string;
  answer: string;
  session_id: string | null;
  /** v2에만 존재할 수 있는 additive field. */
  status?: "planned" | "accepted";
  planning?: CeoQueryPlanning | null;
  /** PR #224의 기존 응답 projection. */
  task?: {
    task_id: string | null;
    status: string;
    source: "hermes-kanban";
  } | null;
};

/** Kanban status와 분리한 사용자 관점의 카드 결말. */
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
  /** Root는 질문 scope marker이며 본부 결과 집계에서 제외한다. */
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

function explainError(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return `CEO Hermes 연결 실패 (HTTP ${status})`;
}
async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${BFF}${path}`, {
    cache: "no-store",
    // 헤더는 `withAccountHeaders`만 만든다 - 호출부마다 붙이면 한 곳을
    // 빠뜨렸을 때 그 경로만 다른 사용자로 나간다.
    headers: withAccountHeaders({ Accept: "application/json" }),
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  return body as T;
}

export async function askCeo(
  query: string,
  requestId?: string,
): Promise<CeoQueryResult> {
  let response: Response;
  try {
    response = await fetch(`${BFF}/ui/ceo/ask`, {
      method: "POST",
      cache: "no-store",
      headers: withAccountHeaders({
        "Content-Type": "application/json",
        Accept: "application/json",
        ...(requestId ? { "X-Request-Id": requestId } : {}),
      }),
      body: JSON.stringify({
        query,
        request_id: requestId ?? crypto.randomUUID(),
        // 서버에 `user_id -> fund_id` 역참조 경로가 없어(fund_memberships가
        // 비어 있다) 화면이 쌍으로 보낸다. 없으면 생략 - BFF가 Mandate
        // 스냅샷 없이 진행하고 없는 한도를 지어내지 않는다.
        ...(currentFundId() ? { fund_id: currentFundId() } : {}),
      }),
    });
  } catch {
    throw new Error(
      `CEO Hermes(BFF ${BFF})에 연결하지 못했습니다. FastAPI BFF가 실행 중인지 확인하세요.`,
    );
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  if (typeof body !== "object" || body === null) {
    throw new Error("CEO Hermes 응답 계약이 올바르지 않습니다.");
  }

  const result = body as Partial<CeoQueryResult>;
  const knownVersion = ACCEPTED_QUERY_VERSIONS.includes(
    result.schema_version as (typeof ACCEPTED_QUERY_VERSIONS)[number],
  );
  if (
    !knownVersion ||
    typeof result.answer !== "string" ||
    typeof result.task_id !== "string"
  ) {
    throw new Error(
      `CEO Hermes 응답 계약이 올바르지 않습니다. (받은 schema_version: ${String(result.schema_version)})`,
    );
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

/**
 * PR #224의 graph/result API를 정규화한다.
 *
 * `/ui/ceo/ask` 응답은 root task 생성의 accepted contract이고, 진행 조회는
 * `/tasks/{id}`, `/graph`, `/result`의 읽기 전용 API를 사용한다. Root는 scope
 * marker일 뿐이므로 진행 수치와 unusable/stalled 집계에서 제외한다.
 */
export async function ceoProgress(
  rootTaskId: string,
): Promise<CeoQueryProgress> {
  const encoded = encodeURIComponent(rootTaskId);
  const [status, graph, result] = await Promise.all([
    getJson<TaskStatusResponse>(`/ui/ceo/tasks/${encoded}`),
    getJson<TaskGraphResponse>(`/ui/ceo/tasks/${encoded}/graph`),
    getJson<TaskResultResponse>(`/ui/ceo/tasks/${encoded}/result`),
  ]);

  const summaryFor = (node: TaskGraphResponse["nodes"][number]): string => {
    if (node.id === graph.root && result.result?.summary) {
      return result.result.summary;
    }
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
      depends_on: graph.edges
        .filter(([, child]) => child === node.id)
        .map(([parent]) => parent),
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
    ["NO_ANSWER", "FAILED", "BLOCKED", "STALE", "NO_ASSIGNEE"].includes(
      card.outcome,
    ),
  );
  const stalled = workerCards.filter((card) =>
    ["QUEUED", "RUNNING"].includes(card.outcome),
  );

  return {
    schema_version: "ceo.query-progress.v1",
    root_task_id: status.root_task_id || graph.root,
    total: workerCards.length,
    finished: workerCards.filter((card) => terminal.has(card.outcome)).length,
    all_terminal:
      workerCards.length > 0
        ? workerCards.every((card) => terminal.has(card.outcome))
        : status.status === "completed",
    answer_grounded:
      Boolean(result.result?.summary) && result.qa_verdict !== "FAIL",
    unusable,
    stalled,
    cards,
  };
}
