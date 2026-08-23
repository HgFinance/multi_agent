/**
 * HR 조직 구성·개선 client — BFF `/ui/workforce/{hiring-requests,improvements,workforce-plans}`.
 *
 * 세 자원(채용 제안·자기 개선 후보·인력 계획)은 서로 행 단위 관계가 없는 독립
 * 리스트다 - 칸반 컬럼 세 개를 병렬로 그리는 것과 같아서 백엔드에서 하나로
 * 조인할 이유가 없다(Roster+Access와 다르다). 각자 얇은 BFF 프록시를 그대로
 * 쓰고, 이 화면이 세 번 fetch해서 컬럼별로 그린다.
 *
 * hiring-requests/improvements/workforce-plans의 workforce-api 쪽 저장소는
 * roster/scorecard와 달리 DATABASE_URL 미설정 시 501이 아니라 In-Memory로
 * 조용히 대체된다(api/app.py 머리말) - 빈 목록이 "연동 안 됨"인지 "정말 0건"인지
 * 이 client만으로는 구분할 수 없다.
 */

import { BFF, bffFetch } from "./bffClient";

export type HiringRequestStatus = "DRAFT" | "OPEN" | "EVALUATING" | "APPROVED" | "REJECTED" | "CLOSED";

export type HiringRequest = {
  request_id: string;
  department_id: string;
  business_problem: string;
  evidence: Record<string, unknown>;
  required_capabilities: Record<string, unknown>;
  budget: Record<string, unknown>;
  status: HiringRequestStatus | string;
  trace_id: string;
  created_at: string;
  requested_by: string;
  decided_by: string | null;
  decided_at: string | null;
  decision_reason: string | null;
};

export type CandidateStatus =
  | "PROPOSED"
  | "EVALUATING"
  | "SHADOW"
  | "PENDING_APPROVAL"
  | "APPROVED"
  | "REJECTED"
  | "HOLD"
  | "DEPLOYED"
  | "OBSERVING"
  | "KEPT"
  | "ROLLED_BACK"
  | "RETIRED";

export type ImprovementCandidate = {
  candidate_id: string;
  author: string;
  target_type: "SKILL" | "PROFILE" | "WORKFLOW" | "AGENT" | string;
  target_ref: string;
  target_current_version: number;
  evidence_ids: string[];
  expected_effect: string;
  risk_class: "LOW" | "MEDIUM" | "HIGH" | string;
  rollback_target_version: number;
  status: CandidateStatus | string;
};

export type WorkforcePlanStatus = "DRAFT" | "APPROVED" | "ACTIVE" | "RETIRED";

export type WorkforcePlan = {
  plan_id: string;
  department_id: string;
  period_start: string;
  period_end: string;
  skill_gaps: Record<string, unknown>;
  actions: unknown[];
  budget: Record<string, unknown>;
  assumptions: Record<string, unknown>;
  status: WorkforcePlanStatus | string;
  approval_id: string | null;
};

export class WorkforceLifecycleError extends Error {
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

function explain(body: unknown, status: number, fallback: string): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (typeof detail === "object" && detail !== null && "message" in detail) {
      const message = (detail as { message?: unknown }).message;
      if (typeof message === "string" && message.trim()) return message;
    }
  }
  return `${fallback} (HTTP ${status})`;
}

async function getJson<T>(path: string, hasShape: (value: unknown) => value is T, label: string): Promise<T> {
  let response: Response;
  try {
    response = await bffFetch(path, { cache: "no-store", headers: { Accept: "application/json" } });
  } catch {
    throw new WorkforceLifecycleError(
      `BFF(${BFF})에 연결하지 못했습니다. FastAPI BFF가 실행 중인지 확인하세요.`,
      0,
    );
  }
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new WorkforceLifecycleError(explain(body, response.status, `${label} 조회 실패`), response.status);
  if (!hasShape(body)) throw new WorkforceLifecycleError(`${label} 응답 계약이 올바르지 않습니다.`, response.status);
  return body;
}

function hasHiringShape(value: unknown): value is { hiring_requests: HiringRequest[] } {
  return typeof value === "object" && value !== null && Array.isArray((value as Record<string, unknown>).hiring_requests);
}

function hasImprovementsShape(value: unknown): value is { candidates: ImprovementCandidate[] } {
  return typeof value === "object" && value !== null && Array.isArray((value as Record<string, unknown>).candidates);
}

function hasPlansShape(value: unknown): value is { workforce_plans: WorkforcePlan[] } {
  return typeof value === "object" && value !== null && Array.isArray((value as Record<string, unknown>).workforce_plans);
}

export async function fetchHiringRequests(): Promise<{ hiring_requests: HiringRequest[] }> {
  return getJson("/ui/workforce/hiring-requests", hasHiringShape, "채용 제안");
}

export async function fetchImprovementCandidates(): Promise<{ candidates: ImprovementCandidate[] }> {
  return getJson("/ui/workforce/improvements", hasImprovementsShape, "자기 개선 후보");
}

export async function fetchWorkforcePlans(): Promise<{ workforce_plans: WorkforcePlan[] }> {
  return getJson("/ui/workforce/workforce-plans", hasPlansShape, "인력 계획");
}
