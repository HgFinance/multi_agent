/**
 * BFF `/ui/snapshot` 중 부서 Registry·runtime 상태만 읽는다.
 *
 * main의 `app/ops/readModel.ts`(371줄)에는 포트폴리오·리스크 등 지금 화면이
 * 쓰지 않는 타입이 함께 있다. 여기서는 Agent Logs가 실제로 읽는 부분만
 * 가져왔고, 필드 이름과 의미는 main 계약(`operator-operations.v1`) 그대로다.
 */

import { BFF } from "./ceoClient";

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
  executor: string | null;
  worker_model: string | null;
  output_contract: string | null;
  worker_count: number;
  llm_worker_count: number;
  deterministic_worker_count: number;
  active_worker_count: number;
  conditional_worker_count: number;
  current_stage?: string | null;
  workers: OperationsWorker[];
  source_profile: string;
};

export type AgentStatusEvent = {
  department_code: string;
  worker_id: string | null;
  agent_id: string;
  status: RuntimeStatus;
  role: string | null;
  reason: string | null;
};

export type RuntimeWorker = {
  worker_id: string;
  department_code: string;
  stage: string;
  role: string;
  status: string;
  summary: string | null;
};

export type RuntimeMessage = {
  id: string;
  occurred_at: string;
  kind: string;
  department_code: string | null;
  worker_id: string | null;
  text: string;
};

export type LlmPerformanceMetric = {
  worker_id: string;
  role: string;
  stage: string;
  model_name: string;
  status: string;
  attempts: number;
  latency_ms: number;
  eval_score: number | null;
};

export type OperationsView = {
  observed_at: string;
  runtime_connected: boolean;
  departments: OperationsDepartment[];
  warnings: string[];
  agentStatuses: AgentStatusEvent[];
  activeWorkers: RuntimeWorker[];
  messages: RuntimeMessage[];
  metrics: LlmPerformanceMetric[];
};

/** LLM 성과 metric의 `stage`는 부서 domain과 이름이 몇 개 다르다. main 매핑 그대로. */
export function departmentStage(department: OperationsDepartment): string {
  return (
    { governance: "ceo", workforce: "hr", accounting: "accounting" }[department.domain] ?? department.domain
  );
}

const RUNTIME_STATUS_LABEL: Record<string, string> = {
  CONNECTED: "연결됨",
  STALE: "지연 수신",
  UNKNOWN: "확인 중",
  OFFLINE: "미연결",
  IDLE: "대기",
  REGISTERED: "등록됨",
  QUEUED: "실행 대기",
  RUNNING: "업무 중",
  COMPLETED: "완료",
  WAITING_APPROVAL: "승인 대기",
  BLOCKED: "실행 차단",
  DEGRADED: "안전 보류",
  ERROR: "오류",
  SKIPPED: "미호출",
};

export function readableRuntimeStatus(value: unknown): string {
  const status = String(value ?? "UNKNOWN").toUpperCase().replace(/\s+/g, "_");
  return RUNTIME_STATUS_LABEL[status] ?? status;
}

/** 서버가 그대로 뱉는 실패 사유를 사람이 읽을 문장으로 바꾼다.
 *  main `statusLabels.readableRuntimeMessage`를 그대로 옮겼다. */
export function readableRuntimeMessage(text: string): { summary: string; action?: string } {
  if (text.includes("PIT 입력이 준비되지 않아")) {
    return {
      summary: "시점 고정 데이터가 부족해 분석을 안전하게 보류했습니다.",
      action: "국내 종목과 시장 스냅샷을 준비한 뒤 다시 실행하세요.",
    };
  }
  if (text.includes("Worker 또는 Gate 검증")) {
    return {
      summary: "실행은 끝났지만 Worker 또는 Gate 검증이 완료되지 않았습니다.",
      action: "Risk·QA 결과와 대표 승인을 확인하세요.",
    };
  }
  if (text.includes("task plan에 따라") && text.includes("호출하지 않습니다")) {
    return { summary: "이번 요청의 작업 계획에 포함되지 않아 호출하지 않았습니다." };
  }
  return { summary: text };
}

export async function fetchOperations(): Promise<OperationsView> {
  let response: Response;
  try {
    response = await fetch(`${BFF}/ui/snapshot`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch {
    throw new Error(`BFF(${BFF})에 연결하지 못했습니다. 저장소 루트에서 FastAPI BFF를 8001 포트로 실행하세요.`);
  }
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? String((body as { detail?: unknown }).detail)
        : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  const operations = (
    body as {
      operations?: {
        observed_at?: string;
        runtime_connected?: boolean;
        departments?: OperationsDepartment[];
        warnings?: string[];
        agent_statuses?: AgentStatusEvent[];
        runtime?: {
          active_workers?: RuntimeWorker[];
          messages?: RuntimeMessage[];
          performance_metrics?: LlmPerformanceMetric[];
        };
      };
    } | null
  )?.operations;
  if (!operations || !Array.isArray(operations.departments)) {
    throw new Error("BFF 응답에 부서 Registry가 없습니다.");
  }
  return {
    observed_at: operations.observed_at ?? "",
    runtime_connected: Boolean(operations.runtime_connected),
    departments: operations.departments,
    warnings: operations.warnings ?? [],
    agentStatuses: operations.agent_statuses ?? [],
    activeWorkers: operations.runtime?.active_workers ?? [],
    messages: operations.runtime?.messages ?? [],
    metrics: operations.runtime?.performance_metrics ?? [],
  };
}
