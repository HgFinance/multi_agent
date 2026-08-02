/**
 * Risk·QA 전용 AI Office 연결 계약.
 *
 * 이 파일은 화면 Projection과 Hermes Profile 사이의 allowlist다. 다른 부서는
 * 이 계약에 포함하지 않으며, 여기에 적힌 직원도 주문·원장·Risk Limit을 직접
 * 변경할 수 없다. 실제 금융 상태는 Risk/QA API와 결정론적 엔진의 소유다.
 */

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
  rank: "lead" | "member";
  skills: readonly string[];
  forbiddenTools: readonly string[];
};

export type RiskQaDepartment = {
  id: RiskQaDepartmentId;
  domain: "risk-management" | "qa-department";
  name: string;
  orchestrator: "Hermes";
  employeeExecutor: "LangGraph";
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

/** 연결 허용 범위. 반드시 두 부서만 유지한다. */
export const RISK_QA_DEPARTMENT_IDS = ["ops", "qa"] as const satisfies readonly RiskQaDepartmentId[];

export const RISK_QA_CONNECTION: readonly RiskQaDepartment[] = [
  {
    id: "ops",
    domain: "risk-management",
    name: "리스크본부",
    orchestrator: "Hermes",
    employeeExecutor: "LangGraph",
    sourceProfile: "departments/03-risk/hermes/config.yaml",
    runtimeContract: "/investment-cases/{case_id}/risk-check + /risk/v1/*",
    employees: [
      {
        profileId: "RSK-00",
        persona: "risk-supervisor",
        name: "이예주",
        role: "리스크본부 팀장",
        rank: "lead",
        skills: ["risk.pre_trade.check", "risk.qa.handoff"],
        forbiddenTools: RISK_FORBIDDEN_TOOLS,
      },
      {
        profileId: "RSK-01",
        persona: "pre-trade-risk-analyst",
        name: "노은우",
        role: "Pre-Trade 리스크",
        rank: "member",
        skills: ["risk.pre_trade.check"],
        forbiddenTools: RISK_FORBIDDEN_TOOLS,
      },
      {
        profileId: "RSK-02",
        persona: "market-liquidity-risk-agent",
        name: "문가온",
        role: "시장·유동성 리스크",
        rank: "member",
        skills: ["risk.p1.snapshot", "risk.trading_state.read"],
        forbiddenTools: RISK_FORBIDDEN_TOOLS,
      },
      {
        profileId: "RSK-04",
        persona: "compliance-policy-agent",
        name: "류하진",
        role: "Compliance Policy",
        rank: "member",
        skills: ["risk.compliance.check"],
        forbiddenTools: RISK_FORBIDDEN_TOOLS,
      },
      {
        profileId: "RSK-05",
        persona: "derivatives-margin-risk-agent",
        name: "안유하",
        role: "파생·마진 리스크",
        rank: "member",
        skills: ["risk.p1.snapshot"],
        forbiddenTools: RISK_FORBIDDEN_TOOLS,
      },
      {
        profileId: "RSK-06",
        persona: "operational-counterparty-risk-agent",
        name: "마도연",
        role: "운영·거래상대방 리스크",
        rank: "member",
        skills: ["risk.trading_state.read"],
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
    sourceProfile: "departments/06-ai-qa-audit/hermes/config.yaml",
    runtimeContract: "/investment-cases/{case_id}/qa-check + /qa/v1/*",
    employees: [
      {
        profileId: "QAA-00",
        persona: "qa-audit-supervisor",
        name: "김동규",
        role: "QA·감사본부 팀장",
        rank: "lead",
        skills: ["qa.evidence.check", "qa.internal_audit.evaluate"],
        forbiddenTools: QA_FORBIDDEN_TOOLS,
      },
      {
        profileId: "QAA-01",
        persona: "evidence-qa-agent",
        name: "강태오",
        role: "근거(Evidence) 검증",
        rank: "member",
        skills: ["qa.evidence.check", "qa.evidence.rag"],
        forbiddenTools: QA_FORBIDDEN_TOOLS,
      },
      {
        profileId: "QAA-02",
        persona: "hallucination-critic",
        name: "문세라",
        role: "환각(Hallucination) 검증",
        rank: "member",
        skills: ["qa.evidence.rag"],
        forbiddenTools: QA_FORBIDDEN_TOOLS,
      },
      {
        profileId: "QAA-03",
        persona: "tool-permission-security-reviewer",
        name: "한지오",
        role: "권한·보안 검토",
        rank: "member",
        skills: ["qa.tool_permission.check", "qa.trace.record"],
        forbiddenTools: QA_FORBIDDEN_TOOLS,
      },
      {
        profileId: "QAA-04",
        persona: "model-risk-agent",
        name: "정하은",
        role: "Model Risk",
        rank: "member",
        skills: ["qa.model_risk.evaluate"],
        forbiddenTools: QA_FORBIDDEN_TOOLS,
      },
      {
        profileId: "QAA-05",
        persona: "agent-ops-monitor",
        name: "서유나",
        role: "Agent 운영 모니터링",
        rank: "member",
        skills: ["qa.ops.evaluate", "qa.trace.record"],
        forbiddenTools: QA_FORBIDDEN_TOOLS,
      },
      {
        profileId: "QAA-06",
        persona: "internal-audit-agent",
        name: "배준서",
        role: "내부 감사",
        rank: "member",
        skills: ["qa.internal_audit.evaluate"],
        forbiddenTools: QA_FORBIDDEN_TOOLS,
      },
      {
        profileId: "QAA-07",
        persona: "incident-postmortem-agent",
        name: "조은채",
        role: "인시던트 사후분석",
        rank: "member",
        skills: ["qa.incident.record", "qa.trace.record"],
        forbiddenTools: QA_FORBIDDEN_TOOLS,
      },
    ],
  },
] as const;

export function isRiskQaDepartment(id: string): id is RiskQaDepartmentId {
  return (RISK_QA_DEPARTMENT_IDS as readonly string[]).includes(id);
}
