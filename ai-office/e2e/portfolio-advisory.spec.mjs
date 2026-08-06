import { test, expect } from "@playwright/test";

// These routes model only the BFF contracts used by the browser. No broker, OMS, or
// ledger endpoint is ever registered here, so a passing test cannot perform a write.
function makeRecommendationResult(overrides = {}) {
  return {
    pipeline_status: "COMPLETED",
    workflow: "portfolio-recommendation-full",
    trace_id: "trace-safe-fixture",
    safe_action: "HOLD",
    binding: false,
    production_enabled: false,
    external_writes: false,
    pipeline_version: "portfolio-advisory.v1",
    manual_review_required: true,
    data_context: {
      quality_status: "PASS",
      reasons: [],
      pit_readiness: {
        quality_status: "PASS",
        reasons: [],
        candidate_count: 1,
        research_document_count: 1,
        market_snapshot_count: 1,
        domestic_instrument_count: 1,
      },
    },
    risk_gate: { verdict: "HOLD", reason: "Risk 검토와 명시적 승인이 필요합니다." },
    qa_gate: { decision: "HOLD", reason: "QA 확인 전에는 안전 보류합니다." },
    department_reports: {},
    worker_reports: [],
    pipeline_events: [],
    ...overrides,
  };
}

function makeSnapshot(runtimeOverrides = {}) {
  return {
    schema_version: 1,
    mode: "DEMO",
    snapshot_version: 1,
    server_time: "2026-08-05T00:00:00Z",
    fund_id: "demo-fund",
    book_id: "demo-book",
    portfolio: {
      as_of: "2026-08-05T00:00:00Z",
      nav: "0",
      cash: "0",
      securities_value: "0",
      gross_exposure: "0",
      net_exposure: "0",
      realized_pnl: "0",
      unrealized_pnl: "0",
      fees: "0",
      taxes: "0",
      positions: [],
    },
    trading: { intents: [], orders: [], blocked_by_unknown: false },
    ledger: { journal_count: 0, reversal_count: 0, trial_balance_sum: "0", balanced: true, accounts: {} },
    operations: {
      schema_version: "operator-operations.v1",
      observed_at: "2026-08-05T00:00:00Z",
      sequence: 1,
      status: "CONNECTED",
      runtime_connected: true,
      event_bridge_connected: true,
      message_count: 0,
      implemented_event_contracts: 0,
      planned_event_contracts: 0,
      departments: [],
      communications: [],
      warnings: [],
      runtime: {
        status: "IDLE",
        run_id: null,
        workflow: null,
        phase: null,
        departments: {},
        active_workers: [],
        active_handoff: null,
        messages: [],
        result: null,
        approval: null,
        error: null,
        ...runtimeOverrides,
      },
    },
  };
}

function makeRunStatus(runId, status, result = null) {
  return {
    run_id: runId,
    workflow: "portfolio-recommendation-full",
    status,
    phase: status === "COMPLETED" ? "QA" : "PIT",
    updated_at: "2026-08-05T00:00:01Z",
    profile_user_id: "web-user",
    trace_id: "trace-success-fixture",
    case_id: "case-fixture",
    mandate_version_id: "mandate-version-uuid",
    policy_hash: "sha256:fixture-policy",
    input_hash: "sha256:fixture-input",
    result,
    error: null,
  };
}

