"use client";

import { BFF } from "./readModel";

export type PortfolioInterviewInput = {
  user_id: string;
  mindset: "SAFETY_FIRST" | "BALANCED" | "RISK_SEEKING";
  experience: "BEGINNER" | "INTERMEDIATE" | "EXPERIENCED";
  investment_horizon_years: number;
  max_drawdown_pct: string;
  liquidity_need: "HIGH" | "MEDIUM" | "LOW";
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
    const detail = typeof body === "object" && body !== null && "detail" in body
      ? String((body as { detail?: unknown }).detail)
      : `HTTP ${response.status}`;
    throw new Error(detail);
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
    const detail = typeof body === "object" && body !== null && "detail" in body
      ? String((body as { detail?: unknown }).detail)
      : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return body as { status: string; binding: boolean };
}
