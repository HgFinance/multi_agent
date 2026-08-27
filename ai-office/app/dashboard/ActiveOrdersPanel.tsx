"use client";

import { useQuery } from "@tanstack/react-query";
import {
  fetchConditionalRules,
  formatConditionalRuleAction,
  formatConditionalRuleCondition,
  formatConditionalRuleExpiry,
  type ConditionalRuleError,
  type ConditionalRuleView,
} from "../lib/conditionalRuleClient";
import { fetchPortfolioLive, type PortfolioLive } from "../lib/portfolioLiveClient";

const POLL_MS = 5000;

function symbolNames(data: PortfolioLive | undefined): Map<string, string> {
  const names = new Map<string, string>();
  for (const holding of data?.holdings.rows ?? []) {
    if (holding.symbol && holding.name) names.set(holding.symbol, holding.name);
  }
  for (const event of data?.orders.recent ?? []) {
    if (event.symbol && event.symbol_name) names.set(event.symbol, event.symbol_name);
  }
  return names;
}

function ActiveRuleRow({ rule, names }: { rule: ConditionalRuleView; names: Map<string, string> }) {
  const symbolName = names.get(rule.spec.symbol);
  return (
    <tr className="border-b border-outline-variant last:border-b-0">
      <td className="px-3 py-3 align-top text-on-surface">
        <div className="mb-2">
          <span className="inline-flex max-w-full rounded-full border border-primary/30 bg-secondary-container px-2 py-1 text-[11px] font-semibold leading-4 text-primary">
            {formatConditionalRuleAction(rule.spec.action)}
          </span>
        </div>
        <div className="font-semibold">{symbolName ?? rule.spec.symbol}</div>
        {symbolName ? <div className="mt-0.5 font-data-mono text-[11px] text-on-surface-variant">{rule.spec.symbol}</div> : null}
      </td>
      <td className="px-3 py-3 align-top text-on-surface">
        <div className="break-words leading-5">{formatConditionalRuleCondition(rule.spec.condition)}</div>
        <div className="mt-1 text-[11px] text-on-surface-variant">{formatConditionalRuleExpiry(rule.spec.expires_at)}</div>
      </td>
    </tr>
  );
}

export function ActiveOrdersPanel() {
  const rulesQuery = useQuery<ConditionalRuleView[], ConditionalRuleError>({
    queryKey: ["conditional-rules"],
    queryFn: fetchConditionalRules,
    refetchInterval: POLL_MS,
    staleTime: 2000,
    retry: false,
  });
  const portfolioQuery = useQuery<PortfolioLive>({
    queryKey: ["portfolio-live"],
    queryFn: () => fetchPortfolioLive(),
    refetchInterval: POLL_MS,
    staleTime: 0,
    retry: false,
  });
  const activeRules = (rulesQuery.data ?? []).filter((rule) => rule.state === "ACTIVE");
  const names = symbolNames(portfolioQuery.data);

  return (
    <section
      className="min-w-0 overflow-hidden rounded-lg border border-outline-variant border-l-4 border-l-primary bg-surface-container-lowest shadow-sm"
      aria-labelledby="active-orders-title"
    >
      <div className="flex items-start justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-3">
        <div className="min-w-0">
          <p className="m-0 text-label-md font-label-md uppercase text-on-surface-variant">Active Orders</p>
          <h2 id="active-orders-title" className="m-0 mt-1 text-title-md font-title-md font-bold text-primary">
            현재 대기 중인 주문
          </h2>
        </div>
        {rulesQuery.data ? (
          <span className="shrink-0 rounded-full border border-primary/30 bg-secondary-container px-2.5 py-1 text-xs font-semibold text-primary">
            {activeRules.length}건 활성
          </span>
        ) : (
          <span className="shrink-0 rounded-full border border-outline-variant bg-surface-container px-2.5 py-1 text-[10px] font-semibold text-on-surface-variant">
            확인 중
          </span>
        )}
      </div>

      <div className="p-4">
        {rulesQuery.isPending ? (
          <p className="m-0 rounded-md border border-outline-variant bg-surface-container-low px-3 py-5 text-center text-sm text-on-surface-variant">
            현재 대기 중인 주문을 확인하는 중입니다.
          </p>
        ) : rulesQuery.error ? (
          <p role="alert" className="m-0 rounded-md border border-error/40 bg-error-container px-3 py-5 text-center text-sm text-on-error-container">
            대기 중인 주문을 불러오지 못했습니다: {rulesQuery.error.message}
          </p>
        ) : activeRules.length === 0 ? (
          <p className="m-0 rounded-md border border-outline-variant bg-surface-container-low px-3 py-5 text-center text-sm text-on-surface-variant">
            현재 활성화된 조건주문이 없습니다.
          </p>
        ) : (
          <div className="rounded-md border border-outline-variant">
            <table className="w-full table-fixed text-left text-sm">
              <thead className="border-b border-outline-variant bg-surface-container-low text-label-md text-on-surface-variant">
                <tr>
                  <th className="w-[38%] px-3 py-2.5 font-semibold">종목</th>
                  <th className="w-[62%] px-3 py-2.5 font-semibold">조건</th>
                </tr>
              </thead>
              <tbody>
                {activeRules.map((rule) => (
                  <ActiveRuleRow key={rule.rule_id} rule={rule} names={names} />
                ))}
              </tbody>
            </table>
          </div>
        )}
        <p className="m-0 mt-3 text-[11px] text-on-surface-variant">
          조건을 만족하면 PAPER 주문으로 실행되며, 주문 실행 전까지 이 목록에서 확인할 수 있습니다.
        </p>
      </div>
    </section>
  );
}

export default ActiveOrdersPanel;
