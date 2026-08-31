import assert from "node:assert/strict";
import test from "node:test";

import {
  buildCeoProgress,
  parsePaperOrderWorkflowStatus,
} from "../app/lib/ceoClient.ts";

test("Hermes profile 이름으로 된 그래프 카드가 논리 부서 결과를 표시한다", () => {
  const progress = buildCeoProgress(
    {
      status: {
        task_id: "t_root",
        root_task_id: "t_root",
        status: "completed",
        progress: { primary_total: 2, primary_done: 2, qa: "blocked", synthesis: "done" },
      },
      graph: {
        root: "t_root",
        nodes: [
          { id: "t_root", department: "ceo-agent", status: "done", role: "root", title: "질의" },
          { id: "t_risk", department: "risk-management", status: "done", role: "primary", title: "리스크" },
          { id: "t_accounting", department: "accounting-portfolio-department", status: "done", role: "primary", title: "회계" },
          { id: "t_qa", department: "qa-department", status: "blocked", role: "qa", title: "QA" },
          { id: "t_synthesis", department: "ceo-agent", status: "done", role: "synthesis", title: "종합" },
        ],
        edges: [],
      },
    },
    {
      status: "completed",
      result: { summary: "CEO 최종 종합" },
      departments: {
        risk: "리스크 검토 결과",
        accounting: "회계 검토 결과",
        qa: "QA 검토 결과",
      },
      qa_verdict: "FAIL_BLOCKED_FOR_DECISION",
      block_reason: "추가 근거 필요",
    },
  );

  assert.deepEqual(
    Object.fromEntries(progress.cards.map((card) => [card.task_id, [card.outcome, card.summary]])),
    {
      t_root: ["QUEUED", "CEO 최종 종합"],
      t_risk: ["ANSWERED", "리스크 검토 결과"],
      t_accounting: ["ANSWERED", "회계 검토 결과"],
      t_qa: ["BLOCKED", "QA 검토 결과"],
      t_synthesis: ["ANSWERED", "CEO 최종 종합"],
    },
  );
});

function orderStatus(overrides = {}) {
  return {
    schema_version: "user-paper-order-status.v1",
    order_request_id: "4ff413cd-d80a-4ff4-b268-9d11c8bee5ee",
    mode: "PAPER",
    state: "CLARIFICATION_REQUIRED",
    action: null,
    ceo_root_task_id: "t_root",
    trading_task_id: "t_trading",
    clarification_code: "EVIDENCE_TEXT_MISMATCH",
    error_code: null,
    error_message: null,
    directive: null,
    conditional_rules: null,
    ...overrides,
  };
}

test("주문 상태 응답의 형태를 실제로 검증한다", () => {
  const parsed = parsePaperOrderWorkflowStatus(orderStatus());
  assert.equal(parsed.state, "CLARIFICATION_REQUIRED");
  assert.equal(parsed.clarification_code, "EVIDENCE_TEXT_MISMATCH");
  assert.equal(parsed.directive, null);
  assert.equal(parsed.conditional_rules, null);
});

test("BFF가 계약을 벗어난 응답을 주면 조용히 렌더링하지 않는다", () => {
  // getJson이 `body as T` 캐스팅뿐이라 이런 응답이 그대로 화면까지 갔다.
  for (const broken of [
    null,
    [],
    orderStatus({ schema_version: "user-paper-order-status.v2" }),
    orderStatus({ mode: "LIVE" }),
    orderStatus({ order_request_id: 12345 }),
    orderStatus({ state: null }),
    orderStatus({ clarification_code: { code: "X" } }),
    orderStatus({ directive: { directive_id: "d-1" } }),
    orderStatus({ conditional_rules: "none" }),
  ]) {
    assert.throws(
      () => parsePaperOrderWorkflowStatus(broken),
      /paper_order_status_invalid_response/,
    );
  }
});

test("백엔드가 새 상태나 action을 추가해도 주문 결말 화면을 막지 않는다", () => {
  // 값 목록 드리프트는 CI 계약 테스트가 잡는다. 런타임에서 화면을 통째로
  // 못 쓰게 만드는 편이 더 나쁘다.
  const parsed = parsePaperOrderWorkflowStatus(
    orderStatus({ state: "SOME_NEW_STATE", action: "PLACE_BASKET" }),
  );
  assert.equal(parsed.state, "SOME_NEW_STATE");
  assert.equal(parsed.action, "PLACE_BASKET");
});

test("directive와 conditional_rules를 중첩까지 검증한다", () => {
  const parsed = parsePaperOrderWorkflowStatus(
    orderStatus({
      state: "COMPLETED",
      action: "SELL_ALL",
      directive: {
        directive_id: "d-1",
        state: "FILLED",
        mode: "PAPER",
        error_code: null,
        error_message: null,
      },
      conditional_rules: [
        {
          rule_id: "r-1",
          state: "FAILED",
          last_execution_state: "REJECTED",
          last_guard_code: "MARKET_CLOSED",
          last_error_code: null,
          status_message: null,
        },
      ],
    }),
  );
  assert.equal(parsed.directive?.state, "FILLED");
  assert.equal(parsed.conditional_rules?.[0].last_guard_code, "MARKET_CLOSED");
});
