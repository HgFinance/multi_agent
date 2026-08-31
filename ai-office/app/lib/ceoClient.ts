/**
 * CEO Hermes 질의 클라이언트 — FastAPI BFF `/ui/ceo/ask`.
 *
 * BFF가 중앙 분류기이므로 이 호환 경로에서 전략 생성 요청은 CEO/Kanban 대신
 * `autonomous-research-request.v1` 연구실 접수 계약으로 반환될 수 있다.
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

export type StrategyResearchAccepted = {
  schema_version: "autonomous-research-request.v1";
  accepted: true;
  duplicate: boolean;
  request_id: string;
  lab_id: string;
  status: "QUEUED" | "RESEARCHING" | "COMPLETED" | "BLOCKED" | "CANDIDATE";
  message: string;
  status_url: string;
};

export type StrategyDeploymentAccepted = {
  schema_version: "autonomous-strategy-deployment.v1";
  request_id: string;
  deployment_id: string;
  mode: "shadow" | "paper" | "live";
  symbols: string[];
  status:
    | "AWAITING_APPROVAL"
    | "REQUESTED"
    | "REVIEW_REQUIRED"
    | "BLOCKED"
    | "APPROVED"
    | "DEPLOYING"
    | "ACTIVE"
    | "PAUSED"
    | "FAILED"
    | "REMOVED";
  research_status: string;
  plan_id: string | null;
  result_hash: string | null;
  approval_required: boolean;
  override_review_required: boolean;
  approved_by: string | null;
  bundle_hash: string | null;
  runtime_status: string;
  execution_status: string;
  backtest_summary: Record<string, unknown>;
  message: string;
};

export type StrategyResearchStatus = {
  schema_version: "autonomous-research-status.v1";
  request_id: string;
  lab_id: string;
  goal: string;
  universe: string;
  horizon: string;
  status: "QUEUED" | "RESEARCHING" | "COMPLETED" | "BLOCKED" | "CANDIDATE";
  cycle: number;
  last_action: string | null;
  active_plan_id: string | null;
  plan_count: number;
  result_count: number;
  candidate_available: boolean;
  updated_at: string;
  error: string | null;
  deployment_count: number;
  deployments: StrategyDeploymentAccepted[];
};

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
  action:
    | "PLACE_ORDER"
    | "PLACE_BASKET"
    | "SELL_ALL"
    | "CANCEL_ALL"
    | null;
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
  /**
   * 조건주문은 *활성화* 워크플로가 끝나면 요청이 COMPLETED가 되지만, 그때 만든
   * 규칙은 몇 분 뒤 집행 단계에서 directive 없이 실패할 수 있다. 요청 상태만
   * 보면 성공으로 읽히므로(2026-08-28) 규칙 결말을 함께 싣는다.
   */
  conditional_rules:
    | {
        rule_id: string;
        state: string;
        last_execution_state: string | null;
        last_guard_code: string | null;
        last_error_code: string | null;
        status_message: string | null;
      }[]
    | null;
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
  fundId?: string,
): Promise<CeoQueryResult | StrategyResearchAccepted | StrategyDeploymentAccepted> {
  const resolvedFundId = fundId ?? currentFundId();
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
        ...(resolvedFundId ? { fund_id: resolvedFundId } : {}),
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

  // The BFF is the central classifier.  Strategy intents may return the
  // independent research-session contract from this legacy-compatible path.
  const strategy = body as Partial<StrategyResearchAccepted>;
  const deployment = body as Partial<StrategyDeploymentAccepted>;
  if (
    deployment.schema_version === "autonomous-strategy-deployment.v1" &&
    typeof deployment.deployment_id === "string" &&
    typeof deployment.request_id === "string"
  ) {
    return deployment as StrategyDeploymentAccepted;
  }
  if (
    strategy.schema_version === "autonomous-research-request.v1" &&
    strategy.accepted === true &&
    typeof strategy.request_id === "string" &&
    typeof strategy.status_url === "string"
  ) {
    return strategy as StrategyResearchAccepted;
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

/**
 * Submit a strategy-generation sentence to the isolated autonomous lab.
 * This is intentionally separate from the CEO/Kanban and PAPER-order paths.
 */
export async function askStrategyResearch(
  query: string,
  requestId?: string,
): Promise<StrategyResearchAccepted> {
  const response = await bffFetch("/ui/strategy-research/ask", {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({
      query,
      ...(requestId ? { request_id: requestId } : {}),
    }),
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok || typeof body !== "object" || body === null) {
    throw new Error(explainError(body, response.status));
  }
  const result = body as Partial<StrategyResearchAccepted>;
  if (
    result.schema_version !== "autonomous-research-request.v1" ||
    result.accepted !== true ||
    typeof result.request_id !== "string" ||
    typeof result.status_url !== "string"
  ) {
    throw new Error("자율 전략 연구실 응답 계약이 올바르지 않습니다.");
  }
  return result as StrategyResearchAccepted;
}

export async function strategyResearchStatus(
  requestId: string,
): Promise<StrategyResearchStatus> {
  return getJson<StrategyResearchStatus>(
    `/ui/strategy-research/requests/${encodeURIComponent(requestId)}`,
  );
}

export async function requestStrategyDeployment(
  requestId: string,
  input: {
    mode: "shadow" | "paper" | "live";
    symbols: string[];
    confirm: boolean;
    reason: string;
  },
): Promise<StrategyDeploymentAccepted> {
  const response = await bffFetch(
    `/ui/strategy-research/requests/${encodeURIComponent(requestId)}/deploy`,
    {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(input),
    },
  );
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok || typeof body !== "object" || body === null) {
    throw new Error(explainError(body, response.status));
  }
  const result = body as Partial<StrategyDeploymentAccepted>;
  if (
    result.schema_version !== "autonomous-strategy-deployment.v1" ||
    typeof result.deployment_id !== "string" ||
    typeof result.request_id !== "string"
  ) {
    throw new Error("전략 배포 요청 응답 계약이 올바르지 않습니다.");
  }
  return result as StrategyDeploymentAccepted;
}

export async function approveStrategyDeployment(
  requestId: string,
  deploymentId: string,
  input: {
    confirm: boolean;
    reason: string;
    override_review_required?: boolean;
  },
): Promise<StrategyDeploymentAccepted> {
  const response = await bffFetch(
    `/ui/strategy-research/requests/${encodeURIComponent(requestId)}/deployments/${encodeURIComponent(deploymentId)}/approve`,
    {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(input),
    },
  );
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok || typeof body !== "object" || body === null) {
    throw new Error(explainError(body, response.status));
  }
  const result = body as Partial<StrategyDeploymentAccepted>;
  if (
    result.schema_version !== "autonomous-strategy-deployment.v1" ||
    typeof result.deployment_id !== "string" ||
    typeof result.request_id !== "string"
  ) {
    throw new Error("전략 배포 승인 응답 계약이 올바르지 않습니다.");
  }
  return result as StrategyDeploymentAccepted;
}

export async function powerStrategyDeployment(
  requestId: string,
  deploymentId: string,
  input: { action: "start" | "stop"; reason: string },
): Promise<StrategyDeploymentAccepted> {
  const response = await bffFetch(
    `/ui/strategy-research/requests/${encodeURIComponent(requestId)}/deployments/${encodeURIComponent(deploymentId)}/power`,
    {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(input),
    },
  );
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok || typeof body !== "object" || body === null) {
    throw new Error(explainError(body, response.status));
  }
  const result = body as Partial<StrategyDeploymentAccepted>;
  if (
    result.schema_version !== "autonomous-strategy-deployment.v1" ||
    typeof result.deployment_id !== "string" ||
    typeof result.request_id !== "string"
  ) {
    throw new Error("전략 컨테이너 상태 응답 계약이 올바르지 않습니다.");
  }
  return result as StrategyDeploymentAccepted;
}

