/**
 * Risk·QA 전용 AI Office 연결 계약.
 *
 * 이 파일은 화면 Projection과 두 부서의 Hermes Profile/Worker Registry를
 * 연결하는 allowlist다. employees에는 Hermes Head를 포함하지 않고 실제
 * LangGraph Worker만 둔다. Head 정보는 별도 필드로 표시해 인원 수 혼동을 막는다.
 * 실제 금융 상태·주문·원장·Risk Limit은 각 Domain API와 결정론적 엔진이 소유한다.
 */

import type { Agent, Snapshot } from "../game/sim";
import { BFF } from "./readModel";

export const RISK_QA_RETRY_POLICY = {
  maxRetries: 2,
  maxAttempts: 3,
  riskFallback: "REJECT + HALTED",
  qaFallback: "ESCALATE + manual_review_required",
} as const;

export const RISK_QA_LOG_EVENTS = [
  "InputSnapshot",
  "AgentOutput",
  "Validation",
  "Decision",
  "Order",
  "Fill",
] as const;

export type RiskQaDepartmentId = "ops" | "qa";

export type RiskQaEmployee = {
  profileId: string;
  persona: string;
  name: string;
  role: string;
  rank: "worker";
  skills: readonly string[];
  forbiddenTools: readonly string[];
};

export type RiskQaDepartment = {
  id: RiskQaDepartmentId;
  domain: "risk-management" | "qa-department";
  name: string;
  orchestrator: "Hermes";
  employeeExecutor: "LangGraph";
  headProfile: string;
  headProvider: "openai-codex";
  headModel: "gpt-5.6-luna";
  workerModel: "qwen3:1.7b";
  sourceProfile: string;
  runtimeContract: string;
  employees: readonly RiskQaEmployee[];
};

const RISK_FORBIDDEN_TOOLS = [
  "oms.submit",
  "ledger.write",
  "risk.trading_state.write",
  "risk.trading_state.clear",
] as const;

const QA_FORBIDDEN_TOOLS = [
  "oms.submit",
  "ledger.write",
  "risk.limit.write",
  "risk.trading_state.write",
  "risk.trading_state.clear",
] as const;

export const RISK_QA_DEPARTMENT_IDS: readonly RiskQaDepartmentId[] = ["ops", "qa"];

export const RISK_QA_CONNECTION: readonly RiskQaDepartment[] = [
  {
    id: "ops",
    domain: "risk-management",
    name: "Risk관리본부",
    orchestrator: "Hermes",
    employeeExecutor: "LangGraph",
    headProfile: "risk-management",
    headProvider: "openai-codex",
    headModel: "gpt-5.6-luna",
    workerModel: "qwen3:1.7b",
    sourceProfile: "departments/03-risk/hermes/config.yaml",
    runtimeContract: "/investment-cases/{case_id}/risk-check · /risk/v1/*",
    employees: [
      {
        profileId: "market-liquidity-worker",
        persona: "market-liquidity-worker",
        name: "문가온",
        role: "Market·Liquidity Risk",
        rank: "worker",
        skills: ["risk.trading_state.read", "risk.p1.snapshot"],
        forbiddenTools: RISK_FORBIDDEN_TOOLS,
      },
      {
        profileId: "pre-trade-risk-worker",
        persona: "pre-trade-risk-worker",
        name: "노은우",
        role: "Pre-trade Risk",
        rank: "worker",
        skills: ["risk.case.check"],
        forbiddenTools: RISK_FORBIDDEN_TOOLS,
      },
      {
        profileId: "compliance-policy-worker",
        persona: "compliance-policy-worker",
        name: "류하진",
        role: "Point-in-time Compliance Policy",
        rank: "worker",
        skills: ["risk.compliance.check"],
        forbiddenTools: RISK_FORBIDDEN_TOOLS,
      },
      {
        profileId: "derivatives-counterparty-worker",
        persona: "derivatives-counterparty-worker",
        name: "안유하",
        role: "Derivatives·Counterparty Exposure",
        rank: "worker",
        skills: ["risk.trading_state.record.read"],
        forbiddenTools: RISK_FORBIDDEN_TOOLS,
      },
    ],
  },
  {
    id: "qa",
    domain: "qa-department",
    name: "AI QA·감사본부",
    orchestrator: "Hermes",
    employeeExecutor: "LangGraph",
    headProfile: "qa-department",
    headProvider: "openai-codex",
    headModel: "gpt-5.6-luna",
    workerModel: "qwen3:1.7b",
    sourceProfile: "departments/06-ai-qa-audit/hermes/config.yaml",
    runtimeContract: "/investment-cases/{case_id}/qa-check · /qa/v1/*",
    employees: [
      {
        profileId: "evidence-qa-worker",
        persona: "evidence-qa-worker",
        name: "강태오",
        role: "Evidence·Citation QA",
        rank: "worker",
        skills: ["qa.evidence.check"],
        forbiddenTools: QA_FORBIDDEN_TOOLS,
      },
      {
        profileId: "hallucination-critic-worker",
        persona: "hallucination-critic-worker",
        name: "문세라",
        role: "Hallucination·Contradiction Review",
        rank: "worker",
        skills: ["qa.evidence.rag"],
        forbiddenTools: QA_FORBIDDEN_TOOLS,
      },
      {
        profileId: "model-and-internal-audit-worker",
        persona: "model-and-internal-audit-worker",
        name: "정하은",
        role: "Model Risk·Internal Audit",
        rank: "worker",
        skills: ["qa.model_risk.evaluate", "qa.internal_audit.evaluate"],
        forbiddenTools: QA_FORBIDDEN_TOOLS,
      },
      {
        profileId: "ops-and-permission-worker",
        persona: "ops-and-permission-worker",
        name: "배준서",
        role: "Agent Ops·Tool Permission",
        rank: "worker",
        skills: ["qa.ops.evaluate", "qa.tool_permission.check"],
        forbiddenTools: QA_FORBIDDEN_TOOLS,
      },
      {
        profileId: "incident-postmortem-worker",
        persona: "incident-postmortem-worker",
        name: "이수빈",
        role: "Incident·Postmortem",
        rank: "worker",
        skills: ["qa.incident.record"],
        forbiddenTools: QA_FORBIDDEN_TOOLS,
      },
    ],
  },
] as const;