async function installBffFixture(page, config = {}) {
  const state = {
    snapshot: config.snapshot ?? makeSnapshot(),
    snapshotRequests: 0,
    statusRequests: [],
    idempotencyKeys: [],
    startPayload: null,
  };
  await page.addInitScript(() => {
    window.__fixtureSockets = [];
    class FixtureWebSocket {
      static OPEN = 1;
      readyState = 0;
      onopen = null;
      onclose = null;
      onerror = null;
      onmessage = null;

      constructor() {
        window.__fixtureSockets.push(this);
        queueMicrotask(() => {
          this.readyState = FixtureWebSocket.OPEN;
          this.onopen?.(new Event("open"));
        });
      }

      close() {
        this.readyState = 3;
        this.onclose?.(new Event("close"));
      }

      send() {}
    }
    window.WebSocket = FixtureWebSocket;
  });
  await page.route("http://127.0.0.1:8001/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const { pathname } = url;
    const json = (status, body) => route.fulfill({
      status,
      contentType: "application/json",
      body: JSON.stringify(body),
    });

    if (pathname === "/ui/snapshot" && request.method() === "GET") {
      state.snapshotRequests += 1;
      return json(200, state.snapshot);
    }
    if (pathname === "/ui/integrations" && request.method() === "GET") {
      return json(200, {});
    }
    if (pathname === "/ui/portfolio-universes" && request.method() === "GET") {
      return json(200, {
        default_universe_id: "KOREA_EQUITY_WATCHLIST",
        universes: [{
          universe_id: "KOREA_EQUITY_WATCHLIST",
          name: "국내 주식 Watchlist",
          description: "deterministic fixture",
          status: "ACTIVE",
          source: "fixture",
          instrument_count: 1,
        }],
      });
    }
    if (/^\/ui\/mandates\/[^/]+\/current$/.test(pathname) && request.method() === "GET") {
      return json(200, { mandate_id: "web-mandate", current_version: 6, status: "ACTIVE" });
    }
    if (/^\/ui\/mandates\/[^/]+\/change-requests$/.test(pathname) && request.method() === "POST") {
      if (config.governanceResponse) {
        return json(config.governanceResponse.status, config.governanceResponse.body);
      }
      return json(200, {
        stage: "FAST_APPLIED",
        mandate_id: "web-mandate",
        version: 7,
        direction: "ACTIVATE",
        case_id: "case-fixture",
        detail: "Mandate가 안전하게 활성화되었습니다.",
      });
    }
    if (/^\/ui\/mandate-cases\/[^/]+\/timeline$/.test(pathname) && request.method() === "GET") {
      return json(200, {
        events: [{
          sequence: 1,
          event_type: "mandate.activated.v1",
          to_status: "ACTIVATED",
          payload: { mandate_version_id: "mandate-version-uuid", policy_hash: "sha256:fixture-policy" },
          occurred_at: "2026-08-05T00:00:00Z",
        }],
      });
    }
    if (pathname === "/ui/mandate-approvals" && request.method() === "GET") {
      return json(200, { approvals: [] });
    }
    if (pathname === "/ui/portfolio-recommendations" && request.method() === "POST") {
      const idempotencyKey = request.headers()["idempotency-key"];
      if (idempotencyKey) state.idempotencyKeys.push(idempotencyKey);
      config.onStart?.(state, request);
      if (config.startResponse) return json(config.startResponse.status, config.startResponse.body);
      return json(202, { run_id: "run-fixture", status: "QUEUED", trace_id: idempotencyKey ?? "trace-fixture" });
    }
    if (/^\/ui\/portfolio-recommendations\/[^/]+$/.test(pathname) && request.method() === "GET") {
      const runId = pathname.split("/").at(-1);
      state.statusRequests.push(runId);
      const responses = config.statusResponses ?? [makeRunStatus(runId, "COMPLETED", makeRecommendationResult())];
      const response = responses[Math.min(state.statusRequests.length - 1, responses.length - 1)];
      return json(200, { ...response, run_id: runId });
    }
    return json(404, { detail: "fixture route missing" });
  });

  return state;
}

async function openMandate(page, state) {
  await page.goto("/");
  await page.getByRole("button", { name: "Mandate 설정" }).click();
  await expect(page.locator("#portfolio-interview")).toContainText("Mandate Configuration");
  await expect.poll(() => state.snapshotRequests).toBeGreaterThan(0);
  await expect(page.locator("#portfolio-interview .status-pill").first()).toContainText("대기");
}

async function submitMandate(page) {
  await page.getByRole("button", { name: "Mandate 제출하고 검토 시작" }).click();
}

test.describe.configure({ mode: "serial" });

