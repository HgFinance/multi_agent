import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

async function render() {
  const workerUrl = new URL("../dist/server/index.js", import.meta.url);
  workerUrl.searchParams.set("test", `${process.pid}-${Date.now()}`);
  const { default: worker } = await import(workerUrl.href);
  return worker.fetch(
    new Request("http://localhost/", {
      headers: { accept: "text/html" },
    }),
    {
      ASSETS: {
        fetch: async () => new Response("Not found", { status: 404 }),
      },
    },
    {
      waitUntil() {},
      passThroughOnException() {},
    },
  );
}

test("server-renders the HgFinance organization projection", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);
  const html = await response.text();

  assert.match(html, /<html lang="ko">/i);
  assert.match(html, /<title>HgFinance - AI 헤지펀드 오피스<\/title>/i);
  for (const label of [
    "리서치본부",
    "퀀트·백테스트본부",
    "트레이딩본부",
    "리스크관리본부",
    "회계·포트폴리오본부",
    "AI QA·감사본부",
    "Agent Workforce 인사팀",
    "CEO Office 지원",
  ]) {
    assert.match(html, new RegExp(label));
  }
  assert.doesNotMatch(html, /Your site taking shape|Building your site/);
});

test("keeps the current organization and Risk/QA bridge wired", async () => {
  const [config, page, layout, staff, world, packageJson, riskQaBridge, sim] = await Promise.all([
    readFile(new URL("../company.config.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/game/staff.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/game/world.ts", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
    readFile(new URL("../app/ops/riskQaBridge.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/game/sim.ts", import.meta.url), "utf8"),
  ]);

  const departmentBlock = config.match(/export const DEPARTMENTS = \[(?<departments>[\s\S]*?)\] as const/);
  assert.ok(departmentBlock?.groups?.departments);
  assert.equal(
    [...departmentBlock.groups.departments.matchAll(/^\s+id: "[^"]+",/gm)].length,
    8,
  );

  assert.match(config, /name: "HgFinance"/);
  assert.match(config, /pageTitle: "HgFinance - AI 헤지펀드 오피스"/);
  assert.match(config, /staff\("ops", "member"/);
  assert.equal((config.match(/staff\("ops", "member"/g) ?? []).length, 4);
  assert.equal((config.match(/staff\("qa", "member"/g) ?? []).length, 5);
  assert.match(config, /executive-briefing-worker/);
  // sim.ts의 debate()가 role 문자열로 두 사람을 찾는다. 이름이 바뀌면 토론이 조용히 사라진다.
  for (const role of ["Bull 리서처", "Bear 리서처"]) {
    assert.ok(config.includes(`"${role}"`), `${role} 누락 — debate()가 no-op이 된다`);
    assert.ok(sim.includes(`=== "${role}"`), `sim.ts가 찾는 role 문자열과 불일치`);
  }
  assert.match(page, /<OfficeWorld/);
  assert.match(page, /<OpsPanel/);
  assert.match(page, /<RiskQaPanel/);
  assert.match(page, /<DepartmentCommunicationPanel/);
  assert.match(page, /<BffProvider>/);
  assert.match(layout, /title: COMPANY\.pageTitle/);
  assert.match(layout, /<html lang="ko">/);
  assert.match(staff, /STAFF_LIST\.map/);
  assert.match(world, /DEPARTMENTS\.map/);
  assert.match(world, /FLOORS = \[1, 2\] as const/);
  assert.match(packageJson, /"name": "hgfinance-ai-office"/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);

  assert.match(riskQaBridge, /market-liquidity-worker/);
  assert.match(riskQaBridge, /incident-postmortem-worker/);
  assert.doesNotMatch(riskQaBridge, /RSK-00|QAA-07/);
  assert.match(riskQaBridge, /headModel: "gpt-5\.6-luna"/);
  assert.match(riskQaBridge, /workerModel: "qwen3:1\.7b"/);
  assert.match(riskQaBridge, /orchestrator: "Hermes"/);
  assert.match(riskQaBridge, /employeeExecutor: "LangGraph"/);
  assert.match(riskQaBridge, /InputSnapshot/);
});

test("keeps one portfolio analysis entry point", async () => {
  const [page, panel, client] = await Promise.all([
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/ops/PortfolioInterviewPanel.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/ops/portfolioClient.ts", import.meta.url), "utf8"),
  ]);

  assert.equal((page.match(/사용자 입력으로 분석 시작/g) ?? []).length, 1);
  assert.match(page, /portfolio-interview-form/);
  assert.match(page, /requestSubmit\(\)/);
  assert.match(page, /a\.status === "업무 중"/);
  assert.doesNotMatch(panel, /LangGraph 분석 시작/);
  assert.doesNotMatch(panel, /현금화 필요/);
  assert.match(panel, /분석 카테고리/);
  assert.match(panel, /CEO TASK ROUTING/);
  assert.match(panel, /className="ticker"/);
  assert.doesNotMatch(client, /liquidity_need/);
});

test("routes integration readiness through the operator BFF", async () => {
  const report = await readFile(new URL("../app/game/report.ts", import.meta.url), "utf8");
  assert.match(report, /\/ui\/integrations/);
  assert.doesNotMatch(report, /fetch\("\/api\/integrations"\)/);
});
