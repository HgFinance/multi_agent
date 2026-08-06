import { test, expect } from "@playwright/test";

const liveE2e = process.env.PORTFOLIO_LIVE_E2E === "1";
const bffUrl = process.env.NEXT_PUBLIC_BFF_URL;
const liveUserId = process.env.PORTFOLIO_LIVE_USER_ID;
const liveAuthToken = process.env.PORTFOLIO_LIVE_AUTH_TOKEN;
const mandateId = process.env.PORTFOLIO_LIVE_MANDATE_ID;
const recommendationInput = process.env.PORTFOLIO_LIVE_RECOMMENDATION_INPUT;

function parseRecommendationInput() {
  if (!recommendationInput) return null;
  const value = JSON.parse(recommendationInput);
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("PORTFOLIO_LIVE_RECOMMENDATION_INPUT must be a JSON object");
  }
  return value;
}

function authHeaders() {
  return {
    "X-User-Id": liveUserId,
    Authorization: `Bearer ${liveAuthToken}`,
  };
}

test.describe("live advisory BFF boundary", () => {
  test.beforeEach(async () => {
    test.skip(!liveE2e, "Set PORTFOLIO_LIVE_E2E=1 for a deployed authenticated environment.");
    test.skip(!bffUrl, "NEXT_PUBLIC_BFF_URL must point at the deployed BFF.");
    test.skip(!liveUserId, "PORTFOLIO_LIVE_USER_ID must identify the authenticated operator.");
    test.skip(!liveAuthToken, "PORTFOLIO_LIVE_AUTH_TOKEN must be supplied by CI secrets.");
  });

  test("reads a canonical snapshot and keeps the browser non-binding", async ({ page, request }) => {
    await page.context().setExtraHTTPHeaders(authHeaders());
    const response = await request.get(`${bffUrl}/ui/snapshot`, { headers: authHeaders() });
    expect(response.ok()).toBeTruthy();
    const snapshot = await response.json();
    expect(["DEMO", "PAPER", "LIVE"]).toContain(snapshot.mode);
    expect(snapshot.operations).toBeTruthy();
    expect(snapshot.trading).toBeTruthy();
    expect(snapshot.ledger).toBeTruthy();

    await page.goto("/");
    await page.getByRole("button", { name: "📊 대시보드" }).click();
    await page.getByRole("button", { name: "Operations Console" }).click();
    await expect(page.getByRole("heading", { name: "실행의 모든 흔적을" })).toBeVisible();
    await expect(page.getByRole("button", { name: "주문 전송" })).toHaveCount(0);
    await expect(page.getByText("Ledger Posting", { exact: true })).toHaveCount(0);
  });

  test("binds Governance mandate metadata to one browser-started run", async ({ page }) => {
    test.skip(!mandateId, "PORTFOLIO_LIVE_MANDATE_ID must identify the canonical mandate.");
    const input = parseRecommendationInput();
    test.skip(!input, "PORTFOLIO_LIVE_RECOMMENDATION_INPUT must be supplied by CI configuration.");
    expect(input.mandate_id).toBe(mandateId);
    expect(typeof input.mandate_version_id).toBe("string");
    expect(typeof input.policy_hash).toBe("string");
    expect(typeof input.trace_id).toBe("string");

    await page.context().setExtraHTTPHeaders(authHeaders());
    const current = await page.evaluate(async ({ url, id }) => {
      const response = await fetch(`${url}/ui/mandates/${encodeURIComponent(id)}/current`, {
        headers: { Accept: "application/json" },
      });
      return { status: response.status, body: await response.json().catch(() => null) };
    }, { url: bffUrl, id: mandateId });
    expect(current.status).toBe(200);
    expect(current.body).toMatchObject({
      mandate_id: mandateId,
      mandate_version_id: input.mandate_version_id,
      policy_hash: input.policy_hash,
    });
    expect(current.body).toHaveProperty("case_id");

    const started = await page.evaluate(async ({ url, body }) => {
      const response = await fetch(`${url}/ui/portfolio-recommendations`, {
        method: "POST",
        headers: {
          Accept: "application/json",
          "Content-Type": "application/json",
          "Idempotency-Key": body.trace_id,
        },
        body: JSON.stringify(body),
      });
      return { status: response.status, body: await response.json().catch(() => null) };
    }, { url: bffUrl, body: input });
    expect([202, 409]).toContain(started.status);
    expect(started.body).toMatchObject({
      run_id: expect.any(String),
      mandate_id: mandateId,
      mandate_version_id: input.mandate_version_id,
      policy_hash: input.policy_hash,
    });

    const runId = started.body.run_id;
    await expect.poll(async () => {
      const status = await page.evaluate(async ({ url, id }) => {
        const response = await fetch(`${url}/ui/portfolio-recommendations/${encodeURIComponent(id)}`, {
          headers: { Accept: "application/json" },
        });
        return { status: response.status, body: await response.json().catch(() => null) };
      }, { url: bffUrl, id: runId });
      expect(status.status).toBe(200);
      return {
        ...status.body,
        binding: status.body?.result?.binding,
        external_writes: status.body?.result?.external_writes,
      };
    }, { timeout: 30_000, intervals: [500, 1_000, 2_000] }).toMatchObject({
      run_id: runId,
      mandate_id: mandateId,
      mandate_version_id: input.mandate_version_id,
      policy_hash: input.policy_hash,
      binding: false,
      external_writes: false,
    });

    const finalStatus = await page.evaluate(async ({ url, id }) => {
      const response = await fetch(`${url}/ui/portfolio-recommendations/${encodeURIComponent(id)}`, {
        headers: { Accept: "application/json" },
      });
      return response.json();
    }, { url: bffUrl, id: runId });
    expect(finalStatus.result?.binding).toBe(false);
    expect(finalStatus.result?.external_writes).toBe(false);
  });
});