export type RiskQaProjection = {
  schema_version: "operator-domain.v1";
  domain: "risk-qa";
  mode: "DEMO";
  status: "CONNECTED" | "DEGRADED";
  observed_at: string;
  event_bridge_connected: boolean;
  sequence: number;
  departments: ReadonlyArray<{
    department_code: string;
    status: string;
    status_reason?: string;
    worker_count?: number;
    active_worker_count?: number;
    active_workers?: readonly string[];
  }>;
  agents: ReadonlyArray<{
    agent_id: string;
    worker_id: string | null;
    status: string;
    reason: string | null;
  }>;
  warnings: readonly string[];
};

export async function fetchRiskQaProjection(): Promise<RiskQaProjection> {
  const response = await fetch(`${BFF}/ui/risk-qa`, {
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? String((body as { detail?: unknown }).detail)
        : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return body as RiskQaProjection;
}

export type RiskQaActivity = {
  departmentStatus: Snapshot["deptStatus"][string];
  statusLabel: "출근 대기" | "진행 중" | "업무 중" | "완료" | "승인 대기" | "연동 대기" | "대기";
  taskLabel: string;
  onDutyCount: number;
  workingCount: number;
  employees: readonly { employee: RiskQaEmployee; agent: Agent | null }[];
};

const ACTIVE_AGENT_STATUSES: readonly Agent["status"][] = ["업무 중", "회의 중"];

/** AI Office simulation의 Agent 상태를 Risk/QA Worker 카드에 투영한다. */
export function getRiskQaActivity(
  department: RiskQaDepartment,
  agents: readonly Agent[],
  snapshot: Snapshot,
): RiskQaActivity {
  const teamAgents = agents.filter((agent) => agent.deptId === department.id);
  const activeAgents = teamAgents.filter((agent) => ACTIVE_AGENT_STATUSES.includes(agent.status));
  const departmentStatus = snapshot.running ? snapshot.deptStatus[department.id] ?? "대기" : "대기";
  const statusLabel = snapshot.running ? departmentStatus : "출근 대기";
  const taskLabel =
    activeAgents.find((agent) => agent.taskLabel)?.taskLabel ??
    (statusLabel === "완료"
      ? "오늘 업무 완료"
      : statusLabel === "출근 대기"
        ? "업무 시작을 기다리는 중"
        : "다음 작업을 기다리는 중");

  return {
    departmentStatus,
    statusLabel,
    taskLabel,
    onDutyCount: teamAgents.filter((agent) => agent.status !== "출근 전").length,
    workingCount: activeAgents.length,
    employees: department.employees.map((employee) => ({
      employee,
      agent: teamAgents.find((agent) => agent.name === employee.name) ?? null,
    })),
  };
}

export function isRiskQaDepartment(id: string): id is RiskQaDepartmentId {
  return RISK_QA_DEPARTMENT_IDS.includes(id as RiskQaDepartmentId);
}
