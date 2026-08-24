"use client";

import { useQuery } from "@tanstack/react-query";
import {
  fetchAccountingLedger,
  formatLedgerCell,
  formatLedgerDate,
  formatLedgerTime,
  formatMoney,
  signTone,
  AccountingLedgerError,
  type AccountingLedger,
  type LedgerEntry,
} from "../lib/accountingLedgerClient";

/**
 * 회계 거래 원장 패널.
 *
 * 트레이딩 패널과 일부러 다르게 생겼다. 저쪽은 "주문이 지금 어떤 상태인가"를
 * 보고, 여기는 **"확정된 거래가 얼마였고 비용이 얼마였나"**를 본다 — 회계가
 * 실제로 쓰는 축이다. 그래서 도넛·실시간 배지 대신 차변/대변식 정렬, 잔액
 * 이월 칸, 합계 행(`tfoot`)을 둔다.
 *
 * 여기 나오는 값은 **브로커 거래 기준**이고 우리 공식 원장이 아니다. "비공식"
 * 배지를 박아 두는 이유이고, 마감으로 확정하는 것은 회계본부 원장이다.
 */

const POLL_MS = 30_000; // 확정된 과거 거래라 실시간일 이유가 없다

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

type LedgerSide = "BUY" | "SELL";

/** 원천마다 category/summary 중 한 곳에만 방향이 들어올 수 있어 둘 다 확인한다. */
function getLedgerSide(entry: LedgerEntry): LedgerSide | null {
  const category = (entry.category ?? "").toUpperCase();
  if (/(매수|BUY)/.test(category)) return "BUY";
  if (/(매도|SELL)/.test(category)) return "SELL";

  const summary = (entry.summary ?? "").toUpperCase();
  if (/(매수|BUY)/.test(summary)) return "BUY";
  if (/(매도|SELL)/.test(summary)) return "SELL";
  return null;
}

function ledgerSideTone(side: LedgerSide | null): string {
  if (side === "BUY") return "border-red-300 bg-red-50 text-red-700";
  if (side === "SELL") return "border-blue-300 bg-blue-50 text-blue-700";
  return "border-outline-variant bg-surface-container-lowest text-outline";
}

function LedgerRow({ entry, showDate }: { entry: LedgerEntry; showDate: boolean }) {
  const cancelled = Boolean(entry.cancelled && entry.cancelled.trim());
  const side = getLedgerSide(entry);
  const sideTextTone = side === "BUY" ? "text-red-700" : side === "SELL" ? "text-blue-700" : "text-on-surface";
  const sideRowTone = side === "BUY" ? "border-l-2 border-l-red-400 bg-red-50/20" : side === "SELL" ? "border-l-2 border-l-blue-400 bg-blue-50/20" : "";
  return (
    <tr className={`border-b border-outline-variant/70 last:border-b-0 ${sideRowTone} ${cancelled ? "opacity-55" : ""}`}>
      <td className="whitespace-nowrap px-3 py-2 align-top font-data-mono text-on-surface-variant">
        {/* 같은 날은 첫 줄에만 날짜를 적는다 - 종이 원장이 하는 방식이고, 하루 단위 묶음이 눈에 들어온다 */}
        <span className="block">{showDate ? formatLedgerDate(entry.trade_date) : ""}</span>
        <span className="block text-[11px] tabular-nums text-on-surface-variant">
          {formatLedgerTime(entry.trade_time)}
        </span>
      </td>
      <td className="px-3 py-2 align-top">
        <span className={`block truncate ${sideTextTone}`} title={entry.summary ?? undefined}>
          {entry.summary ?? entry.category ?? "—"}
        </span>
        <span className="mt-0.5 flex flex-wrap items-center gap-1">
          {side ? (
            <span className={`inline-flex whitespace-nowrap rounded-full border px-1.5 py-px text-[10px] font-semibold ${ledgerSideTone(side)}`}>
              {side === "BUY" ? "매수" : "매도"}
            </span>
          ) : entry.category ? (
            <span className="text-[10px] text-outline">{entry.category}</span>
          ) : null}
        </span>
        {cancelled ? <span className="text-[10px] text-[color:var(--color-error)]">{entry.cancelled}</span> : null}
      </td>
      <td className="px-3 py-2 align-top">
        <span className="block truncate text-on-surface" title={entry.symbol ?? undefined}>
          {entry.symbol_name ?? "—"}
        </span>
        {entry.symbol ? <span className="font-data-mono text-[10px] text-outline">{entry.symbol}</span> : null}
      </td>
      <td className="px-3 py-2 text-right align-top font-data-mono text-on-surface-variant">
        {formatLedgerCell(entry.quantity)}
      </td>
      <td className="px-3 py-2 text-right align-top font-data-mono text-on-surface-variant">
        {formatLedgerCell(entry.unit_price)}
      </td>
      <td className={`px-3 py-2 text-right align-top font-data-mono font-semibold ${sideTextTone}`}>
        {formatLedgerCell(entry.amount)}
      </td>
      <td className="px-3 py-2 text-right align-top font-data-mono text-outline">
        {formatLedgerCell(entry.commission)}
      </td>
      <td className="px-3 py-2 text-right align-top font-data-mono text-outline">{formatLedgerCell(entry.tax)}</td>
      <td className={`px-3 py-2 text-right align-top font-data-mono font-semibold ${signTone(entry.realized_pnl)}`}>
        {formatLedgerCell(entry.realized_pnl)}
      </td>
      <td className="whitespace-nowrap bg-surface-container-low/60 px-3 py-2 text-right align-top font-data-mono font-semibold text-primary">
        {formatLedgerCell(entry.cash_after)}
      </td>
    </tr>
  );
}