export async function removeStrategyDeployment(
  requestId: string,
  deploymentId: string,
  input: { confirm: boolean; reason: string },
): Promise<StrategyDeploymentAccepted> {
  const response = await bffFetch(
    `/ui/strategy-research/requests/${encodeURIComponent(requestId)}/deployments/${encodeURIComponent(deploymentId)}/remove`,
    {
      method: "POST",
      cache: "no-store",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(input),
    },
  );
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok || typeof body !== "object" || body === null) {
    throw new Error(explainError(body, response.status));
  }
  const result = body as Partial<StrategyDeploymentAccepted>;
  if (
    result.schema_version !== "autonomous-strategy-deployment.v1" ||
    typeof result.deployment_id !== "string" ||
    typeof result.request_id !== "string"
  ) {
    throw new Error("전략 제거 응답 계약이 올바르지 않습니다.");
  }
  return result as StrategyDeploymentAccepted;
}

export async function strategyDeployments(
  requestId: string,
): Promise<{ schema_version: "autonomous-strategy-deployments.v1"; request_id: string; deployments: StrategyDeploymentAccepted[] }> {
  return getJson(
    `/ui/strategy-research/requests/${encodeURIComponent(requestId)}/deployments`,
  );
}

export async function strategyDeploymentStatus(
  requestId: string,
  deploymentId: string,
): Promise<StrategyDeploymentAccepted> {
  return getJson<StrategyDeploymentAccepted>(
    `/ui/strategy-research/requests/${encodeURIComponent(requestId)}/deployments/${encodeURIComponent(deploymentId)}`,
  );
}

/** Keep client routing conservative; the server remains the authority on state. */
export function looksLikeStrategyResearchQuery(query: string): boolean {
  const value = query.toLocaleLowerCase();
  const noun = /전략|strategy|알파|시그널|백테스트|트레이딩\s*전략|quant|backtest/;
  const verb = /생성|만들|개발|연구|검증|발굴|찾아|설계|generate|create|build|develop|research|validate|discover|find|design/;
  return (noun.test(value) && verb.test(value)) || /백테스트/.test(value);
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

/** Fixed local fixture identity 범위의 CEO root task만 조회한다. */
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

function statusRecord(value: unknown): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new Error("paper_order_status_invalid_response");
  }
  return value as Record<string, unknown>;
}

