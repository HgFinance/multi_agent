import { BFF, bffFetch } from "./bffClient";

export const MARKET_RANKING_KINDS = ["volume", "change", "amount"] as const;
export type MarketRankingKind = (typeof MARKET_RANKING_KINDS)[number];

export type MarketRankingRow = {
  rank: number;
  symbol: string | null;
  name: string | null;
  price: string | null;
  change: string | null;
  change_rate: string | null;
  volume: string | null;
  amount: string | null;
};

export type MarketRankingResponse = {
  schema_version: "market.rankings.v1" | string;
  as_of: string;
  source: string;
  kind: MarketRankingKind;
  label: string;
  metric_label: string;
  rows: MarketRankingRow[];
};

export class MarketRankingError extends Error {
  readonly status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

function explain(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return `시장 상위 종목 조회 실패 (HTTP ${status})`;
}

function isMarketRanking(value: unknown): value is MarketRankingResponse {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.schema_version === "string" &&
    typeof candidate.kind === "string" &&
    MARKET_RANKING_KINDS.includes(candidate.kind as MarketRankingKind) &&
    typeof candidate.label === "string" &&
    typeof candidate.metric_label === "string" &&
    Array.isArray(candidate.rows)
  );
}

export async function fetchMarketRanking(kind: MarketRankingKind): Promise<MarketRankingResponse> {
  let response: Response;
  try {
    response = await bffFetch(`/ui/market/rankings?kind=${kind}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch {
    throw new MarketRankingError(`BFF(${BFF})에 연결하지 못했습니다.`, 0);
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new MarketRankingError(explain(body, response.status), response.status);
  if (!isMarketRanking(body)) {
    throw new MarketRankingError("시장 상위 종목 응답 계약이 올바르지 않습니다.", response.status);
  }
  return body;
}