test("renders the advisory control plane in a fail-closed offline state", async ({ page }) => {
  await page.route("http://127.0.0.1:8001/**", (route) => route.abort("failed"));
  await page.goto("/");

  await expect(page).toHaveTitle("HgFinance - AI 헤지펀드 오피스");
  await page.getByRole("button", { name: "Mandate 설정" }).click();
  await expect(page.locator("#portfolio-interview")).toContainText("Mandate Configuration");
  await expect(page.locator("#portfolio-interview")).toContainText("USER INPUT → CEO ROUTER → RISK / QA GATE");
  await expect(page.locator("#portfolio-interview")).toContainText("Mandate 제출하고 검토 시작");
});

test("keeps the browser surface advisory-only", async ({ page }) => {
  await page.route("http://127.0.0.1:8001/**", (route) => route.abort("failed"));
  await page.goto("/");

  await page.getByRole("button", { name: "Mandate 설정" }).click();
  await expect(page.locator("#portfolio-interview")).toContainText("Mandate Configuration");
  await expect(page.locator("#portfolio-interview")).not.toContainText("주문 전송");
  await expect(page.locator("#portfolio-interview")).not.toContainText("Ledger Posting");
});

test("fails closed when the BFF returns an invalid snapshot contract", async ({ page }) => {
  await page.route("http://127.0.0.1:8001/ui/snapshot", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ schema_version: "invalid", mode: "DEMO" }),
  }));
  await page.goto("/");
  await page.getByRole("button", { name: "📊 대시보드" }).click();
  await page.getByRole("button", { name: "Operations Console" }).click();
  await expect(page.getByRole("heading", { name: "실행의 모든 흔적을" })).toBeVisible();
  await expect(page.getByText("미연결", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("RUNTIME CONNECTED", { exact: true })).toHaveCount(0);
});

test("shows a typed 422 start failure without leaving an executable action", async ({ page }) => {
  const state = await installBffFixture(page, {
    startResponse: {
      status: 422,
      body: { detail: "portfolio_input_invalid", error_code: "portfolio_input_invalid" },
    },
  });
  await openMandate(page, state);
  await submitMandate(page);

  await expect(page.getByRole("alert")).toContainText("portfolio_input_invalid");
  await expect(page.locator("#portfolio-interview")).toContainText("설정 완료");
  await expect(page.getByRole("button", { name: "주문 전송" })).toHaveCount(0);
  await expect(page.getByText("Ledger Posting", { exact: true })).toHaveCount(0);
});

test("shows the Governance unavailable 503 as a fail-closed advisory error", async ({ page }) => {
  const state = await installBffFixture(page, {
    governanceResponse: {
      status: 503,
      body: { detail: "governance_api_unavailable", error_code: "governance_api_unavailable" },
    },
  });
  await openMandate(page, state);
  await submitMandate(page);

  await expect(page.getByRole("alert")).toContainText("CEO Governance API에 연결할 수 없습니다.");
  await expect(page.getByRole("button", { name: "주문 전송" })).toHaveCount(0);
  await expect(page.getByText("Ledger Posting", { exact: true })).toHaveCount(0);
});

test("shows a 409 duplicate/idempotency conflict and keeps the flow advisory-only", async ({ page }) => {
  const state = await installBffFixture(page, {
    startResponse: {
      status: 409,
      body: { detail: "idempotency_conflict", error_code: "idempotency_conflict" },
    },
  });
  await openMandate(page, state);
  await submitMandate(page);

  await expect(page.getByRole("alert")).toContainText("idempotency_conflict");
  await expect.poll(() => state.idempotencyKeys.length).toBe(1);
  expect(state.idempotencyKeys[0]).toBeTruthy();
  await expect(page.getByRole("button", { name: "주문 전송" })).toHaveCount(0);
  await expect(page.getByText("Ledger Posting", { exact: true })).toHaveCount(0);
});

