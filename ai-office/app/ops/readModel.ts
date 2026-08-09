// Trading·Portfolio Read Model — 백엔드가 확정한 상태를 화면으로 옮기는 계약.
//
// 소유: 도현 (트레이딩 + 회계·포트폴리오)
// 근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 3.2(6·7), 5.1, 9(UI-0), 11
// 생성기: departments/05-accounting-portfolio/portfolio/ui_read_model.py
//
// 규칙 두 가지가 여기서도 유지된다.
//
//  1. 금액·수량은 string이다. JSON number는 double이라 Decimal이 깨진다.
//     화면은 표시만 하므로 파싱하지 않는다. 계산이 필요하면 서버가 한다.
//  2. 이 파일은 수치를 만들지 않는다. NAV도 비중도 백엔드가 확정한 값이다
//     (계획 1절 — 화면은 공식 장부를 계산하지 않는다).

/** 원 단위 Decimal 문자열. 절대 Number로 바꾸지 않는다. */
export type Money = string;

export type Position = {
  instrument_id: string;
  quantity: Money;
  average_cost: Money;
  mark_price: Money;
  mark_as_of: string;
  market_value: Money;
  unrealized_pnl: Money;
  /** NAV가 0 이하면 비중이 정의되지 않는다 — 그때만 null이다. */
  weight: Money | null;
};

export type OrderIntentRow = {
  order_intent_id: string;
  state: string;
  requested_quantity: Money;
  risk_decision_id: string | null;
  risk_approved_qty: Money | null;
  valid_until: string;
};

export type BrokerOrderRow = {
  order_id: string;
  order_intent_id: string;
  client_order_id: string;
  broker_order_id: string | null;
  broker_adapter: string;
  state: string;
  side: string;
  instrument_id: string;
  requested_quantity: Money;
  filled_quantity: Money;
  leaves_quantity: Money;
  limit_price: Money | null;
  average_fill_price: Money | null;
  fill_count: number;
  is_terminal: boolean;
};

export type TradingSnapshot = {
  schema_version: number;
  /** DEMO/PAPER/LIVE 데이터를 같은 화면에서 섞지 않는다 (계획 4절). */
  mode: "DEMO" | "PAPER" | "LIVE";
  snapshot_version: number;
  server_time: string;
  fund_id: string;
  book_id: string;
  portfolio: {
    as_of: string;
    nav: Money;
    cash: Money;
    securities_value: Money;
    gross_exposure: Money;
    net_exposure: Money;
    realized_pnl: Money;
    unrealized_pnl: Money;
    fees: Money;
    taxes: Money;
    positions: Position[];
  };
  trading: {
    intents: OrderIntentRow[];
    orders: BrokerOrderRow[];
    /** UNKNOWN 주문이 있으면 신규 주문이 막힌 상태다. */
    blocked_by_unknown: boolean;
  };
  ledger: {
    journal_count: number;
    reversal_count: number;
    trial_balance_sum: Money;
    balanced: boolean;
    accounts: Record<string, Money>;
  };
  operations?: OperationsSnapshot;
};

export type RuntimeStatus =
  | "OFFLINE"
  | "IDLE"
  | "QUEUED"
  | "RUNNING"
  | "WAITING_APPROVAL"
  | "BLOCKED"
  | "DEGRADED"
  | "ERROR";

export type OperationsWorker = {
  worker_id: string;
  runtime_kind: "llm" | "deterministic";
  status: string;
  trigger: string | null;
};

export type OperationsDepartment = {
  department_code: string;
  name: string;
  domain: string;
  status: RuntimeStatus;
  status_reason: string;
  runtime_observed: boolean;
  head_persona: string | null;
  head_provider: string | null;
  head_model: string | null;
  executor: string | null;
  worker_model: string | null;
  output_contract: string | null;
  failure_action: string | null;
  worker_count: number;
  llm_worker_count: number;
  deterministic_worker_count: number;
  active_worker_count: number;
  conditional_worker_count: number;
  active_workers?: string[];
  current_stage?: string | null;
  workers: OperationsWorker[];
  source_profile: string;
};

export type OperationsCommunication = {
  event_type: string;
  status: "IMPLEMENTED" | "IMPLEMENTED_INTERNAL" | "PLANNED" | "LEGACY_ALIAS" | string;
  layer: string;
  producer: string;
  consumers: string[];
  case_binding: string | null;
  source: string;
  live: boolean;
  transport: string;
};

export type OperationsSnapshot = {
  schema_version: "operator-operations.v1";
  observed_at: string;
  sequence?: number;
  agent_statuses?: Array<{
    event_id: string;
    event_type: "agent.status.v1";
    schema_version: 1;
    sequence: number;
    department_code: string;
    agent_id: string;
    worker_id: string | null;
    status: RuntimeStatus;
    role: string | null;
 reason: string | null;
 metadata?: Record<string, unknown>;
  }>;
  agent_status_events?: Array<Record<string, unknown>>;
  status: "DEGRADED" | "CONNECTED" | string;
  runtime_connected: boolean;
  event_bridge_connected: boolean;
  message_count: number;
  implemented_event_contracts: number;
  planned_event_contracts: number;
  departments: OperationsDepartment[];
  communications: OperationsCommunication[];
  runtime: OperationsRuntime;
  warnings: string[];
};

export type OperationsRuntimeWorker = {
  worker_id: string;
  department_code: string;
  stage: string;
  role: string;
  status: string;
  summary: string | null;
};

