"use client";

import { useQuery } from "@tanstack/react-query";
import {
  fetchPortfolioLive,
  formatMoney,
  formatNumber,
  PortfolioLiveError,
  type PortfolioLive,
} from "../lib/portfolioLiveClient";

const POLL_MS = 3000;

function SyncBadge({ label, tone }: { label: string; tone: string }) {
  return <span className={"rounded-full border px-2.5 py-1 text-[10px] font-semibold " + tone}>{label}</span>;
}

export function TodayTradingSummaryCard() {
  const query = useQuery<PortfolioLive, PortfolioLiveError>({
    queryKey: ["portfolio-live"],
    queryFn: () => fetchPortfolioLive(),
    refetchInterval: POLL_MS,
    staleTime: 0,
    retry: false,
  });
  const data = query.data ?? null;
  const activity = data?.today_activity;
  const payload = activity?.data;
  const summary = payload?.summary;
  const detailError = activity?.error ?? data?.stream.error ?? data?.orders.error ?? query.error?.message ?? null;
  const hasError = Boolean(detailError);
  const isUnavailable = query.error?.status === 503;

  return (
    <section
      className="min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm"
      aria-labelledby="today-trading-summary-title"
    >
      <div className="flex items-start justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-3">
        <div className="min-w-0">
          <p className="m-0 text-label-md font-label-md uppercase text-on-surface-variant">Daily Trading</p>
          <h2 id="today-trading-summary-title" className="m-0 mt-1 text-title-md font-title-md font-bold text-primary">
            금일 매매 브리핑
          </h2>
        </div>
        {hasError ? (
          <SyncBadge
            label={isUnavailable ? "미연동" : "확인 필요"}
            tone="border-error/40 bg-error-container text-on-error-container"
          />
        ) : payload ? null : (
          <SyncBadge label="확인 중" tone="border-outline-variant bg-surface-container text-on-surface-variant" />
        )}
      </div>

      <div className="space-y-3 p-4">
        {payload ? (
          <>
            <div
              className="flex flex-wrap items-center gap-x-4 gap-y-1 rounded-md border border-outline-variant bg-surface-container-low px-3 py-2 text-xs"
              aria-label="오늘 매수 매도 금액 요약"
            >
              <span className="text-on-surface-variant">
                매수 약정 <b className="font-data-mono text-error">{formatMoney(summary?.buy_amount ?? null)}</b>
              </span>
              <span className="text-on-surface-variant">
                매도 약정 <b className="font-data-mono text-primary">{formatMoney(summary?.sell_amount ?? null)}</b>
              </span>
              <span className="text-on-surface-variant">
                수수료 {formatMoney(summary?.total_fee ?? null)} · 세금 {formatMoney(summary?.total_tax ?? null)}
              </span>
            </div>

            <div className="rounded-md border border-secondary-container bg-secondary-container/40 px-3 py-2.5">
              <div className="flex items-center gap-2 text-label-md font-label-md text-primary">
                <span className="material-symbols-outlined text-[16px]" aria-hidden="true">
                  auto_awesome
                </span>
                한눈에 보기
              </div>
              <p className="m-0 mt-1.5 text-sm leading-6 text-on-surface">
                {payload.trade_count > 0 ? (
                  <>
                    오늘 <b>{formatNumber(String(payload.trade_count))}건</b>의 매매 기록이 있습니다. 총 약정{" "}
                    <b>{formatMoney(summary?.total_amount ?? null)}</b>입니다.
                  </>
                ) : (
                  "오늘 기록된 매매 내역이 없습니다."
                )}
              </p>
            </div>

            <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 border-t border-outline-variant pt-3 text-xs text-on-surface-variant">
              <span>
                총 수량 {formatNumber(summary?.buy_quantity ?? null)} 매수 · {formatNumber(summary?.sell_quantity ?? null)} 매도
              </span>
              <span>정산 {formatMoney(summary?.total_settlement ?? null)}</span>
            </div>
          </>
        ) : (
          <div className="flex min-h-[15rem] flex-col items-center justify-center rounded-md border border-outline-variant bg-surface-container-lowest px-4 text-center">
            <span className="material-symbols-outlined text-3xl text-outline" aria-hidden="true">
              {isUnavailable ? "cloud_off" : detailError ? "error" : "hourglass_top"}
            </span>
            <p className="m-0 mt-3 text-sm font-semibold text-on-surface">
              {isUnavailable
                ? "실시간 계좌 연동이 꺼져 있습니다."
                : detailError
                  ? "오늘 매매 요약을 불러오지 못했습니다."
                  : "오늘 매매 요약을 확인하는 중입니다."}
            </p>
            <p className="m-0 mt-1 max-w-xl break-words text-xs text-on-surface-variant">
              {isUnavailable
                ? "연동을 켜면 t0150 당일 매매 내역이 표시됩니다."
                : detailError ?? "브로커 응답을 기다리고 있습니다."}
            </p>
          </div>
        )}

        {activity?.error && payload ? (
          <p className="m-0 text-[11px] text-error">마지막 갱신에 실패해 이전 요약을 표시 중입니다.</p>
        ) : null}
      </div>
    </section>
  );
}
