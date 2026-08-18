/**
 * CEO Hermes 질의 클라이언트 — FastAPI BFF `/ui/ceo/ask`.
 *
 * 같은 ingress에서 자문과 사용자 PAPER 주문 명령을 받는다. 선택된 `book_id`는
 * 주문 범위를 서버에 전달할 뿐이며, 권한 확인과 실행 판단은 BFF가 소유한다.
 *
 * PR #224의 v1 계약과 PR #226의 v2 planning projection을 함께 지원한다.
 * v2 필드는 모두 additive라서 BFF와 프론트의 배포 순서가 바뀌어도 v1
 * 응답을 읽을 수 있다.
 */

import { BFF, bffFetch } from "./bffClient";
import { currentFundId } from "./currentFund";

export { BFF } from "./bffClient";

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
  /** Present only for the CEO -> Trading Hermes -> PAPER OMS lane. */
  order_request_id?: string | null;
  order_state?: string | null;
  order_mode?: "PAPER" | null;
  trading_task_id?: string | null;
};

export type PaperOrderWorkflowStatus = {
  schema_version: "user-paper-order-status.v1";
  order_request_id: string;
  mode: "PAPER";
  state: string;
  action: "PLACE_ORDER" | "SELL_ALL" | "CANCEL_ALL" | null;
  ceo_root_task_id: string | null;
  trading_task_id: string | null;
  clarification_code: string | null;
  error_code: string | null;
  error_message: string | null;
  directive: {
    directive_id: string;
    state: string;
    mode: "PAPER";
    error_code: string | null;
    error_message: string | null;
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
  /**
   * Synthesis 노드가 끝났을 때의 최종 답변. `/result`의 `result.summary`를
   * 그대로 옮긴 것 - `cards`에도 같은 텍스트가 root 카드에 실리지만, 호출부가
   * "root 카드를 찾아서 summary를 읽어라"를 각자 구현하면 root 카드를 목록에서
   * 거르는 화면(현재 DashboardView)에서 이 텍스트 자체가 통째로 안 보이게 되는
   * 사고가 난다 - 실제로 그랬다(2026-08-13). 최종 답변은 카드 목록과 별개로
   * 최상위 필드로 명시한다.
   */
  final_answer: string | null;
  unusable: CeoQueryCard[];
  stalled: CeoQueryCard[];
  cards: CeoQueryCard[];
};

export type TaskStatusResponse = {
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

export type TaskGraphResponse = {
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

export type TaskResultResponse = {
  status: "processing" | "completed";
  result: { summary: string } | null;
  departments: Record<string, string>;
  qa_verdict: string | null;
  block_reason: string | null;
};

/** 계정별 이력 한 건. `GET /ui/ceo/tasks`의 한 항목. */
export type CeoTaskListItem = {
  task_id: string;
  query: string | null;
  status: "queued" | "running" | "blocked" | "failed" | "completed" | "archived";
  created_at: string | null;
  selected_departments: string[];
  /** root body의 `requested_by=` 값. 옛 Root는 없어 `null`("계정 불명"). */
  owner_id: string | null;
};

export type CeoTaskListResponse = {
  schema_version: "ceo.task-list.v1";
  items: CeoTaskListItem[];
};

/** 워크플로 단계가 더는 안 바뀌는 상태. 이력 표시·폴링 중단 판정에 함께 쓴다. */
export const TERMINAL_WORKFLOW_STATUSES = new Set([
  "completed",
  "archived",
  "failed",
  "blocked",
]);

function explainError(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return `CEO Hermes 연결 실패 (HTTP ${status})`;
}
async function getJson<T>(path: string): Promise<T> {
  const response = await bffFetch(path, {
    cache: "no-store",
    // Bearer/fixture identity는 중앙 bffFetch만 주입한다.
    headers: { Accept: "application/json" },
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  return body as T;
}

export async function askCeo(
  query: string,
  requestId?: string,
  bookId?: string,
): Promise<CeoQueryResult> {
  let response: Response;
  try {
    response = await bffFetch("/ui/ceo/ask", {
      method: "POST",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        ...(requestId ? { "X-Request-Id": requestId } : {}),
      },
      body: JSON.stringify({
        query,
        request_id: requestId ?? crypto.randomUUID(),
        // 서버에 `user_id -> fund_id` 역참조 경로가 없어(fund_memberships가
        // 비어 있다) 화면이 쌍으로 보낸다. 없으면 생략 - BFF가 Mandate
        // 스냅샷 없이 진행하고 없는 한도를 지어내지 않는다.
        ...(currentFundId() ? { fund_id: currentFundId() } : {}),
        // 주문일 가능성이 있는 자연어를 위해 서버가 검증할 PAPER Book 범위를
        // 전달한다. 선택하지 않았으면 생략해 일반 자문은 그대로 사용할 수 있다.
        ...(bookId ? { book_id: bookId } : {}),
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

/** Verified JWT subject 범위의 CEO root task만 조회한다. */
export async function listCeoTasks(): Promise<CeoTaskListResponse> {
  return getJson<CeoTaskListResponse>("/ui/ceo/tasks");
}

export type CeoWorkflowStatusAndGraph = {
  status: TaskStatusResponse;
  graph: TaskGraphResponse;
};

/**
 * 본부별 진행 조회 절반 - `/tasks/{id}` + `/tasks/{id}/graph`만 부른다.
 *
 * 최종 답변(`/result`)과 폴링 간격이 다르므로(본부 진행 10초, 최종 답변 15초)
 * 이 둘을 하나로 묶으면 더 잦은 쪽 주기에 맞춰 둘 다 돌게 된다. 호출부가
 * 독립된 두 타이머로 이 함수와 {@link ceoWorkflowResult}를 각자 돌린다.
 */
export async function ceoWorkflowStatus(
  rootTaskId: string,
): Promise<CeoWorkflowStatusAndGraph> {
  const encoded = encodeURIComponent(rootTaskId);
  const [status, graph] = await Promise.all([
    getJson<TaskStatusResponse>(`/ui/ceo/tasks/${encoded}`),
    getJson<TaskGraphResponse>(`/ui/ceo/tasks/${encoded}/graph`),
  ]);
  return { status, graph };
}

/** 본부별 진행 조회 나머지 절반 - `/tasks/{id}/result`만 부른다. */
export async function ceoWorkflowResult(
  rootTaskId: string,
): Promise<TaskResultResponse> {
  return getJson<TaskResultResponse>(
    `/ui/ceo/tasks/${encodeURIComponent(rootTaskId)}/result`,
  );
}

export async function paperOrderWorkflowStatus(
  orderRequestId: string,
): Promise<PaperOrderWorkflowStatus> {
  return getJson<PaperOrderWorkflowStatus>(
    `/ui/paper-order-requests/${encodeURIComponent(orderRequestId)}`,
  );
}

const _EMPTY_RESULT: TaskResultResponse = {
  status: "processing",
  result: null,
  departments: {},
  qa_verdict: null,
  block_reason: null,
};

/**
 * PR #224의 graph/result API를 정규화한다.
 *
 * `status`+`graph`(10초 주기)와 `result`(15초 주기, 아직 안 왔으면 `null`)를
 * 호출부가 각자 폴링해 합친다 - 이 함수 자체는 네트워크를 부르지 않는다.
 * Root는 scope marker일 뿐이므로 진행 수치와 unusable/stalled 집계에서 제외한다.
 */
export function buildCeoProgress(
  { status, graph }: CeoWorkflowStatusAndGraph,
  result: TaskResultResponse | null,
): CeoQueryProgress {
  const effectiveResult = result ?? _EMPTY_RESULT;

  const summaryFor = (node: TaskGraphResponse["nodes"][number]): string => {
    if (node.id === graph.root && effectiveResult.result?.summary) {
      return effectiveResult.result.summary;
    }
    return effectiveResult.departments[node.department] ?? "";
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
      Boolean(effectiveResult.result?.summary) && effectiveResult.qa_verdict !== "FAIL",
    final_answer: effectiveResult.result?.summary ?? null,
    unusable,
    stalled,
    cards,
  };
}
