"use client";

import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import {
  fetchMarketRanking,
  MarketRankingError,
  type MarketRankingKind,
  type MarketRankingRow,
} from "../lib/marketRankingClient";
import { formatMoney, formatNumber, formatPercent } from "../lib/portfolioLiveClient";

const TAB_OPTIONS: { kind: MarketRankingKind; label: string }[] = [
  { kind: "amount", label: "거래대금 상위" },
  { kind: "volume", label: "거래량 상위" },
  { kind: "change", label: "등락률 상위" },
];

function metricValue(row: MarketRankingRow, kind: MarketRankingKind): string {
  if (kind === "volume") return `${formatNumber(row.volume)}주`;
  if (kind === "amount") return formatMoney(row.amount);
  return formatPercent(row.change_rate);
}

function changeTone(value: string | null): string {
  if (!value || value.startsWith("-")) return "text-error";
  if (value === "0" || value === "0.0") return "text-on-surface-variant";
  return "text-tertiary";
}

export function MarketRankingCard() {
  const [activeKind, setActiveKind] = useState<MarketRankingKind>("amount");
  const query = useQuery<Awaited<ReturnType<typeof fetchMarketRanking>>, MarketRankingError>({
    queryKey: ["market-ranking", activeKind],
    queryFn: () => fetchMarketRanking(activeKind),
    refetchInterval: 30_000,
    staleTime: 15_000,
    retry: false,
  });
  const data = query.data;
  const isUnavailable = query.error?.status === 503;

  return (
    <section
      className="min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm"
      aria-labelledby="market-ranking-title"
    >
      <div className="border-b border-outline-variant bg-surface-container-low px-4 py-3">
        <div className="flex items-start justify-between gap-3">
          <div className="min-w-0">
            <p className="m-0 text-label-md font-label-md uppercase text-on-surface-variant">Market Snapshot</p>
            <h2 id="market-ranking-title" className="m-0 mt-1 text-title-md font-title-md font-bold text-primary">
              오늘의 시장 상위 종목
            </h2>
          </div>
          <span className="shrink-0 rounded-full border border-outline-variant bg-surface-container-lowest px-2.5 py-1 text-[10px] font-semibold text-on-surface-variant">
            TOP 5
          </span>
        </div>
        <div className="mt-3 flex flex-wrap gap-2" role="tablist" aria-label="시장 상위 종목 기준">
          {TAB_OPTIONS.map((option) => {
            const selected = activeKind === option.kind;
            return (
              <button
                key={option.kind}
                type="button"
                role="tab"
                aria-selected={selected}
                onClick={() => setActiveKind(option.kind)}
                className={`rounded-md border px-2.5 py-1.5 text-xs font-semibold transition-colors ${
                  selected
                    ? "border-primary bg-primary text-on-primary"
                    : "border-outline-variant bg-surface-container-lowest text-on-surface-variant hover:bg-surface-container"
                }`}
              >
                {option.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="p-4">
        {query.isLoading && !data ? (
          <div className="flex min-h-56 items-center justify-center rounded-md border border-outline-variant text-sm text-on-surface-variant">
            시장 상위 종목을 불러오는 중입니다.
          </div>
        ) : query.error ? (
          <div className="flex min-h-56 flex-col items-center justify-center rounded-md border border-error/30 bg-error-container/30 px-4 text-center">
            <span className="material-symbols-outlined text-3xl text-error" aria-hidden="true">
              {isUnavailable ? "cloud_off" : "error"}
            </span>
            <p className="m-0 mt-3 text-sm font-semibold text-on-error-container">
              {isUnavailable ? "시장 데이터 연동이 꺼져 있습니다." : "시장 상위 종목을 불러오지 못했습니다."}
            </p>
            <p className="m-0 mt-1 max-w-xl break-words text-xs text-on-surface-variant">
              {query.error?.message ?? "잠시 후 다시 시도해 주세요."}
            </p>
          </div>
        ) : data && data.rows.length > 0 ? (
          <ol className="m-0 divide-y divide-outline-variant rounded-md border border-outline-variant p-0">
            {data.rows.map((row) => (
              <li key={`${row.rank}-${row.symbol ?? row.name ?? "unknown"}`} className="list-none px-3 py-3">
                <div className="flex items-center gap-3">
                  <span className="flex size-7 shrink-0 items-center justify-center rounded-full bg-secondary-container text-xs font-bold text-primary">
                    {row.rank}
                  </span>
                  <div className="min-w-0 flex-1">
                    <div className="flex min-w-0 items-baseline gap-2">
                      <b className="truncate text-sm text-on-surface">{row.name ?? "이름 없음"}</b>
                      <span className="shrink-0 text-[11px] text-on-surface-variant">{row.symbol ?? "-"}</span>
                    </div>
                    <p className="m-0 mt-1 text-xs text-on-surface-variant">현재가 {formatMoney(row.price)}</p>
                  </div>
                  <div className="shrink-0 text-right">
                    <b className={`font-data-mono text-sm ${changeTone(row.change_rate)}`}>
                      {metricValue(row, activeKind)}
                    </b>
                    <p className={`m-0 mt-1 text-[11px] ${changeTone(row.change_rate)}`}>
                      {formatPercent(row.change_rate)}
                    </p>
                  </div>
                </div>
              </li>
            ))}
          </ol>
        ) : (
          <div className="flex min-h-56 items-center justify-center rounded-md border border-outline-variant text-sm text-on-surface-variant">
            조회된 종목이 없습니다.
          </div>
        )}
        <p className="m-0 mt-3 text-[11px] text-on-surface-variant">LS 시장 상위 조회 · 가볍게 참고하는 오늘의 메인</p>
      </div>
    </section>
  );
}