test("polls a successful start by run id and renders its advisory result", async ({ page }) => {
  const runId = "run-success-123";
  const result = makeRecommendationResult({
    safe_action: "REVIEW",
    suitability: {
      recommendations: [{
        portfolio_id: "domestic-safe",
        name: "국내 안정형",
        risk_band: "LOW",
        fit_score: 88,
        target_allocations: { "005930": "0.20" },
        target_amounts: { "005930": "20000000" },
        reasons: ["안전한 자문 후보"],
        evidence_refs: ["fixture:evidence"],
      }],
    },
    instrument_recommendations_status: "COMPLETE",
    instrument_recommendations: [{
      portfolio_id: "domestic-safe",
      symbol: "005930",
      exchange: "XKRX",
      name: "삼성전자",
      asset_class: "KOREA_EQUITY",
      target_weight: "0.20",
      target_amount: "20000000",
      expected_return: null,
      data_status: "PIT",
    }],
  });
  const state = await installBffFixture(page, {
    startResponse: { status: 202, body: { run_id: runId, status: "RUNNING", trace_id: "trace-success-fixture" } },
    statusResponses: [makeRunStatus(runId, "RUNNING"), makeRunStatus(runId, "COMPLETED", result)],
    onStart: (fixtureState, request) => {
      try {
        const rawBody = request.postData?.() ?? "";
        fixtureState.startPayload = rawBody ? JSON.parse(rawBody) : {};
      } catch {
        fixtureState.startPayload = {};
      }
      fixtureState.snapshot = makeSnapshot({
        status: "COMPLETED",
        run_id: runId,
        workflow: "portfolio-recommendation-full",
        phase: "QA",
        result,
        approval: { status: "REJECT", binding: false, approved_at: null, comment: "자문 전용" },
      });
    },
  });
  await openMandate(page, state);
  // The Dashboard is rendered after start. Its read model carries the same run
  // and result so that the test observes the result even after the panel unmounts.
  await submitMandate(page);

  await expect.poll(() => state.startPayload?.mandate_id).toBe("web-mandate");
  await expect.poll(() => state.startPayload?.mandate_version_id).toBe("mandate-version-uuid");
  await expect.poll(() => state.statusRequests.length).toBeGreaterThan(0);
  expect(state.statusRequests.every((requestedRunId) => requestedRunId === runId)).toBeTruthy();
  await expect(page.getByText(`run ${runId}`, { exact: true })).toBeVisible();
  await expect(page.getByText("국내 안정형", { exact: true })).toBeVisible();
  await expect(page.getByText("실제 전송·게시·결제는 대표 승인 후 진행해요", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "주문 전송" })).toHaveCount(0);
  await expect(page.getByText("Ledger Posting", { exact: true })).toHaveCount(0);
});
test("recovers a WebSocket reconnect and sequence gap without trusting stale REST data", async ({ page }) => {
  const state = await installBffFixture(page);
  await page.goto("/");
  await expect.poll(() => state.snapshotRequests).toBeGreaterThan(0);
  await page.getByRole("button", { name: "📊 대시보드" }).click();
  await page.getByRole("button", { name: "Operations Console" }).click();

  await page.evaluate(() => window.__fixtureSockets[0]?.close());
  await expect.poll(() => page.evaluate(() => window.__fixtureSockets.length)).toBeGreaterThan(1);
  await page.waitForTimeout(50);
  const snapshotsBeforeGap = state.snapshotRequests;
  await page.evaluate(() => {
    window.__fixtureSockets.at(-1)?.onmessage?.({
      data: JSON.stringify({ event_type: "agent.status.v1", sequence: 5 }),
    });
  });
  await expect.poll(() => state.snapshotRequests).toBeGreaterThan(snapshotsBeforeGap);
  await expect(page.getByText("sequence 1", { exact: true })).toBeVisible();
  expect(state.statusRequests).toHaveLength(0);
  await page.screenshot({ path: "test-results/portfolio-advisory-recovery.png", fullPage: true });
});

