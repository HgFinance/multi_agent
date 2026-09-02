import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

import {
  cancelConditionalRule,
  fetchConditionalRules,
  pauseConditionalRule,
  resumeConditionalRule,
} from "../app/lib/conditionalRuleClient.ts";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const panel = readFileSync(resolve(root, "app/dashboard/ActiveOrdersPanel.tsx"), "utf8");

function rule(overrides = {}) {
  return {
    rule_id: "11111111-1111-4111-8111-111111111111",
    fund_id: "22222222-2222-4222-8222-222222222222",
    book_id: "33333333-3333-4333-8333-333333333333",
    raw_instruction: "삼성전자 현재가가 7만원 이상이면 1주 시장가 매수",
    state: "ACTIVE",
    rule_version: 1,
    spec_sha256: "a".repeat(64),
    confirmed_at: "2026-09-02T00:00:00Z",
    created_at: "2026-09-02T00:00:00Z",
    updated_at: "2026-09-02T00:00:00Z",
    spec: {
      symbol: "005930",
      condition: { type: "COMPARISON" },
      action: {
        side: "BUY",
        sizing: { type: "FIXED_SHARES", value: "1" },
        order_type: "MARKET",
      },
      expires_at: "2026-09-02T06:30:00Z",
    },
    last_execution_state: null,
    last_guard_code: null,
    last_error_code: null,
    directive_id: null,
    status_message: null,
    ...overrides,
  };
}

test("대기 주문 전이는 인증 BFF의 pause/resume/delete 경로만 사용한다", async () => {
  const seen = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    seen.push({ url: String(url), method: init?.method });
    return Response.json(rule());
  };
  try {
    await pauseConditionalRule("rule/one");
    await resumeConditionalRule("rule/one");
    await cancelConditionalRule("rule/one");
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.deepEqual(seen, [
    { url: "http://127.0.0.1:8001/ui/conditional-rules/rule%2Fone/pause", method: "POST" },
    { url: "http://127.0.0.1:8001/ui/conditional-rules/rule%2Fone/resume", method: "POST" },
    { url: "http://127.0.0.1:8001/ui/conditional-rules/rule%2Fone", method: "DELETE" },
  ]);
});

test("원문·fund·book 없는 대기 주문 응답은 편집 가능한 주문으로 표시하지 않는다", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => Response.json([rule({ raw_instruction: undefined })]);
  try {
    await assert.rejects(fetchConditionalRules(), /조건주문 응답 계약/);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("대기 주문 화면은 원문 편집과 감사 이력을 보존하는 삭제를 제공한다", () => {
  assert.match(panel, />\s*수정\s*</);
  assert.match(panel, />\s*삭제\s*</);
  assert.match(panel, /return rule\.raw_instruction/);
  assert.match(panel, /await pauseConditionalRule\(rule\.rule_id\)/);
  assert.match(panel, /await waitForReplacement\(response\.order_request_id\)/);
  assert.match(panel, /await cancelConditionalRule\(rule\.rule_id\)/);
  assert.match(panel, /if \(!replacementActivated\)/);
});
