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
  type AccountSummary,
  type LedgerEntry,
  type Pnl,
} from "../lib/accountingLedgerClient";

/**
 * 회계 거래 원장 패널.
 *
 * 트레이딩 패널과 일부러 다르게 생겼다. 저쪽은 "주문이 지금 어떤 상태인가"를
 * 보고, 여기는 **"확정된 거래가 얼마였고 비용이 얼마였나"**를 본다 — 회계가
 * 실제로 쓰는 축이다. 그래서 도넛·실시간 배지 대신 차변/대변식 정렬, 잔액
 * 이월 칸, 합계 행(`tfoot`)을 둔다.
 *
 * 여기 나오는 값은 **브로커 정산 기준**이고 우리 공식 원장이 아니다. "비공식"
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

/** 회계가 보는 축. 비용은 묶어서 보여야 의미가 생긴다. */
function Metric({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
  tone?: string;
}) {
  return (
    <div className="min-w-0 border-l border-outline-variant px-4 py-1 first:border-l-0 first:pl-0">
      <span className="block text-label-md font-label-md uppercase text-on-surface-variant">{label}</span>
      <p className={`m-0 mt-1 truncate font-data-mono text-title-md font-bold ${tone ?? "text-primary"}`} title={value}>
        {value}
      </p>
      {hint ? <span className="mt-0.5 block text-[11px] text-outline">{hint}</span> : null}
    </div>
  );
}

