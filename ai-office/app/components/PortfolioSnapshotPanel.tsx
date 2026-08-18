"use client";

import { useEffect, useState } from "react";
import {
  fetchPortfolioSnapshot,
  type AssetAllocation,
  type BrokerOrderSnapshot,
  type OrderIntentSnapshot,
  type PortfolioReadModel,
  type PortfolioPosition,
} from "../lib/portfolioSnapshotClient";
import { PORTFOLIO_SCOPE_CHANGED_EVENT } from "../lib/currentFund";
import { ACCOUNT_CHANGED_EVENT } from "../lib/currentAccount";

const STATUS_LABELS: Record<string, string> = {
  CREATED: "주문 생성",
  SUBMITTED: "제출됨",
  ACKNOWLEDGED: "접수 확인",
  PARTIALLY_FILLED: "부분 체결",
  FILLED: "체결 완료",
  CANCEL_PENDING: "취소 대기",
  CANCELLED: "취소됨",
  REJECTED: "거부됨",
  EXPIRED: "만료됨",
  UNKNOWN: "상태 확인 필요",
  DRAFT: "초안",
  RISK_PENDING: "Risk 심사 중",
  APPROVED: "Risk 승인",
  RESIZED: "수량 조정",
  USER_PENDING: "사용자 승인 대기",
  USER_APPROVED: "사용자 승인",
  READY_TO_SUBMIT: "제출 준비",
};

function formatDecimal(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const negative = value.startsWith("-");
  const raw = negative ? value.slice(1) : value;
  const [integerPart, fractionPart] = raw.split(".");
  const integer = (integerPart || "0").replace(/^0+(?=\d)/, "") || "0";
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const fraction = fractionPart?.replace(/0+$/, "");
  return `${negative ? "-" : ""}${grouped}${fraction ? `.${fraction}` : ""}`;
}

function formatMoney(value: string | null | undefined): string {
  return value === null || value === undefined ? "—" : `${formatDecimal(value)}원`;
}

function formatPercent(value: string | null | undefined): string {
  if (value === null || value === undefined || value === "") return "—";
  const negative = value.startsWith("-");
  const raw = negative ? value.slice(1) : value;
  const [integerPart, fractionPart = ""] = raw.split(".");
  const digits = `${integerPart || "0"}${fractionPart}`;
  const point = (integerPart || "0").length + 2;
  const shifted = point >= digits.length
    ? `${digits}${"0".repeat(point - digits.length)}`
    : `${digits.slice(0, point)}.${digits.slice(point)}`;
  return `${negative ? "-" : ""}${formatDecimal(shifted)}%`;
}

function shorten(value: string | null | undefined): string {
  if (!value) return "—";
  return value.length > 12 ? `${value.slice(0, 8)}…` : value;
}

function statusLabel(value: string): string {
  const key = value.toUpperCase();
  return STATUS_LABELS[key] ?? key.replaceAll("_", " ");
}

function statusTone(value: string): string {
  const key = value.toUpperCase();
  if (["FILLED", "APPROVED", "USER_APPROVED", "READY_TO_SUBMIT", "ACKNOWLEDGED"].includes(key)) {
    return "border-tertiary-fixed-dim bg-tertiary-fixed/30 text-on-tertiary-fixed-variant";
  }
  if (["REJECTED", "UNKNOWN", "EXPIRED", "CANCELLED"].includes(key)) {
    return "border-error/40 bg-error-container text-on-error-container";
  }
  return "border-outline-variant bg-surface-container text-on-surface-variant";
}

function StatusPill({ value }: { value: string }) {
  return (
    <span className={`inline-flex max-w-full items-center justify-center rounded-full border px-2.5 py-1 text-xs font-semibold whitespace-nowrap ${statusTone(value)}`}>
      {statusLabel(value)}
    </span>
  );
}

function EmptyRows({ colSpan, message }: { colSpan: number; message: string }) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-3 py-7 text-center text-sm text-on-surface-variant">
        {message}
      </td>
    </tr>
  );
}

