import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const inspector = readFileSync(resolve(root, "app/agent-logs/DepartmentInspector.tsx"), "utf8");
const panel = readFileSync(resolve(root, "app/components/AccountingLedgerPanel.tsx"), "utf8");

test("회계 상세는 장부의 확정·결제 상태를 사용자에게 보여준다", () => {
  assert.match(inspector, /GroupHeading index=\{1\} title="결산·원장 상태"/);
  assert.match(inspector, /<AccountingLedgerPanel \/>/);
  assert.match(panel, /이번 기간 결산 상태/);
  assert.match(panel, /AccountingCloseStatus/);
  assert.match(panel, /data\.totals\.unsettled_count/);
  assert.match(panel, /결제 진행률/);
  assert.match(panel, /손익 기준/);
  assert.match(panel, /결제가 끝나지 않은 거래가/);
});