function LedgerRow({ entry, showDate }: { entry: LedgerEntry; showDate: boolean }) {
  const cancelled = Boolean(entry.cancelled && entry.cancelled.trim());
  return (
    <tr className={`border-b border-outline-variant/70 last:border-b-0 ${cancelled ? "opacity-55" : ""}`}>
      <td className="whitespace-nowrap px-3 py-2 align-top font-data-mono text-on-surface-variant">
        {/* 같은 날은 첫 줄에만 날짜를 적는다 - 종이 원장이 하는 방식이고, 하루 단위 묶음이 눈에 들어온다 */}
        <span className="block">{showDate ? formatLedgerDate(entry.trade_date) : ""}</span>
        <span className="block text-[11px] tabular-nums text-on-surface-variant">
          {formatLedgerTime(entry.trade_time)}
        </span>
      </td>
      <td className="px-3 py-2 align-top">
        <span className="block truncate text-on-surface" title={entry.summary ?? undefined}>
          {entry.summary ?? entry.category ?? "—"}
        </span>
        <span className="mt-0.5 flex flex-wrap items-center gap-1">
          {entry.category ? <span className="text-[10px] text-outline">{entry.category}</span> : null}
          {/* 결제 전 줄을 확정 수치와 같은 모양으로 두면 회계가 마감에 그대로 쓴다 */}
          {entry.settlement === "UNSETTLED" ? (
            <span className="rounded-full border border-outline-variant bg-surface-container px-1.5 py-px text-[10px] font-semibold text-on-surface-variant">
              미결제
            </span>
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
      <td className="px-3 py-2 text-right align-top font-data-mono text-on-surface">
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

/** 계좌 기본정보. 기간 원장이 아니라 **지금 이 계좌**가 어떤 상태인가를 본다. */
function AccountCard({ summary }: { summary: AccountSummary | undefined }) {
  const cells: { label: string; value: string; tone?: string }[] = [
    { label: "추정순자산", value: formatMoney(summary?.net_asset ?? null) },
    { label: "평가금액", value: formatMoney(summary?.valuation ?? null) },
    { label: "매입금액", value: formatMoney(summary?.purchase_amount ?? null) },
    {
      label: "평가손익",
      value: formatMoney(summary?.valuation_pnl ?? null),
      tone: signTone(summary?.valuation_pnl ?? null),
    },
    {
      label: "실현손익",
      value: formatMoney(summary?.realized_pnl ?? null),
      tone: signTone(summary?.realized_pnl ?? null),
    },
  ];

  return (
    <section
      className="rounded-md border border-outline-variant bg-surface-container-lowest p-4"
      aria-labelledby="accounting-account-card"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="accounting-account-card" className="m-0 text-title-md font-title-md text-primary">
          계좌 기본정보
        </h3>
        <span className="text-[11px] text-outline">
          {summary?.holding_count ? `보유 ${summary.holding_count}종목` : "보유 없음"}
          {summary?.as_of ? ` · ${new Date(summary.as_of).toLocaleTimeString("ko-KR")} 기준` : ""}
        </span>
      </div>

      {summary?.error ? (
        <p role="alert" className="m-0 mt-2 rounded border border-error/40 bg-error-container px-3 py-2 text-xs text-on-error-container">
          잔고 조회 실패: {summary.error}
        </p>
      ) : null}

      <div className="mt-3 grid grid-cols-2 gap-y-4 md:grid-cols-3 xl:grid-cols-5">
        {cells.map((cell) => (
          <div key={cell.label} className="min-w-0 border-l border-outline-variant px-4 first:border-l-0 first:pl-0">
            <span className="block text-label-md font-label-md uppercase text-on-surface-variant">{cell.label}</span>
            <p
              className={`m-0 mt-1 truncate font-data-mono text-title-md font-bold ${cell.tone ?? "text-primary"}`}
              title={cell.value}
            >
              {cell.value}
            </p>
          </div>
        ))}
      </div>
    </section>
  );
}

/**
 * 손익(PNL).
 *
 * 실현과 평가를 한 칸에 합치지 않는다 — 평가손익은 아직 팔지 않은 값이라
 * 확정된 돈처럼 보이면 안 된다. 거래비용은 **총손익에서 빼지 않고** 참고로만
 * 둔다: 브로커 실현손익이 이미 제비용 포함 기준이라 또 빼면 이중 차감이다.
 */
function PnlCard({ pnl }: { pnl: Pnl | undefined }) {
  const lines = [
    { label: "실현손익", value: pnl?.realized ?? null, hint: "매도로 확정된 손익" },
    { label: "평가손익", value: pnl?.valuation ?? null, hint: "보유 중 · 미확정" },
  ];

  return (
    <section
      className="rounded-md border border-outline-variant bg-surface-container-lowest p-4"
      aria-labelledby="accounting-pnl-card"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="accounting-pnl-card" className="m-0 text-title-md font-title-md text-primary">
          손익 (PNL)
        </h3>
        <span className="text-[11px] text-outline">실현 + 평가</span>
      </div>

      <dl className="m-0 mt-3 space-y-2">
        {lines.map((line) => (
          <div key={line.label} className="flex items-baseline justify-between gap-3">
            <dt className="m-0 min-w-0 text-body-sm font-body-sm text-on-surface">
              {line.label}
              <span className="ml-2 text-[11px] text-outline">{line.hint}</span>
            </dt>
            <dd className={`m-0 shrink-0 font-data-mono text-body-sm font-semibold ${signTone(line.value)}`}>
              {formatMoney(line.value)}
            </dd>
          </div>
        ))}

        <div className="flex items-baseline justify-between gap-3 border-t border-outline-variant pt-2">
          <dt className="m-0 text-body-sm font-body-sm font-bold text-on-surface">총손익</dt>
          <dd className={`m-0 shrink-0 font-data-mono text-title-md font-bold ${signTone(pnl?.total ?? null)}`}>
            {formatMoney(pnl?.total ?? null)}
          </dd>
        </div>

        <div className="flex items-baseline justify-between gap-3 border-t border-outline-variant pt-2">
          <dt className="m-0 min-w-0 text-body-sm font-body-sm text-on-surface-variant">
            기간 거래비용
            <span className="ml-2 text-[11px] text-outline">
              수수료 {formatMoney(pnl?.commission ?? null)} · 세금 {formatMoney(pnl?.tax ?? null)}
            </span>
          </dt>
          <dd className="m-0 shrink-0 font-data-mono text-body-sm text-on-surface-variant">
            {formatMoney(pnl?.cost ?? null)}
          </dd>
        </div>
      </dl>

      {pnl?.cost_included_in_realized ? (
        <p className="m-0 mt-3 border-t border-outline-variant pt-2 text-[11px] leading-5 text-outline">
          거래비용은 총손익에서 다시 빼지 않았습니다. 실현손익이 제비용 포함 기준이라 한 번 더 빼면 이중 차감입니다 — 위 금액은
          비용 규모를 보기 위한 참고 수치입니다.
        </p>
      ) : null}
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
          <span className="truncate">accounting_ledger.transactions</span>
        </span>
        <div className="flex shrink-0 items-center gap-1.5">
          {data ? <Badge>{data.environment_label}</Badge> : null}
          {data?.account.masked ? <Badge>계좌 {data.account.masked}</Badge> : null}
          <Badge>비공식</Badge>
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
              계좌 거래 원장
            </h2>
            <p className="mt-2 max-w-3xl text-body-sm font-body-sm text-on-surface-variant">
              확정된 거래만 기록합니다. 수수료·세금·손익은 정산 기준이며, 공식 장부는 회계본부 마감이 확정합니다.
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

        <AccountCard summary={data?.account_summary} />

        {/* 회계 요약 — 비용과 손익을 나란히 둔다 */}
        <div className="grid grid-cols-2 gap-y-4 rounded-md border border-outline-variant bg-surface-container-lowest p-4 md:grid-cols-3 xl:grid-cols-6">
          <Metric label="거래 건수" value={totals ? `${totals.count}건` : "—"} />
          <Metric label="수수료" value={formatMoney(totals?.commission ?? null)} />
          <Metric label="세금" value={formatMoney(totals?.tax ?? null)} hint="거래세·소득세·주민세" />
          <Metric label="비용 합계" value={formatMoney(totals?.cost ?? null)} hint="수수료 + 세금" />
          <Metric
            label="실현손익"
            value={formatMoney(totals?.realized_pnl ?? null)}
            tone={signTone(totals?.realized_pnl ?? null)}
            hint="비용 차감 후 정산 기준"
          />
          <Metric
            label="기말 예수금"
            value={formatMoney(data?.cash_balance ?? null)}
            hint={data?.cash_balance ? "마지막 거래 직후" : "거래 없음"}
          />
        </div>

        {data && data.totals.unsettled_count > 0 ? (
          <p role="status" className="m-0 rounded border border-outline-variant bg-surface-container px-3 py-2 text-xs text-on-surface-variant">
            <b className="font-semibold text-on-surface">미결제 {data.totals.unsettled_count}건</b>이 포함돼 있습니다. 당일 체결분은
            결제(T+2)가 끝나기 전이라 확정 원장 대신 매매일지에서 옵니다 — 수수료·세금은 정산 기준이고, 실현손익은 결제 후에 확정됩니다.
          </p>
        ) : null}

        {data?.source_error ? (
          <p role="alert" className="m-0 rounded border border-error/40 bg-error-container px-3 py-2 text-xs text-on-error-container">
            브로커 조회에 실패해 <b className="font-semibold">저장된 기록</b>을 보여 주고 있습니다 — 최신이 아닐 수 있습니다. ({data.source_error})
          </p>
        ) : null}

        {data?.today_error ? (
          <p role="alert" className="m-0 rounded border border-error/40 bg-error-container px-3 py-2 text-xs text-on-error-container">
            당일 매매일지를 불러오지 못했습니다: {data.today_error}. 아래 표에는 결제가 끝난 거래만 나옵니다.
          </p>
        ) : null}

        <PnlCard pnl={data?.pnl} />

        <section
          className="min-w-0 overflow-hidden rounded-lg border border-outline-variant"
          aria-labelledby="accounting-ledger-table-title"
        >
          <div className="flex items-center justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-3">
            <h3 id="accounting-ledger-table-title" className="m-0 text-title-md font-title-md text-primary">
              거래 내역
            </h3>
            <span className="flex items-center gap-2 text-xs text-on-surface-variant">
              {data && data.totals.unsettled_count > 0 ? (
                <span className="rounded-full border border-outline-variant bg-surface-container-lowest px-2 py-0.5 font-semibold">
                  미결제 {data.totals.unsettled_count}건
                </span>
              ) : null}
              {data ? `${data.rows.length}건 · 최근순` : "—"}
            </span>
          </div>

          <div className="max-h-[26rem] overflow-auto">
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
        </section>

        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-2 border-t border-outline-variant pt-3 text-xs text-on-surface-variant">
          <span>정산 기준 · 공식 장부는 회계본부 마감이 확정합니다</span>
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