type StatementRow = {
  account: string;
  value: string;
  basis: string;
  tone?: string;
  strong?: boolean;
};

type StatementGroup = {
  label: string;
  rows: StatementRow[];
};

/**
 * 계좌·손익·비용·거래 현황을 하나의 표로 묶는다.
 *
 * 실현손익은 제비용 반영 값이고 평가손익은 미확정 값이므로 같은 손익 구역에
 * 놓되 산출 기준을 분명히 적는다. 비용은 총손익에서 다시 차감하지 않는다.
 */
function FinancialStatement({ data }: { data: AccountingLedger | null }) {
  const summary = data?.account_summary;
  const pnl = data?.pnl;
  const totals = data?.totals;
  const asOf = summary?.as_of
    ? `${new Date(summary.as_of).toLocaleTimeString("ko-KR", { hour: "2-digit", minute: "2-digit" })} 기준`
    : "조회 시점 기준";

  const groups: StatementGroup[] = [
    {
      label: "자산",
      rows: [
        {
          account: "추정순자산",
          value: formatMoney(summary?.net_asset ?? null),
          basis: "보유자산 평가액과 예수금 등을 반영한 추정치",
          strong: true,
        },
        {
          account: "보유자산 평가금액",
          value: formatMoney(summary?.valuation ?? null),
          basis: summary ? `${summary.holding_count}종목 · ${asOf}` : "잔고 조회 후 표시",
        },
        {
          account: "보유자산 매입원가",
          value: formatMoney(summary?.purchase_amount ?? null),
          basis: "현재 보유분의 취득원가",
        },
        {
          account: "기말 예수금",
          value: formatMoney(data?.cash_balance ?? null),
          basis: data ? (data.cash_balance ? "기간 마지막 거래 직후 잔액" : "기간 내 거래 없음") : "조회 후 표시",
        },
      ],
    },
    {
      label: "손익",
      rows: [
        {
          account: "실현손익",
          value: formatMoney(pnl?.realized ?? summary?.realized_pnl ?? null),
          basis: "매도로 확정 · 제비용 반영",
          tone: signTone(pnl?.realized ?? summary?.realized_pnl ?? null),
        },
        {
          account: "평가손익",
          value: formatMoney(pnl?.valuation ?? summary?.valuation_pnl ?? null),
          basis: "보유 중 · 미확정",
          tone: signTone(pnl?.valuation ?? summary?.valuation_pnl ?? null),
        },
        {
          account: "총손익",
          value: formatMoney(pnl?.total ?? null),
          basis: "실현손익 + 평가손익",
          tone: signTone(pnl?.total ?? null),
          strong: true,
        },
      ],
    },
    {
      label: "거래비용",
      rows: [
        { account: "수수료", value: formatMoney(totals?.commission ?? null), basis: "조회 기간 합계" },
        { account: "세금", value: formatMoney(totals?.tax ?? null), basis: "거래세·소득세·주민세" },
        {
          account: "비용 합계",
          value: formatMoney(totals?.cost ?? null),
          basis: pnl?.cost_included_in_realized ? "실현손익에 이미 반영 · 중복 차감 안 함" : "수수료 + 세금",
          strong: true,
        },
      ],
    },
    {
      label: "거래 현황",
      rows: [
        { account: "거래 건수", value: totals ? `${totals.count}건` : "—", basis: "조회 기간 전체" },
        { account: "배당 수입", value: formatMoney(totals?.dividend ?? null), basis: "조회 기간 합계" },
      ],
    },
  ];

  return (
    <section className="min-w-0 overflow-hidden rounded-lg border border-outline-variant" aria-label="계좌 원장">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-outline-variant bg-primary px-4 py-3 text-on-primary">
        <div>
          <h3 className="m-0 text-title-md font-title-md font-bold">계좌 원장</h3>
        </div>
        <span className="text-xs font-semibold opacity-90">자산 · 손익 · 비용 · 거래 현황</span>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[720px] border-collapse text-left text-body-sm">
          <caption className="sr-only">계좌의 자산, 손익, 거래비용과 거래 현황을 합친 원장</caption>
          <thead className="border-b border-outline-variant bg-surface-container text-label-md text-on-surface-variant">
            <tr>
              <th scope="col" className="w-[15%] px-4 py-2.5 font-semibold">구분</th>
              <th scope="col" className="w-[25%] px-4 py-2.5 font-semibold">계정과목</th>
              <th scope="col" className="w-[22%] px-4 py-2.5 text-right font-semibold">금액 · 건수</th>
              <th scope="col" className="w-[38%] px-4 py-2.5 font-semibold">산출 기준</th>
            </tr>
          </thead>
          {groups.map((group) => (
            <tbody key={group.label} className="border-b border-outline-variant last:border-b-0">
              {group.rows.map((row, index) => (
                <tr key={row.account} className="border-b border-outline-variant/60 last:border-b-0 hover:bg-surface-container-low/60">
                  {index === 0 ? (
                    <th
                      scope="rowgroup"
                      rowSpan={group.rows.length}
                      className="border-r border-outline-variant bg-surface-container-low px-4 py-3 align-top font-semibold text-primary"
                    >
                      {group.label}
                    </th>
                  ) : null}
                  <th scope="row" className={`px-4 py-2.5 font-medium text-on-surface ${row.strong ? "font-bold" : ""}`}>
                    {row.account}
                  </th>
                  <td
                    className={`whitespace-nowrap px-4 py-2.5 text-right font-data-mono tabular-nums ${
                      row.strong ? "font-bold" : "font-semibold"
                    } ${row.tone ?? "text-on-surface"}`}
                  >
                    {row.value}
                  </td>
                  <td className="px-4 py-2.5 text-xs leading-5 text-on-surface-variant">{row.basis}</td>
                </tr>
              ))}
            </tbody>
          ))}
        </table>
      </div>
      <p className="m-0 border-t border-outline-variant bg-surface-container-low px-4 py-2.5 text-[11px] leading-5 text-on-surface-variant">
        거래 기준의 참고 표입니다. 공식 장부와 NAV는 회계본부 마감 후 확정됩니다.
      </p>
    </section>
  );
}

