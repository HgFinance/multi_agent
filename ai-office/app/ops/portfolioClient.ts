"use client";

import { BFF } from "./readModel";

function explainPortfolioApiError(body: unknown, status: number): string {
  const detail = typeof body === "object" && body !== null && "detail" in body
    ? String((body as { detail?: unknown }).detail)
    : `HTTP ${status}`;
  if (detail === "Not Found" || status === 404) {
    return "포트폴리오 BFF를 찾지 못했습니다. FastAPI BFF를 8000 포트로 실행하세요.";
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
};

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
