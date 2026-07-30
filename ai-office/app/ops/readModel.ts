// Trading·Portfolio Read Model — 백엔드가 확정한 상태를 화면으로 옮기는 계약.
//
// 소유: 도현 (트레이딩 + 회계·포트폴리오)
// 근거: docs/02-engineering/AI_OFFICE_FRONTEND_PLAN.md 3.2(6·7), 5.1, 9(UI-0), 11
// 생성기: departments/05-accounting-portfolio/portfolio/ui_read_model.py
//
// 규칙 두 가지가 여기서도 유지된다.
//
//  1. 금액·수량은 string이다. JSON number는 double이라 Decimal이 깨진다.
//     화면은 표시만 하므로 파싱하지 않는다. 계산이 필요하면 서버가 한다.
//  2. 이 파일은 수치를 만들지 않는다. NAV도 비중도 백엔드가 확정한 값이다
//     (계획 1절 — 화면은 공식 장부를 계산하지 않는다).

/** 원 단위 Decimal 문자열. 절대 Number로 바꾸지 않는다. */
export type Money = string;

export type Position = {
  instrument_id: string;
  quantity: Money;
  average_cost: Money;
  mark_price: Money;
  mark_as_of: string;
  market_value: Money;
  unrealized_pnl: Money;
  /** NAV가 0 이하면 비중이 정의되지 않는다 — 그때만 null이다. */
  weight: Money | null;
};

export type OrderIntentRow = {
  order_intent_id: string;
  state: string;
  requested_quantity: Money;
  risk_decision_id: string | null;
  risk_approved_qty: Money | null;
  valid_until: string;
};

export type BrokerOrderRow = {
  order_id: string;
  order_intent_id: string;
  client_order_id: string;
  broker_order_id: string | null;
  broker_adapter: string;
  state: string;
  side: string;
  instrument_id: string;
  requested_quantity: Money;
  filled_quantity: Money;
  leaves_quantity: Money;
  limit_price: Money | null;
  average_fill_price: Money | null;
  fill_count: number;
  is_terminal: boolean;
};

export type TradingSnapshot = {
  schema_version: number;
  /** DEMO/PAPER/LIVE 데이터를 같은 화면에서 섞지 않는다 (계획 4절). */
  mode: "DEMO" | "PAPER" | "LIVE";
  snapshot_version: number;
  server_time: string;
  fund_id: string;
  book_id: string;
  portfolio: {
    as_of: string;
    nav: Money;
    cash: Money;
    securities_value: Money;
    gross_exposure: Money;
    net_exposure: Money;
    realized_pnl: Money;
    unrealized_pnl: Money;
    fees: Money;
    taxes: Money;
    positions: Position[];
  };
  trading: {
    intents: OrderIntentRow[];
    orders: BrokerOrderRow[];
    /** UNKNOWN 주문이 있으면 신규 주문이 막힌 상태다. */
    blocked_by_unknown: boolean;
  };
  ledger: {
    journal_count: number;
    reversal_count: number;
    trial_balance_sum: Money;
    balanced: boolean;
    accounts: Record<string, Money>;
  };
};

/** 이 파일이 아는 Major Version. 다르면 적용하지 않는다 (계획 5.3). */
export const SUPPORTED_SCHEMA_VERSION = 1;

/**
 * Snapshot 형태 검증.
 *
 * 계획 5.3은 Zod를 쓰라고 하지만 지금 대상은 정적 DEMO Fixture 하나이고
 * ai-office에 Zod가 없다. 라이브러리를 하나 더 들이는 대신 필요한 것만 본다.
 * WebSocket Event를 받기 시작하면(Phase UI-1) 그때 Zod로 옮긴다.
 */
export function parseSnapshot(input: unknown): TradingSnapshot {
  if (typeof input !== "object" || input === null) {
    throw new Error("Snapshot이 객체가 아닙니다");
  }
  const doc = input as Record<string, unknown>;

  if (doc.schema_version !== SUPPORTED_SCHEMA_VERSION) {
    // 모르는 Version은 추측해서 그리지 않는다. 틀린 숫자를 보여주는 것보다 낫다.
    throw new Error(
      `지원하지 않는 schema_version ${String(doc.schema_version)} (지원: ${SUPPORTED_SCHEMA_VERSION})`,
    );
  }
  if (doc.mode !== "DEMO" && doc.mode !== "PAPER" && doc.mode !== "LIVE") {
    throw new Error(`알 수 없는 mode: ${String(doc.mode)}`);
  }
  for (const key of ["portfolio", "trading", "ledger"] as const) {
    if (typeof doc[key] !== "object" || doc[key] === null) {
      throw new Error(`Snapshot에 ${key}가 없습니다`);
    }
  }
  return doc as unknown as TradingSnapshot;
}

/** 원 단위 표시. 파싱해서 계산하지 않고 자릿수만 끊는다. */
export function won(value: Money | null): string {
  if (value === null) return "—";
  const negative = value.startsWith("-");
  const [intPart = "0"] = (negative ? value.slice(1) : value).split(".");
  const grouped = intPart.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${negative ? "-" : ""}${grouped}원`;
}

/** 비중 표시. weight는 0~1 사이의 Decimal 문자열이다. */
export function percent(value: Money | null): string {
  if (value === null) return "—";
  const asNumber = Number(value);
  if (!Number.isFinite(asNumber)) return "—";
  return `${(asNumber * 100).toFixed(2)}%`;
}