function statusNullableString(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value !== "string") {
    throw new Error("paper_order_status_invalid_response");
  }
  return value;
}

/**
 * BFF 주문 상태 응답을 실제로 검증한다.
 *
 * `getJson`은 `body as T` 캐스팅뿐이라 응답이 계약을 벗어나도 컴파일 타임에도
 * 런타임에도 신호가 없었다(2026-08-31). 화면은 이 값으로 사용자에게 주문의
 * 결말을 보여주므로, 형태가 깨진 응답은 조용히 렌더링하는 대신 실패시킨다 -
 * 호출부의 `isError` 분기가 "다시 확인 중" 문구로 받아 폴링을 이어간다.
 *
 * 반대로 `state`/`action` 같은 열거 문자열의 *값*은 여기서 막지 않는다.
 * 백엔드가 새 상태를 추가했다는 이유로 주문 결말 화면을 통째로 못 쓰게 만드는
 * 편이 더 나쁘다. 값 목록의 드리프트는 CI의
 * `tests/contracts/test_ui_paper_order_action_contract.py`가 잡는다.
 */
export function parsePaperOrderWorkflowStatus(
  value: unknown,
): PaperOrderWorkflowStatus {
  const body = statusRecord(value);
  if (
    body.schema_version !== "user-paper-order-status.v1" ||
    typeof body.order_request_id !== "string" ||
    body.mode !== "PAPER" ||
    typeof body.state !== "string"
  ) {
    throw new Error("paper_order_status_invalid_response");
  }

  const directive =
    body.directive === null || body.directive === undefined
      ? null
      : (() => {
          const value = statusRecord(body.directive);
          if (
            typeof value.directive_id !== "string" ||
            typeof value.state !== "string" ||
            value.mode !== "PAPER"
          ) {
            throw new Error("paper_order_status_invalid_response");
          }
          return {
            directive_id: value.directive_id,
            state: value.state,
            mode: "PAPER" as const,
            error_code: statusNullableString(value.error_code),
            error_message: statusNullableString(value.error_message),
          };
        })();

  let conditionalRules: PaperOrderWorkflowStatus["conditional_rules"] = null;
  if (body.conditional_rules !== null && body.conditional_rules !== undefined) {
    if (!Array.isArray(body.conditional_rules)) {
      throw new Error("paper_order_status_invalid_response");
    }
    conditionalRules = body.conditional_rules.map((entry) => {
      const rule = statusRecord(entry);
      if (typeof rule.rule_id !== "string" || typeof rule.state !== "string") {
        throw new Error("paper_order_status_invalid_response");
      }
      return {
        rule_id: rule.rule_id,
        state: rule.state,
        last_execution_state: statusNullableString(rule.last_execution_state),
        last_guard_code: statusNullableString(rule.last_guard_code),
        last_error_code: statusNullableString(rule.last_error_code),
        status_message: statusNullableString(rule.status_message),
      };
    });
  }

  return {
    schema_version: "user-paper-order-status.v1",
    order_request_id: body.order_request_id,
    mode: "PAPER",
    state: body.state,
    action: statusNullableString(
      body.action,
    ) as PaperOrderWorkflowStatus["action"],
    ceo_root_task_id: statusNullableString(body.ceo_root_task_id),
    trading_task_id: statusNullableString(body.trading_task_id),
    clarification_code: statusNullableString(body.clarification_code),
    error_code: statusNullableString(body.error_code),
    error_message: statusNullableString(body.error_message),
    directive,
    conditional_rules: conditionalRules,
  };
}

export async function paperOrderWorkflowStatus(
  orderRequestId: string,
): Promise<PaperOrderWorkflowStatus> {
  return parsePaperOrderWorkflowStatus(
    await getJson<unknown>(
      `/ui/paper-order-requests/${encodeURIComponent(orderRequestId)}`,
    ),
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
 * Kanban graph는 Hermes profile 이름을, result.departments는 논리 부서 코드를
 * 사용한다. 두 표현을 여기서만 맞춘다. 알 수 없는 profile은 그대로 두어 BFF가
 * 새 키를 추가해도 결과를 숨기지 않는다.
 */
const DEPARTMENT_CODE_BY_PROFILE: Readonly<Record<string, string>> = {
  "ceo-agent": "ceo",
  "research-department": "research",
  "research-liaison": "research",
  "quant-backtest-department": "quant",
  "quant-liaison": "quant",
  "trading-department": "trading",
  "accounting-portfolio-department": "accounting",
  "risk-management": "risk",
  "qa-department": "qa",
  "hr-department": "hr",
};

function departmentCodeForProfile(profile: string): string {
  return DEPARTMENT_CODE_BY_PROFILE[profile] ?? profile;
}

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
    // 최종 synthesis는 `departments`가 아니라 최상위 `result`에만 있다.
    if ((node.id === graph.root || node.role === "synthesis") && effectiveResult.result?.summary) {
      return effectiveResult.result.summary;
    }
    return effectiveResult.departments[departmentCodeForProfile(node.department)] ?? "";
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
