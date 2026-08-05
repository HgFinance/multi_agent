"use client";

import { BFF } from "./readModel";

export type MandateChange = {
  stage: string;
  mandate_id: string;
  version: number;
  direction: string;
  case_id: string | null;
  detail: string;
};

export type MandateApproval = {
  approval_id: string;
  object_id: string;
  required_role: "RISK" | "QA" | "USER" | string;
  decision: "PENDING" | "APPROVED" | "REJECTED" | "EXPIRED" | string;
  reason: string | null;
  expires_at: string | null;
  decided_at: string | null;
};

export type MandateTimelineEvent = {
  sequence: number;
  event_type: string;
  to_status: string;
  payload: Record<string, unknown>;
  occurred_at: string;
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BFF}${path}`, {
    cache: "no-store",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    ...init,
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = typeof body === "object" && body !== null && "detail" in body
      ? String((body as { detail?: unknown }).detail)
      : `HTTP ${response.status}`;
    throw new Error(detail === "governance_api_unavailable" ? "CEO Governance API에 연결할 수 없습니다." : detail);
  }
  return body as T;
}

export function submitMandateChange(mandateId: string, body: Record<string, unknown>) {
  return request<MandateChange>(`/ui/mandates/${encodeURIComponent(mandateId)}/change-requests`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function fetchCurrentMandate(mandateId: string) {
  return request<{ mandate_id: string; current_version: number; status: string }>(
    `/ui/mandates/${encodeURIComponent(mandateId)}/current`,
    { method: "GET" },
  );
}

export function fetchMandateTimeline(caseId: string) {
  return request<{ events: MandateTimelineEvent[] }>(
    `/ui/mandate-cases/${encodeURIComponent(caseId)}/timeline`,
    { method: "GET" },
  );
}

export function fetchMandateApprovals(objectId: string) {
  return request<{ approvals: MandateApproval[] }>(
    `/ui/mandate-approvals?object_type=MANDATE_VERSION&object_id=${encodeURIComponent(objectId)}`,
    { method: "GET" },
  );
}

export function advanceMandateCase(caseId: string) {
  return request<MandateChange>(`/ui/mandate-cases/${encodeURIComponent(caseId)}/advance`, {
    method: "POST",
    body: JSON.stringify({ at: new Date().toISOString() }),
  });
}

export function decideMandateApproval(approvalId: string, decision: "APPROVED" | "REJECTED") {
  return request<MandateApproval>(`/ui/mandate-approvals/${encodeURIComponent(approvalId)}/decide`, {
    method: "POST",
    body: JSON.stringify({
      decision,
      actor_user_id: process.env.NEXT_PUBLIC_GOVERNANCE_ACTOR_USER_ID?.trim() || "web-user",
      at: new Date().toISOString(),
    }),
  });
}
