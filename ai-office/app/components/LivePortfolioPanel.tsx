"use client";

import { useQuery } from "@tanstack/react-query";
import {
  fetchPortfolioLive,
  formatEventTime,
  formatMoney,
  formatNumber,
  formatPercent,
  ORDER_KINDS,
  PortfolioLiveError,
  type Holding,
  type OrderEvent,
  type PortfolioLive,
} from "../lib/portfolioLiveClient";

/**
 * 실시간 포트폴리오 패널.
 *
 * 주문이 접수·체결·정정·취소·거부될 때마다 상태가 바뀌고, 체결이 나면 잔고를
 * 브로커와 다시 맞춘 결과가 함께 내려온다. 어느 증권사인지·어떤 TR인지는
 * BFF 안쪽 사정이라 이 파일에 없다.
 *
 * 여기 나오는 값은 **브로커 장부**이고 우리 원장이 아니다. "비공식" 배지를
 * 박아 두는 이유이고, 공식 수치는 회계본부 원장이 확정한다.
 */

const POLL_MS = 3000;

/** 주문 상태별 강조색. 체결·거부는 눈에 띄어야 하고 나머지는 중립이다. */
const KIND_TONE: Record<string, string> = {
  ACCEPTED: "border-green-300 bg-green-50 text-green-700",
  FILLED: "border-tertiary-fixed-dim bg-tertiary-fixed/30 text-on-tertiary-fixed-variant",
  AMENDED: "border-outline-variant bg-surface-container-high text-on-surface",
  CANCELLED: "border-outline-variant bg-surface-container-high text-on-surface-variant",
  REJECTED: "border-error/40 bg-error-container text-on-error-container",
};

const FALLBACK_KIND_LABELS: Record<string, string> = {
  ACCEPTED: "접수",
  FILLED: "체결",
  AMENDED: "정정",
  CANCELLED: "취소",
  REJECTED: "거부",
};

function KindTile({ kind, label, count }: { kind: string; label: string; count: number }) {
  return (
    <div className={`min-w-0 rounded-md border px-3 py-2.5 ${KIND_TONE[kind] ?? KIND_TONE.ACCEPTED}`}>
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate text-label-md font-label-md" title={label}>
          {label}
        </span>
        <b className="font-data-mono text-headline-md font-headline-lg leading-none">{count}</b>
      </div>
    </div>
  );
}

function SummaryTile({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-0 rounded-md border border-outline-variant bg-surface-container-lowest px-3 py-2.5">
      <span className="text-label-md font-label-md text-on-surface-variant">{label}</span>
      <p className="m-0 mt-1 truncate text-body-sm font-data-mono font-semibold text-primary" title={value}>
        {value}
      </p>
    </div>
  );
}

const ALLOCATION_COLORS = ["#3f5f9a", "#4b8f8c", "#c47b35", "#8b6aa9", "#c15c63", "#6c8195", "#7a8f4b"];

type PortfolioAllocation = {
  key: string;
  label: string;
  percent: number;
  color: string;
};

function parseChartNumber(value: string | null): number | null {
  if (!value) return null;
  const parsed = Number(value.replace(/,/g, "").replace(/%$/, "").trim());
  return Number.isFinite(parsed) ? parsed : null;
}

function getPortfolioAllocations(rows: Holding[]): PortfolioAllocation[] {
  const weights = rows.map((holding) => parseChartNumber(holding.weight));
  const hasWeights =
    rows.length > 0 &&
    weights.every((weight) => weight !== null && weight >= 0) &&
    // `every`의 null 배제는 `some`까지 좁혀지지 않는다. 여기서 다시 걸러야 한다.
    weights.some((weight) => (weight ?? 0) > 0);
  const values = rows.map((holding, index) => {
    if (hasWeights) return weights[index] ?? 0;
    return Math.max(parseChartNumber(holding.market_value) ?? 0, 0);
  });
  const total = values.reduce((sum, value) => sum + value, 0);

  if (total <= 0) return [];

  return rows
    .map((holding, index) => ({
      key: holding.symbol ?? holding.name ?? `holding-${index}`,
      label: holding.name ?? holding.symbol ?? "이름 없는 종목",
      percent: (values[index] / total) * 100,
      color: ALLOCATION_COLORS[index % ALLOCATION_COLORS.length],
    }))
    .filter((allocation) => allocation.percent > 0);
}

