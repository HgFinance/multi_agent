/**
 * Trading·Accounting Portfolio Read Model client.
 *
 * 화면은 `/ui/snapshot`의 값을 표시만 한다. 금액·수량을 JavaScript number로
 * 바꾸지 않아 Decimal 정밀도와 DEMO/PAPER 출처 구분을 그대로 유지한다.
 */

import { BFF } from "./ceoClient";
import { currentFundId, withAccountHeaders } from "./currentAccount";
import { loadMandateForFund } from "./mandateClient";

export type SnapshotMode = "DEMO" | "PAPER" | "LIVE";

export type PortfolioPosition = {
  instrument_id: string;
  quantity: string | null;
  average_cost: string | null;
  mark_price: string | null;
  mark_as_of: string;
  market_value: string | null;
  unrealized_pnl: string | null;
  weight: string | null;
};

export type AssetAllocation = {
  /** Strategy/instrument identity. The UI must not assume cash/securities. */
  key: string;
  /** Optional short code for compact chips such as 005930. */
  code?: string | null;
  label: string;
  value: string | null;
  weight: string | null;
};

export type PortfolioSnapshot = {
  as_of: string;
  nav: string | null;
  cash: string | null;
  securities_value: string | null;
  realized_pnl: string | null;
  unrealized_pnl: string | null;
  allocation: AssetAllocation[];
  positions: PortfolioPosition[];
};

export type BrokerOrderSnapshot = {
  order_id: string;
  order_intent_id: string;
  client_order_id: string;
  broker_order_id: string | null;
  broker_adapter: string;
  state: string;
  side: string;
  instrument_id: string;
  requested_quantity: string | null;
  filled_quantity: string | null;
  average_fill_price: string | null;
  fill_count: number;
  is_terminal: boolean;
};

export type OrderIntentSnapshot = {
  order_intent_id: string;
  state: string;
  requested_quantity: string | null;
  risk_decision_id: string | null;
  risk_approved_qty: string | null;
  valid_until: string;
};

export type PortfolioReadModel = {
  schema_version: number;
  mode: SnapshotMode;
  snapshot_version: number;
  server_time: string;
  portfolio: PortfolioSnapshot;
  trading: {
    intents: OrderIntentSnapshot[];
    orders: BrokerOrderSnapshot[];
    blocked_by_unknown: boolean;
  };
  ledger: {
    journal_count: number;
    reversal_count: number;
    balanced: boolean;
  };
  sources: Record<string, string>;
};

function explainError(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return `포트폴리오 Snapshot 조회 실패 (HTTP ${status})`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function hasSnapshotShape(value: unknown): value is PortfolioReadModel {
  if (!isRecord(value)) return false;
  const portfolio = value.portfolio;
  const trading = value.trading;
  const ledger = value.ledger;
  return (
    (value.mode === "DEMO" || value.mode === "PAPER" || value.mode === "LIVE") &&
    isRecord(portfolio) &&
    Array.isArray(portfolio.positions) &&
    isRecord(trading) &&
    Array.isArray(trading.intents) &&
    Array.isArray(trading.orders) &&
    isRecord(ledger) &&
    typeof ledger.balanced === "boolean" &&
    isRecord(value.sources)
  );
}

function normalizeSnapshot(value: PortfolioReadModel): PortfolioReadModel {
  // Older BFFs expose cash/securities here. Do not reinterpret those as
  // strategy weights; until the strategy projection arrives, keep allocation
  // empty instead of inventing strategy weights from unrelated fields.
  const allocation = Array.isArray(value.portfolio.allocation)
    ? value.portfolio.allocation.filter((item) => !["cash", "securities"].includes(item.key))
    : [];
  return {
    ...value,
    portfolio: {
      ...value.portfolio,
      allocation,
    },
  };
}

async function loadMandateNav(): Promise<string | null> {
  const fundId = currentFundId();
  if (!fundId) return null;
  const mandate = await loadMandateForFund(fundId);
  return mandate?.policy?.risk_bounds?.base_capital ?? null;
}

function applyPreTradingProjection(
  value: PortfolioReadModel,
  mandateNav: string | null,
): PortfolioReadModel {
  return {
    ...value,
    portfolio: {
      ...value.portfolio,
      // 트레이딩/OMS 연결 전에는 Snapshot NAV를 쓰지 않고 Mandate 기준 자본만 표시한다.
      nav: mandateNav,
      cash: "0",
      securities_value: "0",
      gross_exposure: "0",
      net_exposure: "0",
      realized_pnl: "0",
      unrealized_pnl: "0",
      fees: "0",
      taxes: "0",
      allocation: [],
      positions: [],
    },
    trading: {
      intents: [],
      orders: [],
      blocked_by_unknown: false,
    },
    ledger: {
      journal_count: 0,
      reversal_count: 0,
      balanced: true,
    },
  };
}

export async function fetchPortfolioSnapshot(): Promise<PortfolioReadModel> {
  const fundId = currentFundId();
  const query = fundId ? `?fund_id=${encodeURIComponent(fundId)}` : "";
  let response: Response;
  try {
    response = await fetch(`${BFF}/ui/snapshot${query}`, {
      cache: "no-store",
      headers: withAccountHeaders({ Accept: "application/json" }),
    });
  } catch {
    throw new Error(
      `포트폴리오 Snapshot(BFF ${BFF})에 연결하지 못했습니다. FastAPI BFF가 실행 중인지 확인하세요.`,
    );
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  if (!hasSnapshotShape(body)) {
    throw new Error("포트폴리오 Snapshot 응답 계약이 올바르지 않습니다.");
  }
  const normalized = normalizeSnapshot(body);
  const mandateNav = await loadMandateNav();
  return applyPreTradingProjection(normalized, mandateNav);
}
