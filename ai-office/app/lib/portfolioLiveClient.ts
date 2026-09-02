/**
 * 실시간 포트폴리오 client — BFF `/ui/portfolio/live` 폴링.
 *
 * 브로커가 무엇인지는 **화면이 알지 않는다.** BFF가 접수/체결/정정/취소/거부와
 * 잔고라는 도메인 어휘로만 내려 주므로 브로커를 바꿔도 이 파일은 그대로다.
 *
 * 금액·수량은 문자열 그대로 둔다 — JavaScript number는 double이라 Decimal이
 * 깨진다(`readModel.ts` 규칙 1).
 *
 * 이 값은 브로커 장부이지 우리 원장이 아니다. 응답의 `authoritative: false`를
 * 화면이 "비공식" 배지로 그대로 드러낸다.
 */

import { BFF, bffFetch } from "./bffClient";
import { subscribeBffSse } from "./sseClient";

/** 주문 생명주기. 화면 순서도 이 순서다. */
export const ORDER_KINDS = ["ACCEPTED", "FILLED", "AMENDED", "CANCELLED", "REJECTED"] as const;
export type OrderKind = (typeof ORDER_KINDS)[number];

export type OrderEvent = {
  seq: number;
  kind: OrderKind | string;
  /** 접수 / 체결 / 정정 / 취소 / 거부. 라벨의 정본은 서버다. */
  label: string;
  received_at: string;
  /** 원본은 `HHMMSSmmm` 문자열이다. 서버가 해석하지 않고 그대로 넘긴다. */
  event_time: string | null;
  order_no: string | null;
  orig_order_no: string | null;
  symbol: string | null;
  symbol_name: string | null;
  side: string | null;
  quantity: string | null;
  price: string | null;
  unfilled_quantity: string | null;
  /** LS 주문번호를 기존 권위 주문 감사기록과 결합한 결과다. */
  correlation_status?: "ATTRIBUTED" | "UNATTRIBUTED" | string;
  correlation_source?: string | null;
  origin?: string | null;
  internal_broker_order_id?: string | null;
  directive_id?: string | null;
  directive_state?: string | null;
  directive_leg_state?: string | null;
  order_request_id?: string | null;
  client_request_id?: string | null;
  request_source?: string | null;
  conditional_rule_id?: string | null;
  conditional_rule_state?: string | null;
};

export type Holding = {
  symbol: string | null;
  name: string | null;
  quantity: string | null;
  sellable_quantity: string | null;
  average_cost: string | null;
  purchase_amount: string | null;
  last_price: string | null;
  market_value: string | null;
  unrealized_pnl: string | null;
  return_rate: string | null;
  weight: string | null;
};

export type TodayTradingActivityData = {
  trade_count: number;
  summary: {
    buy_quantity: string | null;
    sell_quantity: string | null;
    buy_amount: string | null;
    sell_amount: string | null;
    total_amount: string | null;
    total_fee: string | null;
    total_tax: string | null;
    total_settlement: string | null;
  };
};

export type PortfolioLive = {
  schema_version: string;
  environment: "PAPER" | "LIVE" | string;
  environment_label: string;
  account: {
    registered: boolean;
    /** 뒤 4자리만 온다. 서버가 자르므로 전체 번호는 브라우저에 없다. */
    masked: string | null;
    error: string | null;
  };
  stream: {
    status: "IDLE" | "CONNECTED" | "DISCONNECTED" | "STOPPED" | string;
    error: string | null;
    connected_at: string | null;
  };
  orders: {
    kinds: { kind: string; label: string }[];
    counts: Record<string, number>;
    recent: OrderEvent[];
    source?: string;
    error?: string | null;
    correlation?: {
      status: "READY" | "DEGRADED" | string;
      source: string;
      attributed: number;
      unattributed: number;
      error: string | null;
    };
  };
  holdings: {
    as_of: string | null;
    error: string | null;
    /** 로컬 상태와 브로커 잔고가 일치하는가. 아직 확인 전이면 null. */
    synced: boolean | null;
    drift: { symbol: string; local: string; broker: string }[];
    net_asset: string | null;
    realized_pnl: string | null;
    purchase_amount: string | null;
    valuation: string | null;
    valuation_pnl: string | null;
    rows: Holding[];
  };
  today_activity?: {
    as_of: string | null;
    error: string | null;
    data: TodayTradingActivityData | null;
  };
  server_time: string;
  authoritative: boolean;
  official_nav_source: string;
};

