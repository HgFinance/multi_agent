"use client";

import { BFF } from "./readModel";

function explainPortfolioApiError(body: unknown, status: number): string {
  const detail = typeof body === "object" && body !== null && "detail" in body
    ? String((body as { detail?: unknown }).detail)
    : `HTTP ${status}`;
  if (detail === "Not Found" || status === 404) {
    return "포트폴리오 BFF를 찾지 못했습니다. FastAPI BFF를 8001 포트로 실행하세요.";
  }
  return detail;
}
function requireObject(body: unknown, label: string): Record<string, unknown> {
  if (typeof body !== "object" || body === null || Array.isArray(body)) {
    throw new Error(`${label} 계약이 올바르지 않습니다.`);
  }
  return body as Record<string, unknown>;
}
function requireArray(body: Record<string, unknown>, key: string, label: string): unknown[] {
  const value = body[key];
  if (!Array.isArray(value)) throw new Error(`${label} 계약의 ${key}가 없습니다.`);
  return value;
}

function requireString(body: Record<string, unknown>, key: string, label: string): string {
  const value = body[key];
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${label} 계약의 ${key}가 없습니다.`);
  }
  return value;
}
function requireBoolean(body: Record<string, unknown>, key: string, label: string): boolean {
  const value = body[key];
  if (typeof value !== "boolean") throw new Error(`${label} 계약의 ${key}가 올바르지 않습니다.`);
  return value;
}

function parseRecommendationResult(value: unknown): Record<string, unknown> | null {
  if (value == null) return null;
  const result = requireObject(value, "추천 결과");
  requireString(result, "pipeline_status", "추천 결과");
  requireString(result, "workflow", "추천 결과");
  requireString(result, "trace_id", "추천 결과");
  requireString(result, "safe_action", "추천 결과");
  requireBoolean(result, "binding", "추천 결과");
  requireBoolean(result, "production_enabled", "추천 결과");
  requireBoolean(result, "manual_review_required", "추천 결과");
  requireObject(result, "data_context", "추천 결과");
  requireObject(result, "risk_gate", "추천 결과");
  requireObject(result, "qa_gate", "추천 결과");
  requireObject(result, "department_reports", "추천 결과");
  requireArray(result, "worker_reports", "추천 결과");
  requireArray(result, "pipeline_events", "추천 결과");
  return result;
}

export type PortfolioInterviewInput = {
  user_id: string;
  mindset: "SAFETY_FIRST" | "BALANCED" | "RISK_SEEKING";
  experience: "BEGINNER" | "INTERMEDIATE" | "EXPERIENCED";
  investment_horizon_years: number;
  max_drawdown_pct: string;
  investment_amount: string;
  currency: "KRW" | "USD" | "EUR";
  universe_id: string;
  category: string;
  include_stock: boolean;
  include_derivatives: boolean;
  query: string;
  trace_id?: string;
  mandate_id?: string;
  case_id?: string;
  mandate_version_id?: string;
  policy_hash?: string;
  max_sector_weight_pct?: number;
  max_gross_exposure_pct?: number;
  max_daily_loss_pct?: number;
  allowed_asset_classes?: string[];
  forbidden_asset_classes?: string[];
};

export type PortfolioUniverseOption = {
  universe_id: string;
  name: string;
  description: string;
  status: string;
  source: string;
  instrument_count: number;
};

export async function fetchPortfolioUniverses(): Promise<{
  default_universe_id: string;
  universes: PortfolioUniverseOption[];
}> {
  const response = await fetch(`${BFF}/ui/portfolio-universes`, {
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(explainPortfolioApiError(body, response.status));
  }
  const payload = requireObject(body, "유니버스 조회");
  const universes = requireArray(payload, "universes", "유니버스 조회").map((item) => {
    const option = requireObject(item, "유니버스 항목");
    const instrumentCount = option.instrument_count;
    if (typeof instrumentCount !== "number" || !Number.isInteger(instrumentCount) || instrumentCount < 0) {
      throw new Error("유니버스 항목 계약의 instrument_count가 올바르지 않습니다.");
    }
    return {
      universe_id: requireString(option, "universe_id", "유니버스 항목"),
      name: requireString(option, "name", "유니버스 항목"),
      description: typeof option.description === "string" ? option.description : "",
      status: requireString(option, "status", "유니버스 항목"),
      source: typeof option.source === "string" ? option.source : "",
      instrument_count: instrumentCount,
    };
  });
  return {
    default_universe_id: requireString(payload, "default_universe_id", "유니버스 조회"),
    universes,
  };
}

export async function startPortfolioRecommendation(input: PortfolioInterviewInput): Promise<{
  run_id: string;
  status: string;
  trace_id?: string;
  case_id?: string | null;
  mandate_id?: string | null;
  mandate_version_id?: string | null;
  policy_hash?: string | null;
  input_hash?: string;
}> {
  const response = await fetch(`${BFF}/ui/portfolio-recommendations`, {
    method: "POST",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...(input.trace_id ? { "Idempotency-Key": input.trace_id } : {}),
      ...(input.user_id ? { "X-User-Id": input.user_id } : {}),
    },
    body: JSON.stringify(input),
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(explainPortfolioApiError(body, response.status));
  }
  const payload = requireObject(body, "추천 시작");
  return {
    run_id: requireString(payload, "run_id", "추천 시작"),
    status: requireString(payload, "status", "추천 시작"),
    trace_id: typeof payload.trace_id === "string" ? payload.trace_id : undefined,
    case_id: typeof payload.case_id === "string" ? payload.case_id : null,
    mandate_id: typeof payload.mandate_id === "string" ? payload.mandate_id : null,
    mandate_version_id: typeof payload.mandate_version_id === "string" ? payload.mandate_version_id : null,
    policy_hash: typeof payload.policy_hash === "string" ? payload.policy_hash : null,
    input_hash: typeof payload.input_hash === "string" ? payload.input_hash : undefined,
  };
}

export async function startSavedPortfolioRecommendation(): Promise<{ run_id: string; status: string }> {
  if (typeof window === "undefined") throw new Error("Mandate 설정은 브라우저에서만 다시 시작할 수 있습니다.");
  let saved: { draft?: Partial<PortfolioInterviewInput & { objective: string; allowed_assets: Record<string, boolean> }> };
  try {
    saved = JSON.parse(window.localStorage.getItem("hgfinance.mandate-config.v1") || "null") as typeof saved;
  } catch {
    throw new Error("저장된 Mandate 초안을 읽을 수 없습니다. Mandate 설정에서 다시 저장하세요.");
  }
  const draft = saved?.draft;
  if (!draft?.user_id || !draft.investment_amount || !draft.universe_id) {
    throw new Error("저장된 Mandate가 없습니다. Mandate 설정을 먼저 저장하세요.");
  }
  return startPortfolioRecommendation({
    user_id: draft.user_id,
    mindset: draft.mindset ?? "SAFETY_FIRST",
    experience: draft.experience ?? "BEGINNER",
    investment_horizon_years: draft.investment_horizon_years ?? 3,
    max_drawdown_pct: draft.max_drawdown_pct ?? "0.10",
    investment_amount: draft.investment_amount,
    currency: draft.currency ?? "KRW",
    universe_id: draft.universe_id,
    category: draft.category ?? "PORTFOLIO_RECOMMENDATION",
    include_stock: draft.allowed_assets?.stock ?? draft.include_stock ?? true,
    include_derivatives: Boolean(
      draft.allowed_assets?.futures || draft.allowed_assets?.options || draft.allowed_assets?.derivatives || draft.include_derivatives,
    ),
    query: draft.objective ?? draft.query ?? "",
  });
}

export async function decidePortfolioRecommendation(
  runId: string,
  decision: "APPROVE" | "REJECT",
  ownerId?: string,
): Promise<{ status: string; binding: boolean }> {
  const response = await fetch(`${BFF}/ui/portfolio-recommendations/${encodeURIComponent(runId)}/approval`, {
    method: "POST",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...(ownerId ? { "X-User-Id": ownerId } : {}),
    },
    body: JSON.stringify({ decision }),
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(explainPortfolioApiError(body, response.status));
  }
  const payload = requireObject(body, "추천 승인");
  return {
    status: requireString(payload, "status", "추천 승인"),
    binding: payload.binding === true,
  };
}

export type PortfolioRunStatus = {
  run_id: string;
  workflow: string;
  status: string;
  phase: string;
  updated_at: string;
  profile_user_id: string;
  trace_id: string;
  case_id: string | null;
  mandate_id: string | null;
  mandate_version_id: string | null;
  policy_hash: string | null;
  input_hash: string;
  result: Record<string, unknown> | null;
  error: string | null;
};

export async function fetchPortfolioRecommendation(runId: string, ownerId?: string): Promise<PortfolioRunStatus> {
  const response = await fetch(`${BFF}/ui/portfolio-recommendations/${encodeURIComponent(runId)}`, {
    cache: "no-store",
    headers: {
      Accept: "application/json",
      ...(ownerId ? { "X-User-Id": ownerId } : {}),
    },
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(explainPortfolioApiError(body, response.status));
  }
  const payload = requireObject(body, "추천 실행 상태");
  return {
    run_id: requireString(payload, "run_id", "추천 실행 상태"),
    workflow: requireString(payload, "workflow", "추천 실행 상태"),
    status: requireString(payload, "status", "추천 실행 상태"),
    phase: requireString(payload, "phase", "추천 실행 상태"),
    updated_at: requireString(payload, "updated_at", "추천 실행 상태"),
    profile_user_id: requireString(payload, "profile_user_id", "추천 실행 상태"),
    trace_id: typeof payload.trace_id === "string" ? payload.trace_id : "",
    case_id: typeof payload.case_id === "string" ? payload.case_id : null,
    mandate_id: typeof payload.mandate_id === "string" ? payload.mandate_id : null,
    mandate_version_id: typeof payload.mandate_version_id === "string" ? payload.mandate_version_id : null,
    policy_hash: typeof payload.policy_hash === "string" ? payload.policy_hash : null,
    input_hash: requireString(payload, "input_hash", "추천 실행 상태"),
    result: parseRecommendationResult(payload.result),
    error: typeof payload.error === "string" ? payload.error : null,
  };
}