export type LlmPerformanceMetric = {
  schema_version: "llm.performance.v1" | string;
  worker_id: string;
  role: string;
  stage: string;
  model_name: string;
  status: string;
  attempts: number;
  llm_calls: number;
  retries: number;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  latency_ms: number;
  eval_score: number | null;
  error_count: number;
  raw_payloads_sent: false;
};

export type OperationsRuntimeMessage = {
  id: string;
  occurred_at: string;
  kind: string;
  department_code: string | null;
  worker_id: string | null;
  text: string;
};

export type OperationsRuntimeHandoff = {
  from_department: string;
  to_department: string;
  from_head: string;
  to_head: string;
  status: string;
  title: string;
  message: string;
  occurred_at: string;
  expires_at: number;
};

export type OperationsRuntime = {
  status: RuntimeStatus | "COMPLETED";
  run_id: string | null;
  workflow: string | null;
  phase: string | null;
  departments: Record<
    string,
    { status: string; current_stage: string | null; active_worker_ids: string[]; kanban_task_id?: string | null }
  >;
  active_workers: OperationsRuntimeWorker[];
  performance_metrics?: LlmPerformanceMetric[];
  active_handoff: OperationsRuntimeHandoff | null;
  observability?: {
    langsmith?: {
      status: string;
      configured: boolean;
      tracing_enabled: boolean;
      endpoint: string | null;
      project: string | null;
    };
  };
  messages: OperationsRuntimeMessage[];
  result: Record<string, unknown> | null;
  approval: {
    status: "PENDING" | "APPROVE" | "REJECT" | string;
    binding: boolean;
    approved_at: string | null;
    comment: string | null;
  } | null;
  error: string | null;
};

/** 이 파일이 아는 Major Version. 다르면 적용하지 않는다 (계획 5.3). */
export const SUPPORTED_SCHEMA_VERSION = 1;

/** FastAPI BFF 주소. 배포 Origin이 정해지면 환경변수로 넘긴다. */
const configuredBff = process.env.NEXT_PUBLIC_BFF_URL?.trim();
export const BFF = (configuredBff || "http://127.0.0.1:8001").replace(/\/+$/, "");
/**
 * Sequence는 단조 증가하는 canonical projection 버전이다.
 * 알 수 없는 값은 최신 상태로 해석하지 않는다.
 */
export function isValidSequence(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

export function getSnapshotSequence(snapshot: TradingSnapshot): number | null {
  if (snapshot.operations === undefined) return 0;
  const sequence = snapshot.operations.sequence;
  return isValidSequence(sequence) ? sequence : null;
}

/**
 * Snapshot 형태 검증.
 *
 * 계획 5.3은 Zod를 쓰라고 하지만 지금 대상은 정적 DEMO Fixture 하나이고
 * ai-office에 Zod가 없다. 라이브러리를 하나 더 들이는 대신 필요한 것만 본다.
 * WebSocket Event를 받기 시작하면(Phase UI-1) 그때 Zod로 옮긴다.
 */
export function parseSnapshot(input: unknown): TradingSnapshot {
  if (typeof input !== "object" || input === null) {
    throw new Error("Snapshot이 객체가 아닙니다");
  }
  const doc = input as Record<string, unknown>;

  if (doc.schema_version !== SUPPORTED_SCHEMA_VERSION) {
    // 모르는 Version은 추측해서 그리지 않는다. 틀린 숫자를 보여주는 것보다 낫다.
    throw new Error(
      `지원하지 않는 schema_version ${String(doc.schema_version)} (지원: ${SUPPORTED_SCHEMA_VERSION})`,
    );
  }
  if (doc.mode !== "DEMO" && doc.mode !== "PAPER" && doc.mode !== "LIVE") {
    throw new Error(`알 수 없는 mode: ${String(doc.mode)}`);
  }
  for (const key of ["portfolio", "trading", "ledger"] as const) {
    if (typeof doc[key] !== "object" || doc[key] === null) {
      throw new Error(`Snapshot에 ${key}가 없습니다`);
    }
  }
  if (doc.operations !== undefined) {
    if (typeof doc.operations !== "object" || doc.operations === null) {
      throw new Error("Snapshot의 operations가 객체가 아닙니다");
    }
    const operations = doc.operations as Record<string, unknown>;
    if (operations.schema_version !== "operator-operations.v1") {
      throw new Error(`지원하지 않는 operations schema_version ${String(operations.schema_version)}`);
    }
    if (!Array.isArray(operations.departments) || !Array.isArray(operations.communications)) {
      throw new Error("Snapshot operations에 부서 또는 통신 목록이 없습니다");
    }
    if (typeof operations.runtime !== "object" || operations.runtime === null) {
      throw new Error("Snapshot operations에 runtime projection이 없습니다");
    }
    if (operations.sequence !== undefined && !isValidSequence(operations.sequence)) {
      throw new Error("Snapshot operations의 sequence가 유효하지 않습니다");
    }
  }
  return doc as unknown as TradingSnapshot;
}

/** 원 단위 표시. 파싱해서 계산하지 않고 자릿수만 끊는다. */
export function won(value: Money | null): string {
  if (value === null) return "—";
  const negative = value.startsWith("-");
  const [intPart = "0"] = (negative ? value.slice(1) : value).split(".");
  const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${negative ? "-" : ""}${grouped}원`;
}

/** 비중 표시. weight는 0~1 사이의 Decimal 문자열이다. */
export function percent(value: Money | null): string {
  if (value === null) return "—";
  const asNumber = Number(value);
  if (!Number.isFinite(asNumber)) return "—";
  return `${(asNumber * 100).toFixed(2)}%`;
}
