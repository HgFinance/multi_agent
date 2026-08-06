"use client";

import { BFF } from "./readModel";

export type RiskMandateAssessment = {
  mandate_id: string;
  pipeline_status: string;
  employees: Record<string, { status?: string; verdict?: string; severity?: string; action_required?: boolean }>;
  risk_head: { decision?: string; safe_action?: string; manual_approval_required?: boolean };
};

export async function assessRiskMandate(
  mandateId: string,
  body: Record<string, unknown>,
): Promise<RiskMandateAssessment> {
  const response = await fetch(`${BFF}/ui/risk/mandates/${encodeURIComponent(mandateId)}/assess`, {
    method: "POST",
    cache: "no-store",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = typeof payload === "object" && payload !== null && "detail" in payload
      ? String((payload as { detail?: unknown }).detail)
      : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  if (typeof payload !== "object" || payload === null || !("risk_head" in payload)) {
    throw new Error("Risk API 응답 계약이 올바르지 않습니다.");
  }
  return payload as RiskMandateAssessment;
}
