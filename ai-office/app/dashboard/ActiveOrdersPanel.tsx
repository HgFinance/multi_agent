"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Fragment, useState } from "react";
import {
  cancelConditionalRule,
  fetchConditionalRules,
  formatConditionalRuleAction,
  formatConditionalRuleCondition,
  formatConditionalRuleExpiry,
  pauseConditionalRule,
  resumeConditionalRule,
  type ConditionalRuleError,
  type ConditionalRuleView,
} from "../lib/conditionalRuleClient";
import { askCeo, paperOrderWorkflowStatus } from "../lib/ceoClient";
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

function replacementInstruction(rule: ConditionalRuleView): string {
  return rule.raw_instruction;
}

const REPLACEMENT_FAILED_STATES = new Set([
  "CLARIFICATION_REQUIRED",
  "NOT_ORDER",
  "REJECTED",
  "FAILED",
  "UNKNOWN",
]);

async function waitForReplacement(orderRequestId: string): Promise<void> {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    const status = await paperOrderWorkflowStatus(orderRequestId);
    if (status.conditional_rules?.some((rule) => rule.state === "ACTIVE")) return;
    if (REPLACEMENT_FAILED_STATES.has(status.state)) {
      throw new Error(status.error_message || status.error_code || "수정 조건주문을 활성화하지 못했습니다.");
    }
    await new Promise((resolve) => window.setTimeout(resolve, 1_000));
  }
  throw new Error("수정 조건주문의 활성화 확인 시간이 초과되었습니다.");
}

function ActiveRuleRow({
  busy,
  editing,
  names,
  onCancel,
  onEdit,
  onSave,
  rule,
}: {
  busy: boolean;
  editing: boolean;
  names: Map<string, string>;
  onCancel: (rule: ConditionalRuleView) => void;
  onEdit: (rule: ConditionalRuleView) => void;
  onSave: (rule: ConditionalRuleView, instruction: string) => void;
  rule: ConditionalRuleView;
}) {
  const symbolName = names.get(rule.spec.symbol);
  const [instruction, setInstruction] = useState(() => replacementInstruction(rule));
  return (
    <Fragment>
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
        <td className="px-3 py-3 align-top">
          <div className="flex flex-col gap-2">
            <button
              type="button"
              className="rounded-md border border-primary/40 px-2 py-1.5 text-xs font-semibold text-primary hover:bg-secondary-container disabled:cursor-not-allowed disabled:opacity-50"
              disabled={busy}
              onClick={() => onEdit(rule)}
            >
              수정
            </button>
            <button
              type="button"
              className="rounded-md border border-error/40 px-2 py-1.5 text-xs font-semibold text-error hover:bg-error-container disabled:cursor-not-allowed disabled:opacity-50"
              disabled={busy}
              onClick={() => onCancel(rule)}
            >
              삭제
            </button>
          </div>
        </td>
      </tr>
      {editing ? (
        <tr className="border-b border-outline-variant bg-surface-container-low">
          <td colSpan={3} className="px-3 py-3">
            <label className="block text-xs font-semibold text-on-surface" htmlFor={`edit-rule-${rule.rule_id}`}>
              수정할 조건주문 원문
            </label>
            <textarea
              id={`edit-rule-${rule.rule_id}`}
              className="mt-2 min-h-24 w-full rounded-md border border-outline-variant bg-surface-container-lowest px-3 py-2 text-sm text-on-surface"
              disabled={busy}
              value={instruction}
              onChange={(event) => setInstruction(event.target.value)}
            />
            <div className="mt-2 flex justify-end">
              <button
                type="button"
                className="rounded-md bg-primary px-3 py-2 text-xs font-semibold text-on-primary disabled:cursor-not-allowed disabled:opacity-50"
                disabled={busy || instruction.trim().length === 0}
                onClick={() => onSave(rule, instruction.trim())}
              >
                {busy ? "반영 중…" : "수정 저장"}
              </button>
            </div>
            <p className="m-0 mt-2 text-[11px] text-on-surface-variant">
              기존 규칙을 일시중지하고 Control Room의 동일한 해석·검증 경로로 새 규칙을 활성화한 뒤 기존 규칙을 철회합니다.
            </p>
          </td>
        </tr>
      ) : null}
    </Fragment>
  );
}