function PortfolioAllocationChart({ rows }: { rows: Holding[] }) {
  const allocations = getPortfolioAllocations(rows);

  return (
    <div
      className="w-full rounded-md border border-outline-variant bg-surface-container-low px-4 py-3"
      aria-label="보유 종목별 포트폴리오 비중"
    >
      <div className="flex items-center justify-between gap-3">
        <span className="text-label-md font-label-md uppercase text-on-surface-variant">포트폴리오 비중</span>
        <span className="text-[11px] text-on-surface-variant">{allocations.length}종목</span>
      </div>

      {allocations.length > 0 ? (
        <div className="mt-3 grid grid-cols-[13rem_minmax(0,1fr)] items-center gap-4">
          <div className="relative h-[13rem] w-[13rem] shrink-0" role="img" aria-label="보유 종목 비중 도넛 그래프">
            <svg viewBox="0 0 120 120" className="h-full w-full -rotate-90" aria-hidden="true">
              <circle cx="60" cy="60" r="42" fill="none" stroke="#e0e3e5" strokeWidth="16" />
              {allocations.reduce<{ offset: number; elements: React.ReactNode[] }>(
                (result, allocation) => {
                  result.elements.push(
                    <circle
                      key={allocation.key}
                      cx="60"
                      cy="60"
                      r="42"
                      fill="none"
                      pathLength="100"
                      stroke={allocation.color}
                      strokeDasharray={`${allocation.percent} ${100 - allocation.percent}`}
                      strokeDashoffset={-result.offset}
                      strokeWidth="16"
                    />,
                  );
                  result.offset += allocation.percent;
                  return result;
                },
                { offset: 0, elements: [] },
              ).elements}
            </svg>
            <div className="pointer-events-none absolute inset-0 flex flex-col items-center justify-center text-center">
              <span className="text-[10px] text-on-surface-variant">보유 종목</span>
              <strong className="font-data-mono text-sm text-primary">{allocations.length}개</strong>
            </div>
          </div>

          <ul className="m-0 min-w-0 space-y-2 p-0" aria-label="보유 종목별 비중">
            {allocations.map((allocation) => (
              <li key={allocation.key} className="flex min-w-0 items-center justify-between gap-2 text-xs">
                <span className="flex min-w-0 items-center gap-2">
                  <span
                    className="h-2.5 w-2.5 shrink-0 rounded-full"
                    style={{ backgroundColor: allocation.color }}
                    aria-hidden="true"
                  />
                  <span className="truncate text-on-surface" title={allocation.label}>
                    {allocation.label}
                  </span>
                </span>
                <span className="shrink-0 font-data-mono text-on-surface-variant">
                  {allocation.percent.toFixed(1)}%
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="m-0 mt-3 text-xs text-on-surface-variant">보유 종목 비중을 확인하는 중입니다.</p>
      )}
    </div>
  );
}

/**
 * 종목별 평가 수익률 막대. LS 잔고 조회(t0424)가 이미 종목별 `return_rate`를
 * 주므로 별도 조회 없이 그 값만 정렬·시각화한다 - 기간별 수익률(FOCCQ33600)은
 * 모의투자 계좌에서 브로커가 그 자체로 막아(rsp_cd 01900) 대신할 수 없다.
 */
function HoldingReturnChart({ rows }: { rows: Holding[] }) {
  const items = rows
    .map((holding) => ({
      key: holding.symbol ?? holding.name ?? "",
      label: holding.name ?? holding.symbol ?? "이름 없음",
      value: parseChartNumber(holding.return_rate),
    }))
    .filter((item): item is { key: string; label: string; value: number } => item.value !== null)
    .sort((left, right) => right.value - left.value);

  const maxMagnitude = Math.max(1, ...items.map((item) => Math.abs(item.value)));

  return (
    <div className="w-full min-w-0 rounded-md border border-outline-variant bg-surface-container-low px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <span className="flex items-center gap-2 text-label-md font-label-md uppercase text-on-surface-variant">
          <span className="material-symbols-outlined text-[16px]" aria-hidden="true">
            bar_chart
          </span>
          종목별 평가 수익률
        </span>
        <span className="text-[11px] text-on-surface-variant">{items.length}종목</span>
      </div>

      {items.length > 0 ? (
        <ul className="m-0 mt-3 flex flex-col gap-2.5 p-0">
          {items.map((item) => {
            const positive = item.value >= 0;
            const widthPercent = (Math.abs(item.value) / maxMagnitude) * 100;
            return (
              <li key={item.key} className="list-none">
                <div className="flex items-center justify-between gap-2 text-xs">
                  <span className="truncate text-on-surface" title={item.label}>
                    {item.label}
                  </span>
                  <span className={`shrink-0 font-data-mono font-semibold ${positive ? "text-primary" : "text-error"}`}>
                    {positive ? "+" : ""}
                    {item.value.toFixed(2)}%
                  </span>
                </div>
                <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-surface-container-lowest">
                  <div
                    className={`h-full rounded-full ${positive ? "bg-primary" : "bg-error"}`}
                    style={{ width: `${Math.max(widthPercent, 2)}%` }}
                  />
                </div>
              </li>
            );
          })}
        </ul>
      ) : (
        <p className="m-0 mt-3 flex min-h-[13rem] items-center justify-center rounded border border-outline-variant bg-surface-container-lowest px-4 text-center text-xs text-on-surface-variant">
          종목별 수익률을 확인하는 중입니다.
        </p>
      )}
    </div>
  );
}

function HoldingRow({ holding }: { holding: Holding }) {
  return (
    <tr className="border-b border-outline-variant last:border-b-0">
      <td className="truncate px-3 py-2.5 text-on-surface" title={holding.symbol ?? undefined}>
        {holding.name ?? holding.symbol ?? "—"}
      </td>
      <td className="px-3 py-2.5 text-right font-data-mono text-on-surface-variant">
        {formatNumber(holding.quantity)}
      </td>
      <td className="px-3 py-2.5 text-right font-data-mono text-on-surface-variant">
        {formatMoney(holding.average_cost)}
      </td>
      <td className="px-3 py-2.5 text-right font-data-mono text-on-surface">
        {formatMoney(holding.market_value)}
      </td>
      <td className="px-3 py-2.5 text-right font-data-mono text-on-surface-variant">
        {formatPercent(holding.return_rate)}
      </td>
    </tr>
  );
}

type EventSide = "BUY" | "SELL";

function getEventSide(value: string | null): EventSide | null {
  const normalized = (value ?? "").toUpperCase();
  if (/(매수|BUY)/.test(normalized)) return "BUY";
  if (/(매도|SELL)/.test(normalized)) return "SELL";
  return null;
}

function formatEventPrice(event: OrderEvent): string {
  // SC0 접수는 체결가가 아니므로 브로커가 0을 보내도 화면에서는 가격을 단정하지 않는다.
  return event.kind === "ACCEPTED" ? "—" : formatMoney(event.price);
}

function getEventTone(event: OrderEvent): string {
  if (event.kind !== "FILLED") return KIND_TONE[event.kind] ?? KIND_TONE.ACCEPTED;
  const side = getEventSide(event.side);
  if (side === "BUY") {
    return "border-red-300 bg-red-50 text-red-700";
  }
  if (side === "SELL") {
    return "border-blue-300 bg-blue-50 text-blue-700";
  }
  return KIND_TONE.ACCEPTED;
}

function getEventOrigin(event: OrderEvent): { label: string; detail: string } {
  const ids = [
    event.conditional_rule_id ? `조건 규칙 ${event.conditional_rule_id}` : null,
    event.directive_id ? `directive ${event.directive_id}` : null,
    event.order_request_id ? `요청 ${event.order_request_id}` : null,
  ].filter(Boolean);
  const detail = ids.join(" · ");
  if (event.correlation_status === "ATTRIBUTED" && event.conditional_rule_id) {
    return { label: "조건주문", detail };
  }
  if (event.correlation_status === "ATTRIBUTED") {
    return { label: "사용자 주문", detail };
  }
  if (event.origin === "EXTERNAL_HTS") {
    return { label: "외부 HTS", detail: "LS 계좌에서 직접 발생한 주문" };
  }
  return { label: "출처 미확인", detail: "LS 주문은 확인됐지만 내부 요청과 연결되지 않았습니다." };
}

function EventRow({ event }: { event: OrderEvent }) {
  const side = getEventSide(event.side);
  const origin = getEventOrigin(event);
  return (
    <tr className="border-b border-outline-variant last:border-b-0">
      <td className="px-3 py-2.5 font-data-mono text-on-surface-variant">{formatEventTime(event.event_time)}</td>
      <td className="px-3 py-2.5">
        <span
          className={`inline-flex whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-semibold ${getEventTone(event)}`}
        >
          {event.label}
        </span>
      </td>
      <td className="truncate px-3 py-2.5 text-on-surface" title={event.symbol ?? undefined}>
        {event.symbol_name ?? event.symbol ?? "—"}
      </td>
      <td className="px-3 py-2.5 text-on-surface-variant">
        {side === "BUY" ? "매수" : side === "SELL" ? "매도" : event.side ?? "—"}
      </td>
      <td className="px-3 py-2.5 text-right font-data-mono text-on-surface">{formatNumber(event.quantity)}</td>
      <td className="px-3 py-2.5 text-right font-data-mono text-on-surface-variant">{formatEventPrice(event)}</td>
      <td className="px-3 py-2.5 text-on-surface-variant" title={origin.detail || undefined}>
        <span
          className={`inline-flex whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-semibold ${
            event.correlation_status === "ATTRIBUTED"
              ? "border-primary/30 bg-primary/10 text-primary"
              : "border-outline-variant bg-surface-container text-on-surface-variant"
          }`}
        >
          {origin.label}
        </span>
      </td>
      <td
        className="truncate px-3 py-2.5 text-right font-data-mono text-outline"
        title={event.orig_order_no ? `원주문 ${event.orig_order_no}` : undefined}
      >
        {event.order_no ?? "—"}
      </td>
    </tr>
  );
}

function TodayTradingSummary({ activity }: { activity: PortfolioLive["today_activity"] }) {
  if (!activity) return null;
  const activityData = activity.data;

  return (
    <section className="min-w-0 rounded-lg border border-outline-variant bg-surface-container-lowest" aria-labelledby="today-trading-title">
      <div className="flex items-center justify-between gap-3 border-b border-outline-variant px-4 py-3">
        <div>
          <h3 id="today-trading-title" className="m-0 text-title-md font-title-md text-primary">오늘 거래 요약</h3>
          <p className="m-0 mt-1 text-xs text-on-surface-variant">오늘 체결된 거래와 결제 흐름을 빠르게 확인합니다.</p>
        </div>
        <span className="text-xs text-on-surface-variant">{activity.as_of ? "오늘 기준" : "확인 중"}</span>
      </div>
      {activity.error ? (
        <p role="alert" className="m-0 border-b border-error/40 bg-error-container px-4 py-2 text-xs text-on-error-container">
          오늘 거래 요약을 불러오지 못했습니다: {activity.error}
        </p>
      ) : null}
      {activityData ? (
        <div className="grid grid-cols-2 gap-2 p-4 md:grid-cols-3 lg:grid-cols-6">
          <SummaryTile label="거래 횟수" value={String(activityData.trade_count) + "건"} />
          <SummaryTile label="매수 금액" value={formatMoney(activityData.summary.buy_amount)} />
          <SummaryTile label="매도 금액" value={formatMoney(activityData.summary.sell_amount)} />
          <SummaryTile label="총 거래금액" value={formatMoney(activityData.summary.total_amount)} />
          <SummaryTile label="수수료" value={formatMoney(activityData.summary.total_fee)} />
          <SummaryTile label="세금" value={formatMoney(activityData.summary.total_tax)} />
        </div>
      ) : (
        <p className="m-0 px-4 py-6 text-center text-sm text-on-surface-variant">
          {activity.as_of ? "오늘 체결된 거래가 없습니다." : "오늘 거래 요약을 확인하는 중입니다."}
        </p>
      )}
    </section>
  );
}

export default function LivePortfolioPanel() {
  // 재조회가 실패해도 `data`는 마지막 성공값 그대로 남고 `error`만 따로 온다 -
  // 한 번 끊겼다고 화면에 떠 있던 잔고가 사라지지 않는다.
  const query = useQuery<PortfolioLive, PortfolioLiveError>({
    queryKey: ["portfolio-live"],
    queryFn: () => fetchPortfolioLive(),
    refetchInterval: POLL_MS,
    staleTime: 0,
    // 어차피 POLL_MS마다 다시 부른다. 재시도는 첫 화면만 늦춘다.
    retry: false,
  });
  const data = query.data ?? null;
  const error = query.error ?? null;
  const loading = query.isPending;

  const kinds =
    data?.orders.kinds ?? ORDER_KINDS.map((kind) => ({ kind, label: FALLBACK_KIND_LABELS[kind] }));

  // 오늘 사건만 남긴다. 실시간분은 UTC 오프셋(`...+00:00`), 과거 조회분은 KST
  // naive(`2026-08-18T15:19:47`)라 앞 10자를 자르면 새벽에 하루가 어긋난다 -
  // Date로 파싱하면 둘 다 같은 로컬 시각으로 떨어진다.
  const today = new Date().toDateString();
  const recentOrders = (data?.orders.recent ?? []).filter(
    (event) => new Date(event.received_at).toDateString() === today,
  );

  return (
    <section
      className="lg:col-span-2 min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm"
      aria-labelledby="live-portfolio-title"
    >
      <div className="flex items-center justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-2.5">
        <span className="flex min-w-0 items-center gap-2 text-label-md font-label-md text-on-surface-variant">
          <span className="material-symbols-outlined text-[16px]" aria-hidden="true">
            account_balance
          </span>
          <span className="truncate">trading_portfolio.live</span>
        </span>
      </div>

      <div className="space-y-5 p-4 md:p-6">
        <div className="space-y-4">
          <div className="min-w-0">
            <p className="m-0 text-label-md font-label-md uppercase text-on-surface-variant">
              Trading · Accounting / Portfolio
            </p>
            <h2 id="live-portfolio-title" className="mt-2 text-headline-md font-headline-md font-bold text-primary">
              주문 현황과 계좌 잔고
            </h2>
            <p className="mt-2 max-w-3xl text-body-sm font-body-sm text-on-surface-variant">
              주문이 접수·체결·정정·취소·거부될 때마다 상태가 바뀌고, 체결이 나면 잔고를 다시 맞춥니다. <br/>이 화면은 조회만 하며
              주문을 내지 않습니다.
            </p>
          </div>

          <div className="grid min-w-0 gap-4 lg:grid-cols-2">
            <PortfolioAllocationChart rows={data?.holdings.rows ?? []} />
            <HoldingReturnChart rows={data?.holdings.rows ?? []} />
          </div>
        </div>

        {data && !data.account.registered ? (
          <p role="status" className="m-0 rounded border border-outline-variant bg-surface-container px-3 py-2 text-xs text-on-surface-variant">
            {data.account.error
              ? `계좌 확인에 실패했습니다: ${data.account.error}`
              : "계좌를 확인하는 중입니다. 계좌번호는 연결된 값을 서버가 받아 오므로 직접 입력하지 않습니다."}
          </p>
        ) : null}

        {error ? (
          <div
            className={`rounded-lg border p-4 text-sm ${
              error.status === 503
                ? "border-outline-variant bg-surface-container-low text-on-surface-variant"
                : "border-error/40 bg-error-container text-on-error-container"
            }`}
            role={error.status === 503 ? "status" : "alert"}
          >
            <p className="m-0 font-semibold">
              {error.status === 503 ? "실시간 연동이 꺼져 있습니다." : "포트폴리오를 불러오지 못했습니다."}
            </p>
            <p className="m-0 mt-1">{error.message}</p>
          </div>
        ) : null}

        {loading && !data && !error ? (
          <p className="m-0 rounded-lg border border-outline-variant bg-surface-container-low p-5 text-sm text-on-surface-variant">
            계좌 상태를 확인하는 중입니다…
          </p>
        ) : null}

        <TodayTradingSummary activity={data?.today_activity} />

        {/* 잔고 요약 */}
        <div className="grid grid-cols-2 gap-2 md:grid-cols-5" aria-label="계좌 요약">
          <SummaryTile label="추정순자산" value={formatMoney(data?.holdings.net_asset ?? null)} />
          <SummaryTile label="평가금액" value={formatMoney(data?.holdings.valuation ?? null)} />
          <SummaryTile label="매입금액" value={formatMoney(data?.holdings.purchase_amount ?? null)} />
          <SummaryTile label="평가손익" value={formatMoney(data?.holdings.valuation_pnl ?? null)} />
          <SummaryTile label="실현손익" value={formatMoney(data?.holdings.realized_pnl ?? null)} />
        </div>

        {/* 주문 상태 5종 */}
        <div className="grid grid-cols-2 gap-2 md:grid-cols-5" aria-label="주문 상태별 건수">
          {kinds.map((item) => (
            <KindTile
              key={item.kind}
              kind={item.kind}
              label={item.label}
              count={data?.orders.counts[item.kind] ?? 0}
            />
          ))}
        </div>

        <div className="grid min-w-0 grid-cols-1 gap-4">
          <section
            className="order-2 min-w-0 rounded-lg border border-outline-variant bg-surface-container-lowest"
            aria-labelledby="live-holdings-title"
          >
            <div className="flex items-center justify-between gap-3 border-b border-outline-variant px-4 py-3">
              <h3 id="live-holdings-title" className="m-0 text-title-md font-title-md text-primary">
                보유 종목
              </h3>
              <span className="text-xs text-on-surface-variant">{data?.holdings.rows.length ?? 0}건</span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[480px] table-fixed text-left text-xs">
                <thead className="border-b border-outline-variant bg-surface-container-low text-label-md text-on-surface-variant">
                  <tr>
                    <th className="w-[30%] px-3 py-2 font-semibold">종목</th>
                    <th className="w-[15%] px-3 py-2 text-right font-semibold">수량</th>
                    <th className="w-[20%] px-3 py-2 text-right font-semibold">평균단가</th>
                    <th className="w-[22%] px-3 py-2 text-right font-semibold">평가액</th>
                    <th className="w-[13%] px-3 py-2 text-right font-semibold">수익률</th>
                  </tr>
                </thead>
                <tbody>
                  {data && data.holdings.rows.length > 0 ? (
                    data.holdings.rows.map((holding) => (
                      <HoldingRow key={holding.symbol ?? holding.name ?? ""} holding={holding} />
                    ))
                  ) : (
                    <tr>
                      <td colSpan={5} className="px-3 py-7 text-center text-sm text-on-surface-variant">
                        {data?.holdings.as_of ? "보유 종목이 없습니다." : "잔고를 확인하는 중입니다."}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          <section
            className="order-1 min-w-0 rounded-lg border border-outline-variant bg-surface-container-lowest"
            aria-labelledby="live-orders-title"
          >
            <div className="flex items-center justify-between gap-3 border-b border-outline-variant px-4 py-3">
              <h3 id="live-orders-title" className="m-0 text-title-md font-title-md text-primary">
                주문 사건
              </h3>
              <span className="text-xs text-on-surface-variant">
                {data?.environment_label ?? "PAPER"} · {data?.account.masked ?? "계좌 확인 중"} · {recentOrders.length}건
              </span>
            </div>
            {data?.stream.status !== "CONNECTED" ? (
              <p role="status" className="m-0 border-b border-outline-variant bg-surface-container px-4 py-2 text-xs text-on-surface-variant">
                실시간 주문 알림을 다시 연결하는 중입니다. 주문 내역과 계좌 정보는 조회 결과로 계속 표시합니다.
              </p>
            ) : null}
            {data?.orders.error ? (
              <p role="alert" className="m-0 border-b border-error/40 bg-error-container px-4 py-2 text-xs text-on-error-container">
                과거 주문 사건을 불러오지 못해 실시간 수신분만 표시합니다: {data.orders.error}
              </p>
            ) : null}
            {data?.orders.correlation?.status === "DEGRADED" ? (
              <p role="alert" className="m-0 border-b border-error/40 bg-error-container px-4 py-2 text-xs text-on-error-container">
                LS 주문은 표시하지만 내부 조건주문 출처 연결을 확인하지 못했습니다: {data.orders.correlation.error}
              </p>
            ) : null}
            <div className="max-h-[17rem] overflow-auto">
              <table className="w-full min-w-[760px] table-fixed text-left text-xs">
                <thead className="sticky top-0 z-10 border-b border-outline-variant bg-surface-container-low text-label-md text-on-surface-variant">
                  <tr>
                    <th className="w-[11%] px-3 py-2 font-semibold">시각</th>
                    <th className="w-[10%] px-3 py-2 font-semibold">상태</th>
                    <th className="w-[19%] px-3 py-2 font-semibold">종목</th>
                    <th className="w-[9%] px-3 py-2 font-semibold">매매</th>
                    <th className="w-[10%] px-3 py-2 text-right font-semibold">수량</th>
                    <th className="w-[15%] px-3 py-2 text-right font-semibold">가격</th>
                    <th className="w-[14%] px-3 py-2 font-semibold">출처</th>
                    <th className="w-[12%] px-3 py-2 text-right font-semibold">주문번호</th>
                  </tr>
                </thead>
                <tbody>
                  {recentOrders.length > 0 ? (
                    recentOrders.map((event) => (
                      <EventRow
                        key={`${event.kind}-${event.order_no ?? "none"}-${event.received_at}-${event.seq}`}
                        event={event}
                      />
                    ))
                  ) : (
                    <tr>
                      <td colSpan={8} className="px-3 py-7 text-center text-sm text-on-surface-variant">
                        {data?.stream.status === "CONNECTED"
                          ? "연결되어 있습니다. 주문이 발생하면 여기에 바로 나타납니다."
                          : "아직 수신한 주문 사건이 없습니다."}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>
        </div>

        {/* 로컬 상태와 브로커 잔고가 어긋나면 감추지 않는다 */}
        {data && data.holdings.drift.length > 0 ? (
          <p role="alert" className="m-0 rounded border border-error/40 bg-error-container px-3 py-2 text-xs text-on-error-container">
            체결로 계산한 수량과 계좌 잔고가 다릅니다:{" "}
            {data.holdings.drift.map((item) => `${item.symbol} ${item.local}→${item.broker}`).join(", ")}. 계좌 값을 기준으로
            맞췄습니다.
          </p>
        ) : null}

        {data?.holdings.error ? (
          <p role="alert" className="m-0 rounded border border-error/40 bg-error-container px-3 py-2 text-xs text-on-error-container">
            잔고 확인 실패: {data.holdings.error}
          </p>
        ) : null}

        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-outline-variant pt-3 text-xs text-on-surface-variant">
          <span>LS PAPER 계좌 기준 · 주문·체결은 LS 조회, 공식 수치는 회계 원장이 확정합니다</span>
          <span>{POLL_MS / 1000}초마다 자동 갱신</span>
        </div>
      </div>
    </section>
  );
}
