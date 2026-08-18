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
  ACCEPTED: "border-outline-variant bg-surface-container text-on-surface-variant",
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

const STREAM_STATUS: Record<string, { label: string; tone: string }> = {
  CONNECTED: {
    label: "연결됨",
    tone: "border-tertiary-fixed-dim bg-tertiary-fixed/30 text-on-tertiary-fixed-variant",
  },
  IDLE: { label: "연결 준비", tone: "border-outline-variant bg-surface-container text-on-surface-variant" },
  DISCONNECTED: { label: "연결 끊김", tone: "border-error/40 bg-error-container text-on-error-container" },
  STOPPED: { label: "중지됨", tone: "border-outline-variant bg-surface-container text-on-surface-variant" },
};

function Badge({ children, tone }: { children: React.ReactNode; tone?: string }) {
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-full border px-2.5 py-0.5 text-[10px] font-semibold ${
        tone ?? "border-outline-variant bg-surface-container-lowest text-on-surface-variant"
      }`}
    >
      {children}
    </span>
  );
}

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

function EventRow({ event }: { event: OrderEvent }) {
  return (
    <tr className="border-b border-outline-variant last:border-b-0">
      <td className="px-3 py-2.5 font-data-mono text-on-surface-variant">{formatEventTime(event.event_time)}</td>
      <td className="px-3 py-2.5">
        <span
          className={`inline-flex whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-semibold ${
            KIND_TONE[event.kind] ?? KIND_TONE.ACCEPTED
          }`}
        >
          {event.label}
        </span>
      </td>
      <td className="truncate px-3 py-2.5 text-on-surface" title={event.symbol ?? undefined}>
        {event.symbol_name ?? event.symbol ?? "—"}
      </td>
      <td className="px-3 py-2.5 text-on-surface-variant">{event.side ?? "—"}</td>
      <td className="px-3 py-2.5 text-right font-data-mono text-on-surface">{formatNumber(event.quantity)}</td>
      <td className="px-3 py-2.5 text-right font-data-mono text-on-surface-variant">{formatMoney(event.price)}</td>
      <td
        className="truncate px-3 py-2.5 text-right font-data-mono text-outline"
        title={event.orig_order_no ? `원주문 ${event.orig_order_no}` : undefined}
      >
        {event.order_no ?? "—"}
      </td>
    </tr>
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

  const status = data ? STREAM_STATUS[data.stream.status] ?? STREAM_STATUS.IDLE : null;
  const kinds =
    data?.orders.kinds ?? ORDER_KINDS.map((kind) => ({ kind, label: FALLBACK_KIND_LABELS[kind] }));

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
        <div className="flex shrink-0 items-center gap-1.5">
          {data ? <Badge>{data.environment_label}</Badge> : null}
          <Badge>비공식</Badge>
          {status ? <Badge tone={status.tone}>{status.label}</Badge> : null}
        </div>
      </div>

      <div className="space-y-5 p-4 md:p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="m-0 text-label-md font-label-md uppercase text-on-surface-variant">
              Trading · Accounting / Portfolio
            </p>
            <h2 id="live-portfolio-title" className="mt-2 text-headline-md font-headline-md font-bold text-primary">
              주문 현황과 계좌 잔고
            </h2>
            <p className="mt-2 max-w-3xl text-body-sm font-body-sm text-on-surface-variant">
              주문이 접수·체결·정정·취소·거부될 때마다 상태가 바뀌고, 체결이 나면 잔고를 다시 맞춥니다. 이 화면은 조회만 하며
              주문을 내지 않습니다.
            </p>
          </div>

          {/* 계좌 — 등록 안 된 것을 '잔고 0'으로 바꾸지 않는다 */}
          <div className="min-w-[13rem] shrink-0 rounded-md border border-outline-variant bg-surface-container-low px-4 py-3">
            <span className="text-label-md font-label-md uppercase text-on-surface-variant">계좌</span>
            <p className="m-0 mt-1 font-data-mono text-title-md font-bold text-primary">
              {data?.account.masked ?? "확인 중"}
            </p>
            <p className="m-0 mt-0.5 text-[11px] text-on-surface-variant">
              {data?.holdings.as_of
                ? `잔고 ${new Date(data.holdings.as_of).toLocaleTimeString("ko-KR")} 기준`
                : "잔고 확인 전"}
            </p>
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

        <div className="grid min-w-0 grid-cols-1 gap-4 xl:grid-cols-2">
          <section
            className="min-w-0 rounded-lg border border-outline-variant bg-surface-container-lowest"
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
            className="min-w-0 rounded-lg border border-outline-variant bg-surface-container-lowest"
            aria-labelledby="live-orders-title"
          >
            <div className="flex items-center justify-between gap-3 border-b border-outline-variant px-4 py-3">
              <h3 id="live-orders-title" className="m-0 text-title-md font-title-md text-primary">
                주문 사건
              </h3>
              <span className="text-xs text-on-surface-variant">{data?.orders.recent.length ?? 0}건</span>
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[620px] table-fixed text-left text-xs">
                <thead className="border-b border-outline-variant bg-surface-container-low text-label-md text-on-surface-variant">
                  <tr>
                    <th className="w-[13%] px-3 py-2 font-semibold">시각</th>
                    <th className="w-[12%] px-3 py-2 font-semibold">상태</th>
                    <th className="w-[23%] px-3 py-2 font-semibold">종목</th>
                    <th className="w-[10%] px-3 py-2 font-semibold">매매</th>
                    <th className="w-[13%] px-3 py-2 text-right font-semibold">수량</th>
                    <th className="w-[17%] px-3 py-2 text-right font-semibold">가격</th>
                    <th className="w-[12%] px-3 py-2 text-right font-semibold">주문번호</th>
                  </tr>
                </thead>
                <tbody>
                  {data && data.orders.recent.length > 0 ? (
                    data.orders.recent.map((event) => <EventRow key={event.seq} event={event} />)
                  ) : (
                    <tr>
                      <td colSpan={7} className="px-3 py-7 text-center text-sm text-on-surface-variant">
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
        {data?.stream.error ? (
          <p role="alert" className="m-0 rounded border border-error/40 bg-error-container px-3 py-2 text-xs text-on-error-container">
            연결 오류: {data.stream.error}
          </p>
        ) : null}

        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-outline-variant pt-3 text-xs text-on-surface-variant">
          <span>계좌 기준 · 공식 수치는 회계 원장이 확정합니다</span>
          <span>{POLL_MS / 1000}초마다 자동 갱신</span>
        </div>
      </div>
    </section>
  );
}