export function ActiveOrdersPanel() {
  const queryClient = useQueryClient();
  const [editingRuleId, setEditingRuleId] = useState<string | null>(null);
  const [actionMessage, setActionMessage] = useState<string | null>(null);
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
  const cancelMutation = useMutation({
    mutationFn: cancelConditionalRule,
    onSuccess: async () => {
      setActionMessage("조건주문을 철회했습니다. 감사 이력은 보존됩니다.");
      await queryClient.invalidateQueries({ queryKey: ["conditional-rules"] });
    },
  });
  const editMutation = useMutation({
    mutationFn: async ({ instruction, rule }: { instruction: string; rule: ConditionalRuleView }) => {
      await pauseConditionalRule(rule.rule_id);
      let replacementActivated = false;
      try {
        const response = await askCeo(
          instruction,
          `rule-edit:${rule.rule_id}:${crypto.randomUUID()}`,
          rule.book_id,
          rule.fund_id,
        );
        if (!("order_request_id" in response) || !response.order_request_id) {
          throw new Error("수정 문장이 조건주문으로 접수되지 않았습니다.");
        }
        if (REPLACEMENT_FAILED_STATES.has(response.order_state ?? "")) {
          throw new Error(response.answer || "수정 조건주문 접수가 거절되었습니다.");
        }
        await waitForReplacement(response.order_request_id);
        replacementActivated = true;
        await cancelConditionalRule(rule.rule_id);
        return response;
      } catch (error) {
        // If the replacement is already ACTIVE, reviving the old rule would
        // create two executable instructions.  Leave the old one PAUSED so a
        // cleanup retry is safe.  Before activation, restore the original.
        if (!replacementActivated) {
          await resumeConditionalRule(rule.rule_id).catch(() => undefined);
        }
        throw error;
      }
    },
    onSuccess: async () => {
      setEditingRuleId(null);
      setActionMessage("수정된 조건주문을 활성화하고 기존 규칙을 철회했습니다.");
      await queryClient.invalidateQueries({ queryKey: ["conditional-rules"] });
    },
  });
  const busy = cancelMutation.isPending || editMutation.isPending;
  const actionError = cancelMutation.error || editMutation.error;

  function cancelRule(rule: ConditionalRuleView) {
    setActionMessage(null);
    if (!window.confirm(`${names.get(rule.spec.symbol) ?? rule.spec.symbol} 조건주문을 삭제할까요?`)) return;
    cancelMutation.mutate(rule.rule_id);
  }

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
        {actionMessage ? (
          <p role="status" className="mb-3 rounded-md border border-primary/30 bg-secondary-container px-3 py-2 text-sm text-primary">
            {actionMessage}
          </p>
        ) : null}
        {actionError ? (
          <p role="alert" className="mb-3 rounded-md border border-error/40 bg-error-container px-3 py-2 text-sm text-on-error-container">
            {actionError instanceof Error ? actionError.message : String(actionError)}
          </p>
        ) : null}
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
                  <th className="w-[32%] px-3 py-2.5 font-semibold">종목</th>
                  <th className="w-[50%] px-3 py-2.5 font-semibold">조건</th>
                  <th className="w-[18%] px-3 py-2.5 font-semibold">관리</th>
                </tr>
              </thead>
              <tbody>
                {activeRules.map((rule) => (
                  <ActiveRuleRow
                    key={rule.rule_id}
                    busy={busy}
                    editing={editingRuleId === rule.rule_id}
                    names={names}
                    onCancel={cancelRule}
                    onEdit={(selected) => {
                      setActionMessage(null);
                      setEditingRuleId((current) => current === selected.rule_id ? null : selected.rule_id);
                    }}
                    onSave={(selected, instruction) => editMutation.mutate({ instruction, rule: selected })}
                    rule={rule}
                  />
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
