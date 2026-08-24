import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const inspector = await readFile(new URL("../app/agent-logs/DepartmentInspector.tsx", import.meta.url), "utf8");
const panel = await readFile(new URL("../app/components/RiskMandatePanel.tsx", import.meta.url), "utf8");

test("리스크 본부 상세는 Risk 전용 Mandate 설명 패널을 렌더링한다", () => {
  assert.match(inspector, /RiskMandatePanel/);
  assert.match(inspector, /GroupHeading index=\{1\} title="사용자 Mandate와 주문 전 확인"/);
  assert.match(panel, /risk\.mandate_guardrails/);
  assert.doesNotMatch(panel, /text-\[(?:10|11)px\]/);
  assert.doesNotMatch(panel, /결정론적 Pre-trade Risk Gate → Trading/);
  assert.match(panel, /현재 적용 중인 Mandate 한도/);
  assert.match(panel, /주문 요청이 들어오면 이렇게 확인합니다/);
  assert.match(panel, /Mandate와 현재 계좌 상태/);
});

test("Mandate snapshot이 없을 때 위험 한도를 임의로 만들지 않는다", () => {
  assert.match(panel, /현재 Fund의 저장된 Mandate가 없습니다/);
  assert.match(panel, /정책 snapshot이 완전하지 않습니다/);
  assert.match(panel, /실제 판정은 서버 Risk Gate가 담당합니다/);
});
