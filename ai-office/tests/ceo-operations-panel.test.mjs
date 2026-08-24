import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";

const root = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const inspector = readFileSync(resolve(root, "app/agent-logs/DepartmentInspector.tsx"), "utf8");
const panel = readFileSync(resolve(root, "app/components/CeoOperationsPanel.tsx"), "utf8");

test("CEO 상세는 경고와 최근 판단을 제공한다", () => {
  assert.match(inspector, /GroupHeading index=\{1\} title="지시·결과 흐름"/);
  assert.match(inspector, /<CeoOperationsPanel data=\{data\} \/>/);

  assert.doesNotMatch(panel, /지금 시스템은 이렇게 움직이고 있습니다/);
  assert.match(panel, /주의할 점과 최근 판단/);
  assert.match(panel, /data\.warnings/);
  assert.match(panel, /data\.messages/);
  assert.match(panel, /최근에 내려진 판단/);
});
