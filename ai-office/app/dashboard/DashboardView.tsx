"use client";

import { useQuery } from "@tanstack/react-query";
import { useMemo, useSyncExternalStore } from "react";
import LivePortfolioPanel from "../components/LivePortfolioPanel";
import { fetchHermesKanban, type HermesKanbanCard } from "../lib/kanbanClient";
import { KANBAN_BASE_URL, resolveKanbanUrl } from "../lib/kanbanUrl";
import { CeoControlRoomChat } from "./CeoControlRoomChat";
import { MarketRankingCard } from "./MarketRankingCard";
import { PanelBar } from "./PanelBar";
import { TodayTradingSummaryCard } from "./TodayTradingSummaryCard";

/**
 * 대표 Dashboard.
 *
 */

function createdAtMs(value: HermesKanbanCard["created_at"]): number {
  if (typeof value === "number") return value < 1_000_000_000_000 ? value * 1000 : value;
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    return Number.isNaN(parsed) ? 0 : parsed;
  }
  return 0;
}

function formatCreatedAt(value: HermesKanbanCard["created_at"]): string {
  const timestamp = createdAtMs(value);
  if (!timestamp) return "—";
  return new Date(timestamp).toLocaleString("ko-KR", {
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

const NO_SUBSCRIBE = () => () => {};

function usePageHost(): string {
  return useSyncExternalStore(
    NO_SUBSCRIBE,
    () => window.location.hostname,
    () => "",
  );
}

export function RecentOutputsPanel() {
  const pageHost = usePageHost();
  const kanbanUrl = useMemo(
    () => resolveKanbanUrl(KANBAN_BASE_URL, pageHost || undefined),
    [pageHost],
  );
  const query = useQuery({
    queryKey: ["hermes-kanban"],
    queryFn: fetchHermesKanban,
    refetchInterval: 10_000,
    staleTime: 3_000,
    retry: false,
  });
  const outputs = [...(query.data?.columns.done ?? [])]
    .sort((left, right) => createdAtMs(right.created_at) - createdAtMs(left.created_at))
    .slice(0, 5);

  return (
    <section className="bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden shadow-sm">
      <PanelBar icon="inventory_2" title="result_storage" />
      <div className="p-6">
        <span className="block text-label-md font-label-md text-on-surface-variant uppercase mb-1">Recent Outputs</span>
        <h2 className="text-headline-md font-headline-md text-primary mb-4">결과물 창고</h2>
        {query.isPending ? (
          <p className="m-0 rounded border border-outline-variant bg-surface-container-low p-5 text-sm text-on-surface-variant">
            완료된 결과물을 불러오는 중입니다.
          </p>
        ) : query.error ? (
          <p className="m-0 rounded border border-error/40 bg-error-container p-5 text-sm text-on-error-container" role="alert">
            결과물 창고를 불러오지 못했습니다: {query.error.message}
          </p>
        ) : outputs.length === 0 ? (
          <p className="m-0 rounded border border-outline-variant bg-surface-container-low p-5 text-sm text-on-surface-variant">
            완료된 결과물이 없습니다.
          </p>
        ) : (
          <div className="overflow-x-auto border border-outline-variant rounded">
            <table className="w-full text-left text-body-sm font-body-sm">
              <thead className="bg-surface-container-low border-b border-outline-variant text-label-md font-label-md text-secondary uppercase">
                <tr>
                  <th className="p-4 font-semibold">결과물</th>
                  <th className="p-4 font-semibold">담당</th>
                  <th className="p-4 font-semibold">완료 시각</th>
                  <th className="p-4 font-semibold text-right">바로가기</th>
                </tr>
              </thead>
              <tbody>
                {outputs.map((row) => (
                  <tr key={row.task_id} className="border-b border-outline-variant last:border-b-0 hover:bg-surface transition-colors">
                    <td className="p-4 text-on-surface">{row.title}</td>
                    <td className="p-4 text-on-surface-variant">{row.assignee || "—"}</td>
                    <td className="p-4 text-on-surface-variant">{formatCreatedAt(row.created_at)}</td>
                    <td className="p-4 text-right">
                      {kanbanUrl ? (
                        <a
                          className="text-primary underline underline-offset-2"
                          href={kanbanUrl}
                          target="_blank"
                          rel="noreferrer"
                        >
                          보드 보기
                        </a>
                      ) : (
                        <span className="text-outline">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </section>
  );
}

export default function DashboardView() {
  return (
    <>
      <main className="flex-1 w-full max-w-app mx-auto p-margin-mobile md:p-margin-desktop flex flex-col gap-gutter">
        {/* ── 요약 헤더 — 카드에 올리지 않고 바탕에 그대로 둔다 ── */}
        <section className="flex justify-between items-start gap-gutter flex-wrap">
          <div className="min-w-0">
            <p className="text-label-md font-label-md text-on-surface-variant uppercase">Today Overview</p>
            <h1 className="text-headline-lg font-headline-lg text-primary font-bold tracking-tight mt-2">
              오늘 회사가 어떻게 움직이는지 <span className="bg-secondary-container px-2">한눈에</span> 보여드려요
            </h1>
            <p className="text-body-sm font-body-sm text-on-surface-variant mt-2 max-w-3xl">
              Worker는 context를 만들고, 결정은 권한을 가진 결정론적 Gate와 대표님이 맡아요.
            </p>
          </div>
          <span className="text-label-md font-label-md text-outline shrink-0">
            실제 전송·게시·결제는 대표 승인 후 진행해요
          </span>
        </section>

        {/* ── CEO Control Room / 실시간 포트폴리오 ───────── */}
        <div className="grid grid-cols-1 lg:grid-cols-3 gap-gutter items-start">
          <div className="flex min-w-0 flex-col gap-gutter">
            <CeoControlRoomChat />
            <TodayTradingSummaryCard />
            <MarketRankingCard />
          </div>
          <LivePortfolioPanel />
        </div>

      </main>

    </>
  );
}
