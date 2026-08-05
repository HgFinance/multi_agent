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
  return body as {
    default_universe_id: string;
    universes: PortfolioUniverseOption[];
  };
}

export async function startPortfolioRecommendation(input: PortfolioInterviewInput): Promise<{ run_id: string; status: string }> {
  const response = await fetch(`${BFF}/ui/portfolio-recommendations`, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(input),
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(explainPortfolioApiError(body, response.status));
  }
  return body as { run_id: string; status: string };
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
): Promise<{ status: string; binding: boolean }> {
  const response = await fetch(`${BFF}/ui/portfolio-recommendations/${encodeURIComponent(runId)}/approval`, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify({ decision }),
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new Error(explainPortfolioApiError(body, response.status));
  }
  return body as { status: string; binding: boolean };
}