function PositionsTable({ positions }: { positions: PortfolioPosition[] }) {
  return (
    <section className="min-w-0 rounded-lg border border-outline-variant bg-surface-container-lowest" aria-labelledby="portfolio-positions-title">
      <div className="flex items-center justify-between gap-3 border-b border-outline-variant px-4 py-3">
        <h3 id="portfolio-positions-title" className="m-0 text-title-md font-title-md text-primary">포지션</h3>
        <span className="text-xs text-on-surface-variant">{positions.length}건</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[500px] table-fixed text-left text-xs">
          <thead className="border-b border-outline-variant bg-surface-container-low text-label-md text-on-surface-variant">
            <tr>
              <th className="w-[28%] px-3 py-2 font-semibold">종목</th>
              <th className="w-[22%] px-3 py-2 text-right font-semibold">평균단가</th>
              <th className="w-[17%] px-3 py-2 text-right font-semibold">수량</th>
              <th className="w-[21%] px-3 py-2 text-right font-semibold">평가액</th>
              <th className="w-[12%] px-3 py-2 text-right font-semibold">비중</th>
            </tr>
          </thead>
          <tbody>
            {positions.length === 0 ? <EmptyRows colSpan={5} message="현재 표시할 포지션이 없습니다." /> : positions.map((position) => (
              <tr key={`${position.instrument_id}-${position.mark_as_of}`} className="border-b border-outline-variant last:border-b-0">
                <td className="truncate px-3 py-3 font-data-mono text-on-surface" title={position.instrument_id}>{shorten(position.instrument_id)}</td>
                <td className="px-3 py-3 text-right font-data-mono text-on-surface-variant">{formatMoney(position.average_cost)}</td>
                <td className="px-3 py-3 text-right font-data-mono text-on-surface-variant">{formatDecimal(position.quantity)}</td>
                <td className="px-3 py-3 text-right font-data-mono text-on-surface">{formatMoney(position.market_value)}</td>
                <td className="px-3 py-3 text-right font-data-mono text-on-surface-variant">{formatPercent(position.weight)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function OrdersTable({ orders }: { orders: BrokerOrderSnapshot[] }) {
  return (
    <section className="min-w-0 rounded-lg border border-outline-variant bg-surface-container-lowest" aria-labelledby="broker-orders-title">
      <div className="flex items-center justify-between gap-3 border-b border-outline-variant px-4 py-3">
        <h3 id="broker-orders-title" className="m-0 text-title-md font-title-md text-primary">브로커 주문</h3>
        <span className="text-xs text-on-surface-variant">{orders.length}건</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[500px] table-fixed text-left text-xs">
          <thead className="border-b border-outline-variant bg-surface-container-low text-label-md text-on-surface-variant">
            <tr>
              <th className="w-[32%] px-3 py-2 font-semibold">주문번호</th>
              <th className="w-[24%] px-3 py-2 text-right font-semibold">체결/주문</th>
              <th className="w-[22%] px-3 py-2 text-right font-semibold">평균체결가</th>
              <th className="w-[22%] px-3 py-2 text-right font-semibold">상태</th>
            </tr>
          </thead>
          <tbody>
            {orders.length === 0 ? <EmptyRows colSpan={4} message="현재 표시할 브로커 주문이 없습니다." /> : orders.map((order) => (
              <tr key={order.order_id} className="border-b border-outline-variant last:border-b-0">
                <td className="truncate px-3 py-3 font-data-mono text-on-surface" title={order.client_order_id}>
                  <span className="font-semibold">{order.side} </span>{shorten(order.client_order_id)}
                </td>
                <td className="px-3 py-3 text-right font-data-mono text-on-surface-variant">
                  {formatDecimal(order.filled_quantity)} / {formatDecimal(order.requested_quantity)}
                </td>
                <td className="px-3 py-3 text-right font-data-mono text-on-surface">{formatMoney(order.average_fill_price)}</td>
                <td className="px-3 py-3 text-right"><StatusPill value={order.state} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function IntentsTable({ intents }: { intents: OrderIntentSnapshot[] }) {
  return (
    <section className="rounded-lg border border-outline-variant bg-surface-container-lowest" aria-labelledby="order-intents-title">
      <div className="flex items-center justify-between gap-3 border-b border-outline-variant px-4 py-3">
        <h3 id="order-intents-title" className="m-0 text-title-md font-title-md text-primary">Order Intent</h3>
        <span className="text-xs text-on-surface-variant">{intents.length}건</span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[620px] table-fixed text-left text-xs">
          <thead className="border-b border-outline-variant bg-surface-container-low text-label-md text-on-surface-variant">
            <tr>
              <th className="w-[36%] px-3 py-2 font-semibold">Intent</th>
              <th className="w-[18%] px-3 py-2 text-right font-semibold">수량</th>
              <th className="w-[24%] px-3 py-2 font-semibold">Risk 판정</th>
              <th className="w-[22%] px-3 py-2 text-right font-semibold">상태</th>
            </tr>
          </thead>
          <tbody>
            {intents.length === 0 ? <EmptyRows colSpan={4} message="현재 표시할 Order Intent가 없습니다." /> : intents.map((intent) => (
              <tr key={intent.order_intent_id} className="border-b border-outline-variant last:border-b-0">
                <td className="truncate px-3 py-3 font-data-mono text-on-surface" title={intent.order_intent_id}>{shorten(intent.order_intent_id)}</td>
                <td className="px-3 py-3 text-right font-data-mono text-on-surface-variant">{formatDecimal(intent.requested_quantity)}</td>
                <td className="truncate px-3 py-3 font-data-mono text-on-surface-variant" title={intent.risk_decision_id ?? undefined}>
                  {intent.risk_decision_id ? shorten(intent.risk_decision_id) : "미심사"}
                </td>
                <td className="px-3 py-3 text-right"><StatusPill value={intent.state} /></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

const ALLOCATION_COLORS = [
  "var(--color-primary, #172554)",
  "var(--color-tertiary-fixed-dim, #a7c7b5)",
  "var(--color-secondary, #7568a8)",
  "var(--color-primary-fixed-dim, #9db4e8)",
];

function allocationChartStyle(allocation: AssetAllocation[]): React.CSSProperties {
  let cursor = 0;
  const segments = allocation.flatMap((item, index) => {
    const weight = Number(item.weight);
    if (!Number.isFinite(weight) || weight <= 0 || cursor >= 1) return [];
    const start = cursor;
    cursor = Math.min(1, cursor + weight);
    return [`${ALLOCATION_COLORS[index % ALLOCATION_COLORS.length]} ${start * 100}% ${cursor * 100}%`];
  });

  return {
    background: segments.length
      ? `conic-gradient(${segments.join(", ")})`
      : "var(--color-surface-container-high)",
  };
}

function StrategyAllocationChart({ allocation }: { allocation: AssetAllocation[] }) {
  const chartStyle = allocationChartStyle(allocation);

  return (
    <section className="rounded-lg border border-outline-variant bg-surface-container-lowest p-4" aria-labelledby="strategy-allocation-title">
      <div className="flex items-center justify-between gap-3">
        <div>
          <h3 id="strategy-allocation-title" className="m-0 text-title-md font-title-md text-primary">알파 전략 구성</h3>
          <p className="m-0 mt-1 text-xs text-on-surface-variant">알파 전략별 비중 · 트레이딩 연결 전</p>
        </div>
        <span className="rounded-full border border-outline-variant bg-surface-container px-2 py-0.5 text-xs font-semibold text-on-surface-variant">
          {allocation.length > 0 ? "전략 Read Model" : "연결 전"}
        </span>
      </div>
      <div className="mt-4 flex flex-col items-center gap-5 sm:flex-row sm:items-center">
        <div
          className="relative h-40 w-40 shrink-0 rounded-full"
          role="img"
          aria-label="알파 전략별 자산 구성 그래프"
          style={chartStyle}
        >
          <div className="absolute inset-[18px] flex flex-col items-center justify-center rounded-full bg-surface-container-lowest text-center">
            <span className="text-[10px] font-semibold uppercase tracking-wide text-on-surface-variant">STRATEGIES</span>
            <strong className="mt-1 font-data-mono text-xl text-primary">{allocation.length}</strong>
            <span className="text-[10px] text-on-surface-variant">개 구성</span>
          </div>
        </div>
        <div className="grid w-full min-w-0 grid-cols-1 gap-2 sm:grid-cols-2">
          {allocation.map((item, index) => (
            <div key={item.key} className="flex min-w-0 items-center gap-2 rounded-full border border-outline-variant bg-surface-container-low px-3 py-2 text-sm">
              <span className="h-2.5 w-2.5 shrink-0 rounded-full" style={{ backgroundColor: ALLOCATION_COLORS[index % ALLOCATION_COLORS.length] }} aria-hidden="true" />
              {item.code ? <span className="shrink-0 font-data-mono font-bold text-primary">{item.code}</span> : null}
              <span className="min-w-0 truncate text-on-surface">{item.label}</span>
              <span className="ml-auto shrink-0 font-data-mono text-on-surface-variant">· {formatPercent(item.weight)}</span>
            </div>
          ))}
          {allocation.length === 0 ? <p className="m-0 text-sm text-on-surface-variant">전략 데이터 연결 전입니다.</p> : null}
        </div>
      </div>
    </section>
  );
}

export default function PortfolioSnapshotPanel() {
  const [data, setData] = useState<PortfolioReadModel | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);

  useEffect(() => {
    let alive = true;
    const refresh = () => {
      setLoading(true);
      fetchPortfolioSnapshot()
        .then((next) => {
          if (!alive) return;
          setData(next);
          setError("");
        })
        .catch((cause) => alive && setError(cause instanceof Error ? cause.message : String(cause)))
        .finally(() => alive && setLoading(false));
    };

    refresh();
    window.addEventListener(PORTFOLIO_SCOPE_CHANGED_EVENT, refresh);
    window.addEventListener(ACCOUNT_CHANGED_EVENT, refresh);
    return () => {
      alive = false;
      window.removeEventListener(PORTFOLIO_SCOPE_CHANGED_EVENT, refresh);
      window.removeEventListener(ACCOUNT_CHANGED_EVENT, refresh);
    };
  }, [reloadKey]);

  return (
    <section className="lg:col-span-2 min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm" aria-labelledby="portfolio-snapshot-title">
      <div className="flex items-center justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-2.5">
        <span className="flex min-w-0 items-center gap-2 text-label-md font-label-md text-on-surface-variant">
          <span className="material-symbols-outlined text-[16px]" aria-hidden="true">account_balance</span>
          <span className="truncate">trading_portfolio.snapshot</span>
        </span>
        <div className="flex shrink-0 items-center gap-1.5">
          <span className="rounded-full border border-outline-variant bg-surface-container-lowest px-2 py-0.5 text-[10px] font-semibold">BFF Read Model</span>
          <span className="rounded-full border border-outline-variant bg-surface-container-lowest px-2 py-0.5 text-[10px] font-semibold">v1</span>
          <button
            type="button"
            onClick={() => setReloadKey((current) => current + 1)}
            disabled={loading}
            className="rounded border border-outline-variant bg-surface-container-lowest px-2 py-0.5 text-xs font-semibold text-primary transition-colors hover:bg-surface-container disabled:cursor-wait disabled:opacity-50"
          >
            {loading ? "새로고침 중…" : "새로고침"}
          </button>
        </div>
      </div>

      <div className="space-y-5 p-4 md:p-6">
        <div className="flex items-start justify-between gap-4 flex-wrap">
          <div className="min-w-0">
            <p className="m-0 text-label-md font-label-md uppercase text-on-surface-variant">Trading · Accounting / Portfolio</p>
            <h2 id="portfolio-snapshot-title" className="mt-2 text-headline-md font-headline-md font-bold text-primary">Mandate 기준 포트폴리오</h2>
            <p className="mt-2 max-w-3xl text-body-sm font-body-sm text-on-surface-variant">
              사용자 Mandate의 기준 자본을 표시합니다. 트레이딩 연결 전 수치는 0으로 유지됩니다.
            </p>
          </div>
          <div className="flex shrink-0 items-center gap-2">
            <span className={`rounded-full border px-3 py-1 text-label-md font-label-md ${data ? "border-tertiary-fixed-dim bg-tertiary-fixed/30 text-on-tertiary-fixed-variant" : "border-outline text-on-surface-variant"}`}>
              {data?.portfolio.nav ? "MANDATE READ MODEL" : loading ? "MANDATE CHECKING" : "MANDATE UNAVAILABLE"}
            </span>
          </div>
        </div>

        {error && data ? <p role="status" className="m-0 rounded border border-error/40 bg-error-container px-3 py-2 text-xs text-on-error-container">새 Snapshot을 받지 못해 이전 표시를 유지합니다. {error}</p> : null}
        {error && !data ? (
          <div className="rounded-lg border border-error/40 bg-error-container p-5 text-sm text-on-error-container" role="alert">
            <p className="m-0 font-semibold">Read Model을 불러오지 못했습니다.</p>
            <p className="m-0 mt-1">{error}</p>
          </div>
        ) : null}
        {loading && !data && !error ? <p className="m-0 rounded-lg border border-outline-variant bg-surface-container-low p-5 text-sm text-on-surface-variant">Snapshot을 불러오는 중입니다…</p> : null}

        {data ? (
          <>
            <StrategyAllocationChart
              allocation={data.portfolio.allocation}
            />

            <div className="grid grid-cols-2 gap-2 md:grid-cols-5" aria-label="포트폴리오 요약">
              {[
                ["NAV", formatMoney(data.portfolio.nav)],
                ["현금", formatMoney(data.portfolio.cash)],
                ["평가액", formatMoney(data.portfolio.securities_value)],
                ["실현손익", formatMoney(data.portfolio.realized_pnl)],
                ["미실현손익", formatMoney(data.portfolio.unrealized_pnl)],
              ].map(([label, value]) => (
                <div key={label} className="min-w-0 rounded-md border border-outline-variant bg-surface-container-lowest px-3 py-2.5">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-label-md font-label-md text-on-surface-variant">{label}</span>
                  </div>
                  <p className="m-0 mt-1 truncate text-body-sm font-data-mono font-semibold text-primary" title={value}>{value}</p>
                </div>
              ))}
            </div>

            <div className="grid min-w-0 grid-cols-1 gap-4 xl:grid-cols-2">
              <PositionsTable positions={data.portfolio.positions} />
              <OrdersTable orders={data.trading.orders} />
            </div>
            <IntentsTable intents={data.trading.intents} />

            <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-outline-variant pt-3 text-xs text-on-surface-variant">
              <span>원장 분개 {data.ledger.journal_count}건 · 반대분개 {data.ledger.reversal_count}건 · 차대 {data.ledger.balanced ? "일치" : "불일치"}</span>
              <span>트레이딩 연결 전 상태</span>
            </div>
            {data.trading.blocked_by_unknown ? <p role="alert" className="m-0 rounded border border-error/40 bg-error-container px-3 py-2 text-xs text-on-error-container">브로커 상태를 확인할 수 없는 주문이 있어 신규 진입을 진행하지 않습니다.</p> : null}
          </>
        ) : null}
      </div>
    </section>
  );
}
