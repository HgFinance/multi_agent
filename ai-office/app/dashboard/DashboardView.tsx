"use client";

import { useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import LivePortfolioPanel from "../components/LivePortfolioPanel";
import { renderDiscordMarkup } from "../lib/discordRender";
import { KANBAN_BASE_URL, resolveKanbanUrl } from "../lib/kanbanUrl";
import {
  fetchNotionReport,
  fetchNotionReports,
  type NotionReportCard,
  type NotionReportDetail,
} from "../lib/notionReportClient";
import { CeoControlRoomChat } from "./CeoControlRoomChat";
import ActiveOrdersPanel from "./ActiveOrdersPanel";
import { MarketRankingCard } from "./MarketRankingCard";
import { PanelBar } from "./PanelBar";
import { TodayTradingSummaryCard } from "./TodayTradingSummaryCard";

/**
 * 대표 Dashboard.
 *
 */

function formatPublishedAt(value: string | null): string {
  if (!value) return "—";
  const timestamp = Date.parse(value);
  if (Number.isNaN(timestamp)) return value;
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

/**
 * 리포트 모달. Discord 스레드 모달(`agent-logs/AgentLogsView.tsx`의
 * `ThreadDialog`)과 같은 방식·같은 겉모습이다 - `<dialog>`의 `showModal()`이
 * backdrop·Escape 닫기·포커스 트랩을 브라우저에서 주므로 오버레이나 키
 * 핸들러를 직접 만들지 않는다.
 * `m-auto`는 필수 - Tailwind preflight가 `<dialog>`의 기본 중앙 정렬을 지운다.
 */
function NotionReportDialog({
  card,
  kanbanUrl,
  onClose,
}: {
  card: NotionReportCard;
  /** 보드 주소를 신뢰할 수 없으면 `null` - 그때는 버튼을 아예 안 그린다. */
  kanbanUrl: string | null;
  onClose: () => void;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  const [report, setReport] = useState<NotionReportDetail | null>(null);
  const [error, setError] = useState("");

  useEffect(() => {
    ref.current?.showModal();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    fetchNotionReport(card.page_id, controller.signal)
      .then(setReport)
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setError(cause instanceof Error ? cause.message : "리포트를 불러오지 못했습니다.");
      });
    return () => controller.abort();
  }, [card.page_id]);

  // 본문이 오기 전에도 목록이 이미 아는 것(제목·발행 시각·링크)은 보여준다.
  const notionUrl = report?.url || card.url;

  return (
    <dialog
      ref={ref}
      onClose={onClose}
      aria-labelledby="notion-report-title"
      className="m-auto w-[min(46rem,92vw)] max-h-[85vh] p-0 rounded-xl bg-surface-container-lowest text-on-surface border border-outline-variant shadow-sm backdrop:bg-black/60"
    >
      <header className="flex items-start justify-between gap-3 bg-surface-container-lowest border-b border-outline-variant px-5 py-4">
        <div className="min-w-0">
          <h2
            id="notion-report-title"
            title={card.title}
            className="m-0 text-title-md font-title-md font-bold text-on-surface break-words line-clamp-2"
          >
            {card.title}
          </h2>
          <p className="m-0 mt-1 flex items-center gap-2 flex-wrap text-body-sm font-body-sm text-on-surface-variant">
            <span>{formatPublishedAt(card.published_at)}</span>
            {card.category ? (
              <span className="px-2 py-0.5 rounded-full border border-outline-variant bg-surface-container text-xs">
                {card.category}
              </span>
            ) : null}
            {card.state ? (
              <span className="px-2 py-0.5 rounded-full border border-primary/30 bg-secondary-container text-xs text-primary">
                {card.state}
              </span>
            ) : null}
          </p>
        </div>
        <form method="dialog" className="shrink-0">
          <button
            aria-label="리포트 닫기"
            className="grid place-items-center w-8 h-8 rounded-full text-on-surface-variant hover:bg-surface-container-high transition-colors"
          >
            <span className="material-symbols-outlined text-[20px]" aria-hidden="true">
              close
            </span>
          </button>
        </form>
      </header>

      <div className="px-5 py-4 overflow-y-auto max-h-[calc(85vh-10rem)]">
        {error ? (
          <p
            role="alert"
            className="text-body-sm font-body-sm text-on-error-container bg-error-container border border-error/40 rounded px-3 py-2 m-0"
          >
            {error}
          </p>
        ) : report === null ? (
          <p className="text-body-sm font-body-sm text-on-surface-variant m-0">리포트를 불러오는 중입니다…</p>
        ) : report.markdown.trim() ? (
          <div className="flex flex-col gap-2 text-body-sm font-body-sm leading-6 text-on-surface">
            {renderDiscordMarkup(report.markdown)}
            {report.truncated ? (
              <p className="m-0 mt-2 rounded border border-outline-variant bg-surface-container-low px-3 py-2 text-xs text-on-surface-variant">
                본문이 길어 앞부분만 표시했습니다. 전체는 Notion에서 보세요.
              </p>
            ) : null}
          </div>
        ) : (
          <p className="text-body-sm font-body-sm text-on-surface-variant m-0">
            이 리포트에는 본문 블록이 없습니다.
          </p>
        )}
      </div>

      <footer className="flex items-center justify-end gap-2 border-t border-outline-variant px-5 py-3">
        {kanbanUrl ? (
          <a
            className="inline-flex items-center gap-1.5 rounded-lg border border-outline-variant px-3 py-2 text-body-sm font-body-sm text-on-surface-variant hover:bg-surface-container transition-colors"
            href={kanbanUrl}
            target="_blank"
            rel="noreferrer"
          >
            <span className="material-symbols-outlined text-[18px]" aria-hidden="true">
              view_kanban
            </span>
            칸반 바로가기
          </a>
        ) : null}
        {notionUrl ? (
          <a
            className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-3 py-2 text-body-sm font-body-sm font-bold text-on-primary hover:opacity-90 transition-opacity"
            href={notionUrl}
            target="_blank"
            rel="noreferrer"
          >
            <span className="material-symbols-outlined text-[18px]" aria-hidden="true">
              open_in_new
            </span>
            Notion에서 열기
          </a>
        ) : null}
      </footer>
    </dialog>
  );
}

/**
 * 결과물 목록의 열 폭 — 헤더 행과 각 버튼 행이 반드시 같은 문자열을 쓴다.
 *
 * 전에는 `auto`로 잡았는데, 헤더와 각 행이 서로 다른 grid 컨테이너라서
 * `auto` 열은 각자 자기 글자 수만큼만 넓어졌다. 그래서 "구분"/"발행 시각"
 * 헤더가 값과 다른 자리에 섰다. 글자 수가 아니라 가로 비율로 못 박아
 * 어느 행이든 같은 위치에서 열이 시작하게 한다. 양 끝 고정폭 두 열은
 * 아이콘 자리다.
 */
const OUTPUT_ROW_GRID =
  "grid grid-cols-[1.25rem_minmax(0,1fr)_15%_15%_1.25rem] items-center gap-3 px-4";

/**
 * 결과물 창고 — **Notion에 실제로 발행된 리포트만** 보여준다.
 *
 * 전에는 Hermes Kanban의 done 열을 그대로 실었는데, done 카드와 발행된
 * 리포트는 같은 집합이 아니다. `DepartmentNotionProjection`의 부서 필터에
 * 걸리는 카드(ceo·qa·accounting·hr)는 Notion에 아무것도 남기지 않으므로,
 * 그 카드를 열면 보여줄 리포트가 없다. 목록의 출처를 Notion으로 옮겨서
 * "목록에 있으면 반드시 열린다"를 성립시킨다.
 */
export function RecentOutputsPanel() {
  const pageHost = usePageHost();
  const kanbanUrl = useMemo(
    () => resolveKanbanUrl(KANBAN_BASE_URL, pageHost || undefined),
    [pageHost],
  );
  const [openReport, setOpenReport] = useState<NotionReportCard | null>(null);
  const query = useQuery({
    queryKey: ["notion-reports"],
    queryFn: () => fetchNotionReports(20),
    refetchInterval: 30_000,
    staleTime: 10_000,
    retry: false,
  });
  const reports = query.data?.reports ?? [];

  return (
    <section className="bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden shadow-sm">
      <PanelBar icon="inventory_2" title="result_storage" />
      <div className="p-6">
        <span className="block text-label-md font-label-md text-on-surface-variant uppercase mb-1">Recent Outputs</span>
        <h2 className="text-headline-md font-headline-md text-primary mb-4">결과물 창고</h2>
        {query.isPending ? (
          <p className="m-0 rounded border border-outline-variant bg-surface-container-low p-5 text-sm text-on-surface-variant">
            발행된 리포트를 불러오는 중입니다.
          </p>
        ) : query.error ? (
          <p className="m-0 rounded border border-error/40 bg-error-container p-5 text-sm text-on-error-container" role="alert">
            결과물 창고를 불러오지 못했습니다: {query.error.message}
          </p>
        ) : reports.length === 0 ? (
          <p className="m-0 rounded border border-outline-variant bg-surface-container-low p-5 text-sm text-on-surface-variant">
            아직 발행된 리포트가 없습니다.
          </p>
        ) : (
          // 표가 아니라 헤더 + 버튼 목록이다. 행 전체가 하나의 손잡이여야 하는데
          // `<tr>`은 포커스를 못 받고 `<button>`으로 감쌀 수도 없다(HTML이
          // 허용하지 않는다). 헤더와 각 버튼이 `OUTPUT_ROW_GRID` 하나를
          // 공유해서 열이 표처럼 맞는다.
          //
          // 목록이 길어지므로 세로 스크롤을 붙이고, 헤더는 sticky로 남긴다.
          <div className="border border-outline-variant rounded max-h-80 overflow-y-auto">
            <div
              aria-hidden="true"
              className={`${OUTPUT_ROW_GRID} sticky top-0 z-10 bg-surface-container-low border-b border-outline-variant py-3 text-label-md font-label-md font-semibold text-secondary uppercase`}
            >
              <span />
              <span>결과물</span>
              <span>구분</span>
              <span className="text-right">발행 시각</span>
              <span />
            </div>
            <ul className="m-0 list-none p-0">
              {reports.map((row) => (
                <li key={row.page_id} className="border-b border-outline-variant last:border-b-0">
                  <button
                    type="button"
                    onClick={() => setOpenReport(row)}
                    title={row.title}
                    className={`${OUTPUT_ROW_GRID} group w-full cursor-pointer py-3 text-left text-body-sm font-body-sm hover:bg-surface-container focus-visible:bg-surface-container focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-primary transition-colors`}
                  >
                    <span
                      aria-hidden="true"
                      className="material-symbols-outlined text-[20px] leading-none text-on-surface-variant group-hover:text-primary transition-colors"
                    >
                      description
                    </span>
                    <span className="min-w-0 truncate text-on-surface group-hover:text-primary group-hover:underline transition-colors">
                      {row.title}
                    </span>
                    <span className="min-w-0 truncate text-on-surface-variant">{row.category ?? "—"}</span>
                    <span className="truncate text-right text-on-surface-variant">
                      {formatPublishedAt(row.published_at)}
                    </span>
                    <span
                      aria-hidden="true"
                      className="material-symbols-outlined text-[20px] leading-none text-outline group-hover:translate-x-0.5 group-hover:text-primary transition-all"
                    >
                      chevron_right
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          </div>
        )}
        {openReport ? (
          <NotionReportDialog
            key={openReport.page_id}
            card={openReport}
            kanbanUrl={kanbanUrl}
            onClose={() => setOpenReport(null)}
          />
        ) : null}
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
            <ActiveOrdersPanel />
            <TodayTradingSummaryCard />
            <MarketRankingCard />
          </div>
          <LivePortfolioPanel />
        </div>

      </main>

    </>
  );
}
