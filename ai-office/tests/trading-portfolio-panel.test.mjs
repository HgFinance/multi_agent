import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const inspector = readFileSync(resolve(root, "app/agent-logs/DepartmentInspector.tsx"), "utf8");
const panel = readFileSync(resolve(root, "app/components/LivePortfolioPanel.tsx"), "utf8");

test("트레이딩 상세는 오늘 거래 흐름을 사용자 관점으로 보여준다", () => {
  assert.match(inspector, /GroupHeading index=\{1\} title="오늘 거래와 포트폴리오"/);
  assert.match(inspector, /<LivePortfolioPanel \/>/);
  assert.match(panel, /오늘 거래 요약/);
  assert.match(panel, /today_activity/);
  assert.match(panel, /매수 금액/);
  assert.match(panel, /매도 금액/);
  assert.match(panel, /총 거래금액/);
  assert.match(panel, /수수료/);
  assert.match(panel, /세금/);
  assert.match(panel, /조건주문/);
  assert.match(panel, /출처 미확인/);
  assert.match(panel, /LS PAPER 계좌 기준/);
});