export class PortfolioLiveError extends Error {
  /** 연동이 꺼진 것(503)과 실제 장애를 화면이 구분해서 안내한다. */
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
  return `포트폴리오 조회 실패 (HTTP ${status})`;
}

function hasLiveShape(value: unknown): value is PortfolioLive {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  const orders = candidate.orders as Record<string, unknown> | undefined;
  const holdings = candidate.holdings as Record<string, unknown> | undefined;
  return (
    typeof candidate.environment === "string" &&
    typeof candidate.account === "object" &&
    candidate.account !== null &&
    typeof candidate.stream === "object" &&
    candidate.stream !== null &&
    !!orders &&
    Array.isArray(orders.recent) &&
    typeof orders.counts === "object" &&
    !!holdings &&
    Array.isArray(holdings.rows)
  );
}

/**
 * FEED가 바뀌었다는 사실만 나르는 리비전. 값 자체는 화면이 쓰지 않고,
 * 이전과 다른지만 본다 - 실제 데이터는 `fetchPortfolioLive`가 가져온다.
 */
export type PortfolioLiveRevision = {
  seq: number;
  stream_status: string;
  holdings_as_of: string | null;
  today_activity_as_of: string | null;
};

/**
 * 거래 신호 구독. 주문이 접수·체결·정정·취소·거부되거나 잔고가 다시 맞춰지면
 * 콜백이 불린다.
 *
 * 폴링을 대체하지 않는다. 이 채널이 끊겨도 화면은 기존 주기로 계속 갱신되고,
 * 여기서는 지연만 사라진다. 스트림은 서버가 25초쯤에 스스로 닫고
 * `subscribeBffSse`가 마지막 리비전을 커서로 들고 다시 붙으므로, 아무 일도
 * 없는 계좌는 재연결해도 콜백이 불리지 않는다.
 */
export function subscribePortfolioLiveRevision(
  onRevision: (revision: PortfolioLiveRevision) => void,
): () => void {
  return subscribeBffSse({
    path: (cursor) =>
      cursor
        ? `/ui/portfolio/live/signal?after=${encodeURIComponent(cursor)}`
        : "/ui/portfolio/live/signal",
    onEvent: (event) => {
      if (event.event !== "revision") return;
      try {
        onRevision(JSON.parse(event.data) as PortfolioLiveRevision);
      } catch {
        // 깨진 프레임 하나가 구독을 끊게 두지 않는다. 다음 신호나 폴링이 받는다.
      }
    },
  });
}

export async function fetchPortfolioLive(limit = 50): Promise<PortfolioLive> {
  let response: Response;
  try {
    response = await bffFetch(`/ui/portfolio/live?limit=${limit}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch {
    throw new PortfolioLiveError(
      `BFF(${BFF})에 연결하지 못했습니다. FastAPI BFF가 실행 중인지 확인하세요.`,
      0,
    );
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new PortfolioLiveError(explain(body, response.status), response.status);
  if (!hasLiveShape(body)) {
    throw new PortfolioLiveError("포트폴리오 응답 계약이 올바르지 않습니다.", response.status);
  }
  return body;
}

/** `HHMMSSmmm` → `HH:MM:SS`. 해석 못 하는 값은 그대로 보여 준다. */
export function formatEventTime(value: string | null): string {
  if (!value) return "—";
  const digits = value.replace(/\D/g, "");
  if (digits.length < 6) return value;
  return `${digits.slice(0, 2)}:${digits.slice(2, 4)}:${digits.slice(4, 6)}`;
}

/** 천 단위 구분만 넣는다. Number로 바꾸지 않는다. */
export function formatNumber(value: string | null): string {
  if (value === null || value === "") return "—";
  const negative = value.startsWith("-");
  const raw = negative ? value.slice(1) : value;
  const [whole, fraction] = raw.split(".");
  if (!/^\d+$/.test(whole ?? "")) return value;
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${negative ? "-" : ""}${grouped}${fraction ? `.${fraction}` : ""}`;
}

export function formatMoney(value: string | null): string {
  const text = formatNumber(value);
  return text === "—" ? text : `${text}원`;
}

export function formatPercent(value: string | null): string {
  const text = formatNumber(value);
  return text === "—" ? text : `${text}%`;
}