function AccountingCloseStatus({ data }: { data: AccountingLedger | null }) {
  if (!data) return null;
  const totalCount = Math.max(data.totals.count, 0);
  const unsettledCount = Math.max(data.totals.unsettled_count, 0);
  const settledCount = Math.max(totalCount - unsettledCount, 0);
  const settledPercent = totalCount > 0 ? Math.round((settledCount / totalCount) * 100) : 0;
  const persistedLabel = data.persisted === true ? "저장된 원장" : data.persisted === false ? "조회 결과" : "상태 확인 중";
  const sourceLabel = data.authoritative ? "공식 기준" : "참고 기준";

  return (
    <section className="min-w-0 rounded-lg border border-outline-variant bg-surface-container-lowest" aria-labelledby="accounting-close-status-title">
      <div className="border-b border-outline-variant bg-surface-container-low px-4 py-3">
        <h3 id="accounting-close-status-title" className="m-0 text-title-md font-title-md font-semibold text-primary">이번 기간 결산 상태</h3>
        <p className="m-0 mt-1 text-body-sm font-body-sm text-on-surface-variant">
          아래 장부 숫자가 확정값인지, 아직 결제가 남았는지 먼저 확인합니다.
        </p>
      </div>
      <div className="grid grid-cols-2 gap-2 p-4 md:grid-cols-4">
        <div className="rounded-md border border-outline-variant bg-surface px-3 py-2.5">
          <span className="block text-xs text-on-surface-variant">원장 상태</span>
          <strong className="mt-1 block text-body-md font-body-md text-on-surface">{persistedLabel}</strong>
        </div>
        <div className="rounded-md border border-outline-variant bg-surface px-3 py-2.5">
          <span className="block text-xs text-on-surface-variant">결제 완료</span>
          <strong className="mt-1 block font-data-mono text-body-md text-on-surface">{settledCount}건</strong>
        </div>
        <div className="rounded-md border border-outline-variant bg-surface px-3 py-2.5">
          <span className="block text-xs text-on-surface-variant">결제 대기</span>
          <strong className="mt-1 block font-data-mono text-body-md text-on-surface">{unsettledCount}건</strong>
        </div>
        <div className="rounded-md border border-outline-variant bg-surface px-3 py-2.5">
          <span className="block text-xs text-on-surface-variant">손익 기준</span>
          <strong className="mt-1 block text-body-md font-body-md text-on-surface">{sourceLabel}</strong>
        </div>
      </div>
      <div className="px-4 pb-4">
        <div className="flex items-center justify-between gap-2 text-xs text-on-surface-variant">
          <span>결제 진행률</span>
          <span className="font-data-mono">{settledCount}/{totalCount}건 · {settledPercent}%</span>
        </div>
        <div className="mt-2 h-2 overflow-hidden rounded-full bg-surface-container-high" aria-label={"결제 진행률 " + settledPercent + "%"}>
          <div className="h-full rounded-full bg-primary transition-[width]" style={{ width: settledPercent + "%" }} />
        </div>
        {unsettledCount > 0 ? (
          <p className="m-0 mt-3 rounded border border-primary/30 bg-secondary-container px-3 py-2 text-body-sm text-primary">
            결제가 끝나지 않은 거래가 {unsettledCount}건 있어 최종 잔액과 손익은 변동될 수 있습니다.
          </p>
        ) : (
          <p className="m-0 mt-3 text-xs text-on-surface-variant">이 기간에는 결제 대기 거래가 없습니다.</p>
        )}
      </div>
    </section>
  );
}

