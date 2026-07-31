import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

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

test("server-renders the HgFinance eight-organization office", async () => {
  const response = await render();
  assert.equal(response.status, 200);
  assert.match(response.headers.get("content-type") ?? "", /^text\/html\b/i);

  const html = await response.text();
  assert.match(html, /<html lang="ko">/i);
  assert.match(html, /<title>HgFinance - AI 헤지펀드 오피스<\/title>/i);
  assert.match(html, /개인형 헤지펀드/);
  assert.match(html, /리서치본부/);
  assert.match(html, /퀀트·백테스트본부/);
  assert.match(html, /트레이딩본부/);
  assert.match(html, /리스크본부/);
  assert.match(html, /회계·포트폴리오본부/);
  assert.match(html, /AI QA·감사본부/);
  assert.match(html, /Agent Workforce 인사팀/);
  assert.match(html, /CEO Office 지원팀/);
  assert.doesNotMatch(html, /Your site is taking shape|Building your site/);
});

test("keeps organization configuration wired to the live office", async () => {
  const [config, page, layout, staff, world, packageJson] = await Promise.all([
    readFile(new URL("../company.config.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/page.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/layout.tsx", import.meta.url), "utf8"),
    readFile(new URL("../app/game/staff.ts", import.meta.url), "utf8"),
    readFile(new URL("../app/game/world.ts", import.meta.url), "utf8"),
    readFile(new URL("../package.json", import.meta.url), "utf8"),
  ]);

  const departmentBlock = config.match(
    /export const DEPARTMENTS = \[(?<departments>[\s\S]*?)\] as const;/,
  );
  assert.ok(departmentBlock?.groups?.departments);
  assert.equal(
    [...departmentBlock.groups.departments.matchAll(/^\s+id:\s*"[^"]+",/gm)].length,
    8,
  );

  assert.match(config, /name:\s*"HgFinance"/);
  assert.match(config, /pageTitle:\s*"HgFinance - AI 헤지펀드 오피스"/);
  assert.match(page, /new Company\(\)/);
  assert.match(page, /<OfficeWorld/);
  assert.match(page, /<OpsPanel/);
  assert.match(layout, /title:\s*COMPANY\.pageTitle/);
  assert.match(layout, /<html lang="ko">/);
  assert.match(staff, /STAFF_LIST\.map/);
  assert.match(world, /DEPARTMENTS\.map/);
  assert.match(world, /FLOORS = \[1, 2\] as const/);
  assert.match(packageJson, /"name": "hgfinance-ai-office"/);
  assert.doesNotMatch(packageJson, /react-loading-skeleton/);
});
