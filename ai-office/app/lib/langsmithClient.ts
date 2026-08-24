import { bffFetch } from "./bffClient";

/**
 * QA 부서 카드용 LangSmith trace 집계 client — BFF `/ui/qa/observability/langsmith` 조회.
 *
 * `LANGSMITH_API_KEY`는 BFF(`apps/api/langsmith_traces.py`) 밖으로 나가지 않는다.
 * 브라우저는 날짜별 집계 숫자만 받는다 - AI Office CLAUDE.md 규칙대로 자격증명이
 * 필요한 외부 서비스를 브라우저가 직접 부르지 않는다.
 */

export type LangsmithStatus = "READY" | "NOT_CONFIGURED" | "ERROR";

export type LangsmithDailyTrace = {
  date: string;
  success: number;
  error: number;
};

export type LangsmithDailyLatency = {
  date: string;
  p50_seconds: number | null;
  p99_seconds: number | null;
};

export type LangsmithQaTraces = {
  status: LangsmithStatus;
  configured: boolean;
  project: string | null;
  days: number;
  generated_at: string;
  trace_count: number;
  error_rate_pct: number | null;
  daily: LangsmithDailyTrace[];
  latency: LangsmithDailyLatency[];
  detail?: string;
};

export type LangsmithFeedbackItem = {
  artifact_id: string;
  source_run_id: string;
  eval_run_id: string;
  department: string;
  decision: string;
  score: number | null;
  finding_codes: string[];
  summaries: string[];
  metadata: Record<string, unknown>;
  created_at: string;
};

export type LangsmithFeedbackPending = {
  status: string;
  items: LangsmithFeedbackItem[];
};

export type DepartmentFeedbackReview = {
  review_id: string;
  target_department: string;
  reviewer_department: string;
  reviewer_user_id: string;
  comment: string;
  created_at: string;
};

export type DepartmentFeedbackItem = LangsmithFeedbackItem & {
  review_count: number;
  reviews: DepartmentFeedbackReview[];
};

export type DepartmentFeedbackResponse = {
  status: string;
  department: string;
  items: DepartmentFeedbackItem[];
};

function explainError(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return `LangSmith 집계 조회 실패 (HTTP ${status})`;
}

export async function fetchQaLangsmithTraces(days = 7): Promise<LangsmithQaTraces> {
  let response: Response;
  try {
    response = await bffFetch(`/ui/qa/observability/langsmith?days=${encodeURIComponent(String(days))}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch {
    throw new Error("BFF에 연결하지 못해 LangSmith 집계를 가져오지 못했습니다.");
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  return body as LangsmithQaTraces;
}

export async function fetchQaLangsmithFeedback(limit = 50): Promise<LangsmithFeedbackPending> {
  const response = await bffFetch(
    `/ui/qa/observability/feedback/pending?limit=${encodeURIComponent(String(limit))}`,
    { cache: "no-store", headers: { Accept: "application/json" } },
  );
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  return body as LangsmithFeedbackPending;
}

export async function decideQaLangsmithFeedback(
  artifactId: string,
  decision: "APPROVED" | "REJECTED",
  reason: string,
): Promise<{ status: string; artifact_id: string }> {
  const response = await bffFetch(
    `/ui/qa/observability/feedback/${encodeURIComponent(artifactId)}/decision`,
    {
      method: "POST",
      cache: "no-store",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ decision, reason }),
    },
  );
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  return body as { status: string; artifact_id: string };
}

export async function fetchDepartmentLangsmithFeedback(
  department: string,
  limit = 50,
): Promise<DepartmentFeedbackResponse> {
  const response = await bffFetch(
    `/ui/departments/${encodeURIComponent(department)}/observability/feedback?limit=${encodeURIComponent(String(limit))}`,
    { cache: "no-store", headers: { Accept: "application/json" } },
  );
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  return body as DepartmentFeedbackResponse;
}

export async function addDepartmentLangsmithFeedback(
  department: string,
  artifactId: string,
  comment: string,
): Promise<{ status: string; review_id: string; artifact_id: string }> {
  const response = await bffFetch(
    `/ui/departments/${encodeURIComponent(department)}/observability/feedback/${encodeURIComponent(artifactId)}`,
    {
      method: "POST",
      cache: "no-store",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ comment }),
    },
  );
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  return body as { status: string; review_id: string; artifact_id: string };
}
