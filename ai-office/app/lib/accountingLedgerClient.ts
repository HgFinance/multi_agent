/**
 * 회계 거래 원장 client — BFF `/ui/portfolio/ledger` 폴링.
 *
 * 트레이딩의 `portfolioLiveClient`와 원천이 다르다. 저쪽은 주문이 어떻게
 * 흘렀는지(접수·체결·정정·취소·거부)와 현재 보유를 보고, 이쪽은 **확정된 거래와
 * 그 비용**을 본다 — 수수료·세금·매매손익·배당·예수금 잔액이다. 둘을 한 화면에
 * 섞지 않는 이유이고, 회계본부가 자기 축으로 보게 하려는 것이다.
 *
 * 브로커가 무엇인지는 화면이 알지 않는다. 금액은 문자열 그대로 둔다 —
 * JavaScript number는 double이라 Decimal이 깨진다.
 */

import { BFF } from "./ceoClient";
import { withAccountHeaders } from "./currentAccount";

export type LedgerEntry = {
  /** `YYYY-MM-DD`. */
  trade_date: string | null;
  trade_no: string | null;
  /** 원본은 `HHMMSSmmm`. 서버가 해석하지 않고 그대로 넘긴다. */
  trade_time: string | null;
  /** 매수/매도/입금 같은 거래 구분. */
  category: string | null;
  /** 적요 — 원장에서 이 줄이 무엇인지 말해 주는 칸이다. */
  summary: string | null;
  cancelled: string | null;
  symbol: string | null;
  symbol_name: string | null;
  quantity: string | null;
  unit_price: string | null;
  /** 거래금액(총액). */
  amount: string | null;
  /** 정산금액 — 비용까지 반영해 실제로 계좌에서 오간 금액. */
  settled_amount: string | null;
  commission: string | null;
  /** 거래세·소득세·주민세 합계. */
  tax: string | null;
  realized_pnl: string | null;
  dividend: string | null;
  /**
   * 결제까지 끝난 줄인가. 체결 당일 거래는 결제(T+2) 전이라 확정 원장에 아직
   * 없고 당일 매매일지에서 온다 — 회계는 이 둘을 같은 줄로 취급하면 안 된다.
   */
  settlement: "SETTLED" | "UNSETTLED" | string;
  cash_before: string | null;
  /** 이 거래 직후의 예수금 잔액. 원장의 잔액 칸이다. */
  cash_after: string | null;
  currency: string | null;
};

export type LedgerTotals = {
  count: number;
  /** 이 중 아직 결제되지 않은 줄 수. */
  unsettled_count: number;
  commission: string;
  tax: string;
  /** 수수료 + 세금. 서버가 Decimal로 더한다 - 화면에서 문자열을 더하지 않는다. */
  cost: string;
  realized_pnl: string;
  dividend: string;
  settled: string;
};

/** 계좌 자체의 현재 모습. 기간 원장과 축이 다르다. */
export type AccountSummary = {
  as_of: string | null;
  error: string | null;
  net_asset: string | null;
  valuation: string | null;
  purchase_amount: string | null;
  valuation_pnl: string | null;
  realized_pnl: string | null;
  holding_count: number;
};

export type Pnl = {
  realized: string | null;
  valuation: string | null;
  /** 실현 + 평가. 비용을 다시 빼지 않는다 — 아래 플래그 참고. */
  total: string | null;
  commission: string | null;
  tax: string | null;
  cost: string | null;
  /**
   * 브로커 실현손익이 제비용 포함 기준인가. true면 총손익에서 비용을 또 빼면
   * 이중 차감이라, 화면은 비용을 **참고 수치**로만 보여 준다.
   */
  cost_included_in_realized: boolean;
};

