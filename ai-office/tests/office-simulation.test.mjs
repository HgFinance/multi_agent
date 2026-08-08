import assert from "node:assert/strict";
import test from "node:test";

import { Company } from "../app/game/sim.ts";

test("DEMO test harness starts at 09:00 and completes every office department", () => {
  const company = new Company();
  company.setSimulationMode(true);
  company.setTestMode(true);
  company.start();

  assert.equal(company.snapshot().clock, "09:00");

  const phases = new Set();
  let ticks = 0;
  while (company.snapshot().running && ticks < 6000) {
    company.tick(0.25);
    phases.add(company.snapshot().phase);
    ticks += 1;
  }

  const snapshot = company.snapshot();
  assert.ok(ticks < 6000, "the test office run did not converge");
  assert.equal(snapshot.dayComplete, true);
  assert.equal(snapshot.running, false);
  assert.equal(snapshot.clock, "18:00");
  assert.equal(snapshot.progress, 100);
  assert.deepEqual(
    Object.fromEntries(
      Object.entries(snapshot.deptStatus).filter(([department]) => department !== "secretary"),
    ),
    {
      research: "완료",
      strategy1: "완료",
      strategy2: "완료",
      ops: "완료",
      finance: "완료",
      qa: "완료",
      review: "완료",
    },
  );
  assert.ok(phases.has("09:00 전사 출근"));
  assert.ok(phases.has("업무 종료"));
});