test("shows terminal PIT, Risk, and QA safe states without order or ledger actions", async ({ page }) => {
  const terminalResult = makeRecommendationResult({
    pipeline_status: "DEGRADED",
    safe_action: "HOLD",
    manual_review_required: true,
    data_context: {
      quality_status: "FAIL",
      reasons: ["NO_PIT_DOMESTIC_EQUITY_INSTRUMENTS", "NO_PIT_MARKET_SNAPSHOTS"],
      pit_readiness: {
        quality_status: "FAIL",
        reasons: ["NO_PIT_DOMESTIC_EQUITY_INSTRUMENTS", "NO_PIT_MARKET_SNAPSHOTS"],
        candidate_count: 0,
        research_document_count: 0,
        market_snapshot_count: 0,
        domestic_instrument_count: 0,
      },
    },
    risk_gate: { verdict: "HOLD", reason: "PIT 데이터 부족으로 Risk 안전 보류" },
    qa_gate: { decision: "FAIL", reason: "QA는 입력 계약 확인 전까지 차단" },
  });
  const state = await installBffFixture(page, {
    snapshot: makeSnapshot({
      status: "DEGRADED",
      run_id: "run-terminal-safe",
      workflow: "portfolio-recommendation-full",
      phase: "QA",
      result: terminalResult,
      approval: { status: "PENDING", binding: false, approved_at: null, comment: "수동 검토 필요" },
      error: "PIT 입력이 준비되지 않아 실행을 보류했습니다.",
    }),
  });
  await page.goto("/");
  await expect.poll(() => state.snapshotRequests).toBeGreaterThan(0);
  await page.getByRole("button", { name: "📊 대시보드" }).click();
  await page.getByRole("button", { name: "Operations Console" }).click();

  await expect(page.getByRole("heading", { name: "실행의 모든 흔적을" })).toBeVisible();
  await expect(page.getByText("안전 보류", { exact: true }).first()).toBeVisible();
  await expect(page.getByRole("heading", { name: "분석 입력이 준비되지 않아 실행을 보류했습니다." })).toBeVisible();
  await expect(page.getByText("시점 고정 국내 종목 데이터가 없습니다", { exact: false })).toBeVisible();
  await expect(page.locator(".ops-gate-grid article").nth(0)).toContainText("Risk Gate");
  await expect(page.locator(".ops-gate-grid article").nth(0)).toContainText("HOLD");
  await expect(page.locator(".ops-gate-grid article").nth(1)).toContainText("QA Gate");
  await expect(page.locator(".ops-gate-grid article").nth(1)).toContainText("실패");
  await expect(page.locator(".ops-gate-grid article").nth(2)).toContainText("대표 승인");
  await expect(page.locator(".ops-gate-grid article").nth(2)).toContainText("검토 대기");
  await expect(page.getByRole("button", { name: "주문 전송" })).toHaveCount(0);
  await expect(page.getByText("Ledger Posting", { exact: true })).toHaveCount(0);
});
test("keeps distinct Risk reject and QA inconclusive states fail closed", async ({ page }) => {
  const result = makeRecommendationResult({
    pipeline_status: "DEGRADED",
    safe_action: "HOLD",
    manual_review_required: true,
    risk_gate: { verdict: "REJECT", reason: "결정론적 Risk 한도 검증 거절" },
    qa_gate: { decision: "INCONCLUSIVE", reason: "근거 검증이 완료되지 않았습니다." },
  });
  const state = await installBffFixture(page, {
    snapshot: makeSnapshot({
      status: "DEGRADED",
      run_id: "run-gates-distinct",
      workflow: "portfolio-recommendation-full",
      phase: "QA",
      result,
      approval: { status: "PENDING", binding: false, approved_at: null, comment: "수동 검토 필요" },
    }),
  });
  await page.goto("/");
  await expect.poll(() => state.snapshotRequests).toBeGreaterThan(0);
  await page.getByRole("button", { name: "📊 대시보드" }).click();
  await page.getByRole("button", { name: "Operations Console" }).click();

  await expect(page.locator(".ops-gate-grid article").nth(0)).toContainText("거절됨");
  await expect(page.locator(".ops-gate-grid article").nth(1)).toContainText("INCONCLUSIVE");
  await expect(page.getByRole("button", { name: "주문 전송" })).toHaveCount(0);
  await expect(page.getByText("Ledger Posting", { exact: true })).toHaveCount(0);
});