export type AccountingLedger = {
  schema_version: string;
  environment: "PAPER" | "LIVE" | string;
  environment_label: string;
  account: { registered: boolean; masked: string | null };
  period: { start: string; end: string; days: number };
  account_summary?: AccountSummary;
  pnl?: Pnl;
  rows: LedgerEntry[];
  totals: LedgerTotals;
  /** 기간 마지막 거래 직후의 예수금 잔액. 거래가 없으면 null. */
  cash_balance: string | null;
  /**
   * 거래가 없을 때 브로커가 준 사유. "거래 0건"과 "조회가 안 됨"은 다른 말이라
   * 0원으로 뭉개지 않고 사유를 그대로 보여 준다.
   */
  notice: string | null;
  /** 당일 매매일지 조회가 실패했을 때의 사유. 확정분은 그대로 나온다. */
  today_error?: string | null;
  /**
   * 브로커 조회가 실패해 저장된 기록만으로 응답했을 때의 사유.
   * 화면은 이걸 숨기지 않는다 — 최신이 아닐 수 있다는 사실이 값보다 중요하다.
   */
  source_error?: string | null;
  /** 서버가 이 줄들을 durable 저장소에 남겼는가. */
  persisted?: boolean;
  server_time: string;
  authoritative: boolean;
  official_nav_source: string;
};

export class AccountingLedgerError extends Error {
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
  return `거래 원장 조회 실패 (HTTP ${status})`;
}

function hasLedgerShape(value: unknown): value is AccountingLedger {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.environment === "string" &&
    typeof candidate.period === "object" &&
    candidate.period !== null &&
    typeof candidate.totals === "object" &&
    candidate.totals !== null &&
    Array.isArray(candidate.rows)
  );
}

export async function fetchAccountingLedger(days?: number): Promise<AccountingLedger> {
  const query = days ? `?days=${days}` : "";
  let response: Response;
  try {
    response = await fetch(`${BFF}/ui/portfolio/ledger${query}`, {
      cache: "no-store",
      headers: withAccountHeaders({ Accept: "application/json" }),
    });
  } catch {
    throw new AccountingLedgerError(
      `BFF(${BFF})에 연결하지 못했습니다. FastAPI BFF가 실행 중인지 확인하세요.`,
      0,
    );
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new AccountingLedgerError(explain(body, response.status), response.status);
  if (!hasLedgerShape(body)) {
    throw new AccountingLedgerError("거래 원장 응답 계약이 올바르지 않습니다.", response.status);
  }
  return body;
}

/** 천 단위 구분만 넣는다. Number로 바꾸지 않는다. */
export function formatAmount(value: string | null): string {
  if (value === null || value === "") return "—";
  const negative = value.startsWith("-");
  const raw = negative ? value.slice(1) : value;
  const [whole, fraction] = raw.split(".");
  if (!/^\d+$/.test(whole ?? "")) return value;
  const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return `${negative ? "-" : ""}${grouped}${fraction ? `.${fraction}` : ""}`;
}

/** 0은 원장에서 빈칸으로 둔다 — 숫자가 늘어서면 0이 실제 값을 가린다. */
export function formatLedgerCell(value: string | null): string {
  if (value === null || value === "" || value === "0") return "";
  return formatAmount(value);
}

export function formatMoney(value: string | null): string {
  const text = formatAmount(value);
  return text === "—" ? text : `${text}원`;
}

/** `YYYY-MM-DD` → `08.18`. 원장은 같은 해가 이어지므로 월·일이면 충분하다. */
export function formatLedgerDate(value: string | null): string {
  if (!value) return "—";
  const parts = value.split("-");
  return parts.length === 3 ? `${parts[1]}.${parts[2]}` : value;
}

/**
 * 체결시각 → `HH:MM:SS`.
 *
 * 원천마다 모양이 다르다 — 확정 거래내역은 `HHMMSSmmm`, 당일 체결내역은 이미
 * `HH:MM:SS`다. 숫자만 뽑아 같은 규칙으로 맞춘다. 초까지 보여 주는 이유는
 * 대사할 때 같은 분에 여러 건이 몰리면 분 단위로는 구분이 안 되기 때문이다.
 */
export function formatLedgerTime(value: string | null): string {
  if (!value) return "";
  const digits = value.replace(/\D/g, "");
  if (digits.length < 4) return "";
  const hm = `${digits.slice(0, 2)}:${digits.slice(2, 4)}`;
  return digits.length >= 6 ? `${hm}:${digits.slice(4, 6)}` : hm;
}

/** 부호를 색으로 나눈다. 0과 빈 값은 중립이다. */
export function signTone(value: string | null): string {
  if (!value || value === "0") return "text-on-surface-variant";
  return value.startsWith("-") ? "text-[color:var(--color-error)]" : "text-[color:var(--color-tertiary-container)]";
}
