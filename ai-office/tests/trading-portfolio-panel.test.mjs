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
  // 출처 열은 2026-09-02에 화면에서 제거했다. 뱃지 문구가 아니라 열이
  // 다시 살아나는 것을 막는다.
  assert.doesNotMatch(panel, /출처/);
  assert.doesNotMatch(panel, /getEventOrigin/);
  // 거래 신호가 오면 폴링 주기를 기다리지 않고 즉시 다시 읽는다.
  assert.match(panel, /subscribePortfolioLiveRevision/);
  assert.match(panel, /invalidateQueries/);
  assert.match(panel, /LS PAPER 계좌 기준/);
  assert.match(panel, /실시간 주문 알림을 다시 연결하는 중입니다/);
  assert.doesNotMatch(panel, /연결 오류: \{data\.stream\.error\}/);
});
