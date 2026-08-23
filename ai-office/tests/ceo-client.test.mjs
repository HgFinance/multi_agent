import assert from "node:assert/strict";
import test from "node:test";

import { buildCeoProgress } from "../app/lib/ceoClient.ts";

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
