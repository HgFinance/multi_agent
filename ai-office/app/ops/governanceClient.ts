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
  decision: "PENDING" | "APPROVED" | "REJECTED" | "EXPIRED" | "REVOKED" | string;
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
export function assertGovernanceCommandable(connection: "connecting" | "connected" | "stale" | "offline"): void {
  if (connection !== "connected") {
    throw new Error("BFF 연결이 안정적이지 않아 승인·상태 변경을 잠시 차단했습니다.");
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${BFF}${path}`, {
    cache: "no-store",
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    ...init,
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const payload = typeof body === "object" && body !== null ? body as { detail?: unknown; error_code?: unknown; message?: unknown } : {};
    const detail = typeof payload.detail === "string" ? payload.detail : "";
    const code = typeof payload.error_code === "string" ? payload.error_code : detail;
    const message = typeof payload.message === "string" ? payload.message : "";
    if (code === "governance_api_unavailable") throw new Error("CEO Governance API에 연결할 수 없습니다.");
    if (code === "MandatePersistenceError") throw new Error(message || "Mandate를 저장할 수 없습니다. 고급 설정의 Fund ID가 운영 DB의 canonical fund인지 확인하세요.");
    if (code === "MissingActorUserError") throw new Error("대표 승인을 기록하려면 NEXT_PUBLIC_GOVERNANCE_ACTOR_USER_ID에 유효한 사용자 UUID가 필요합니다.");
    if (code === "InvalidUpdateError") throw new Error("승인 Case를 중복 재개했습니다. 승인 상태를 새로고침해 다시 확인하세요.");
    throw new Error(message || detail || `CEO Governance API 오류 (HTTP ${response.status})`);
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
      actor_user_id: process.env.NEXT_PUBLIC_GOVERNANCE_ACTOR_USER_ID?.trim(),
      at: new Date().toISOString(),
    }),
  });
}