export default function AccountingLedgerPanel() {
  const query = useQuery<AccountingLedger, AccountingLedgerError>({
    queryKey: ["accounting-ledger"],
    queryFn: () => fetchAccountingLedger(),
    refetchInterval: POLL_MS,
    staleTime: 0,
    retry: false,
  });
  const data = query.data ?? null;
  const error = query.error ?? null;
  const loading = query.isPending;

  const totals = data?.totals;

  return (
    <section
      className="min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm"
      aria-labelledby="accounting-ledger-title"
    >
      <div className="flex items-center justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-2.5">
        <span className="flex min-w-0 items-center gap-2 text-label-md font-label-md text-on-surface-variant">
          <span className="material-symbols-outlined text-[16px]" aria-hidden="true">
            receipt_long
          </span>
          <span className="truncate">accounting.financial_statement</span>
        </span>
        <div className="flex shrink-0 items-center gap-1.5">
          {data ? <Badge>{data.environment_label}</Badge> : null}
          {data?.account.masked ? <Badge>계좌 {data.account.masked}</Badge> : null}
        </div>
      </div>

      <div className="space-y-5 p-4 md:p-6">
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div className="min-w-0">
            <p className="m-0 text-label-md font-label-md uppercase text-on-surface-variant">
              Accounting · General Ledger
            </p>
            <h2
              id="accounting-ledger-title"
              className="mt-2 text-headline-md font-headline-md font-bold text-primary"
            >
              회계본부 관리 계좌 현황
            </h2>
            <p className="mt-2 max-w-3xl text-body-sm font-body-sm text-on-surface-variant">
              계좌 자산, 손익, 거래비용과 거래 현황을 하나의 표로 정리했습니다. 공식 장부는 회계본부 마감이 확정합니다.
            </p>
          </div>

          <div className="shrink-0 rounded-md border border-outline-variant bg-surface-container-low px-4 py-3 text-right">
            <span className="block text-label-md font-label-md uppercase text-on-surface-variant">회계기간</span>
            <p className="m-0 mt-1 font-data-mono text-body-sm font-semibold text-primary">
              {data ? `${data.period.start} ~ ${data.period.end}` : "확인 중"}
            </p>
            <span className="text-[11px] text-outline">최근 한 달 : {data ? `${data.period.days}일` : ""}</span>
          </div>
        </div>

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
              {error.status === 503 ? "브로커 연동이 꺼져 있습니다." : "거래 원장을 불러오지 못했습니다."}
            </p>
            <p className="m-0 mt-1">{error.message}</p>
          </div>
        ) : null}

        {loading && !data && !error ? (
          <p className="m-0 rounded-lg border border-outline-variant bg-surface-container-low p-5 text-sm text-on-surface-variant">
            거래 원장을 불러오는 중입니다…
          </p>
        ) : null}

        {data?.account_summary?.error ? (
          <p role="alert" className="m-0 rounded border border-error/40 bg-error-container px-3 py-2 text-xs text-on-error-container">
            잔고 조회 실패: {data.account_summary.error}
          </p>
        ) : null}

        {data?.source_error ? (
          <p role="alert" className="m-0 rounded border border-error/40 bg-error-container px-3 py-2 text-xs text-on-error-container">
            브로커 조회에 실패해 <b className="font-semibold">저장된 기록</b>을 보여 주고 있습니다 — 최신이 아닐 수 있습니다. ({data.source_error})
          </p>
        ) : null}

        {data?.today_error ? (
          <p role="alert" className="m-0 rounded border border-error/40 bg-error-container px-3 py-2 text-xs text-on-error-container">
            당일 매매일지를 불러오지 못했습니다: {data.today_error}. 아래 표에는 저장된 기록만 표시됩니다.
          </p>
        ) : null}

        <AccountingCloseStatus data={data} />

        <FinancialStatement data={data} />

        <details className="group min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest">
          <summary className="flex cursor-pointer list-none items-center justify-between gap-3 bg-surface-container-low px-4 py-3 marker:content-none">
            <span className="flex min-w-0 items-center gap-2">
              <span className="material-symbols-outlined text-[18px] text-on-surface-variant transition-transform group-open:rotate-180" aria-hidden="true">
                expand_more
              </span>
              <span className="min-w-0">
                <span className="block text-title-md font-title-md font-semibold text-primary">거래 내역</span>
                <span className="block text-[11px] text-outline">개별 거래가 필요할 때 펼쳐보세요</span>
              </span>
            </span>
            <span className="flex shrink-0 items-center gap-2 text-xs text-on-surface-variant">
              {data ? `${data.rows.length}건 · 최근순` : "—"}
            </span>
          </summary>

          <div className="max-h-[26rem] overflow-auto border-t border-outline-variant">
            <table className="w-full min-w-[900px] text-left text-xs">
              <thead className="sticky top-0 z-10 bg-surface-container text-label-md text-on-surface-variant shadow-[0_1px_0_var(--color-outline-variant)]">
                <tr>
                  <th className="w-[9%] px-3 py-2 font-semibold">거래일시</th>
                  <th className="w-[16%] px-3 py-2 font-semibold">적요</th>
                  <th className="w-[16%] px-3 py-2 font-semibold">종목</th>
                  <th className="w-[8%] px-3 py-2 text-right font-semibold">수량</th>
                  <th className="w-[10%] px-3 py-2 text-right font-semibold">단가</th>
                  <th className="w-[12%] px-3 py-2 text-right font-semibold">거래금액</th>
                  <th className="w-[8%] px-3 py-2 text-right font-semibold">수수료</th>
                  <th className="w-[8%] px-3 py-2 text-right font-semibold">세금</th>
                  <th className="w-[9%] px-3 py-2 text-right font-semibold">손익</th>
                  <th className="w-[12%] bg-surface-container px-3 py-2 text-right font-semibold">예수금 잔액</th>
                </tr>
              </thead>
              <tbody>
                {data && data.rows.length > 0 ? (
                  data.rows.map((entry, index) => (
                    <LedgerRow
                      key={`${entry.trade_date}-${entry.trade_no}-${index}`}
                      entry={entry}
                      showDate={index === 0 || data.rows[index - 1].trade_date !== entry.trade_date}
                    />
                  ))
                ) : (
                  <tr>
                    <td colSpan={10} className="px-3 py-10 text-center">
                      {/* 거래 0건과 조회 불가는 다른 말이다. 사유가 오면 그대로 보여 준다 */}
                      <p className="m-0 text-sm text-on-surface-variant">
                        {data?.notice ?? "이 기간에 기록된 거래가 없습니다."}
                      </p>
                      {data?.notice ? (
                        <p className="m-0 mt-1 text-xs text-outline">
                          브로커가 이 기간의 거래 원장을 제공하지 않았습니다. 금액을 0원으로 단정하지 않습니다.
                        </p>
                      ) : null}
                    </td>
                  </tr>
                )}
              </tbody>
              {data && data.rows.length > 0 && totals ? (
                <tfoot className="sticky bottom-0 border-t-2 border-outline-variant bg-surface-container-low font-semibold">
                  <tr>
                    <td className="px-3 py-2.5 text-on-surface" colSpan={6}>
                      합계 · {totals.count}건
                    </td>
                    <td className="px-3 py-2.5 text-right font-data-mono text-on-surface">
                      {formatLedgerCell(totals.commission)}
                    </td>
                    <td className="px-3 py-2.5 text-right font-data-mono text-on-surface">
                      {formatLedgerCell(totals.tax)}
                    </td>
                    <td className={`px-3 py-2.5 text-right font-data-mono ${signTone(totals.realized_pnl)}`}>
                      {formatLedgerCell(totals.realized_pnl)}
                    </td>
                    <td className="px-3 py-2.5 text-right font-data-mono text-primary">
                      {formatLedgerCell(data.cash_balance)}
                    </td>
                  </tr>
                </tfoot>
              ) : null}
            </table>
          </div>
        </details>

        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-outline-variant pt-3 text-xs text-on-surface-variant">
          <span>거래 기준 · 공식 장부는 회계본부 마감이 확정합니다</span>
          <span>
            {data?.totals.dividend && data.totals.dividend !== "0"
              ? `배당 수입 ${formatMoney(data.totals.dividend)} · `
              : ""}
            {POLL_MS / 1000}초마다 자동 갱신
          </span>
        </div>
      </div>
    </section>
  );
}
