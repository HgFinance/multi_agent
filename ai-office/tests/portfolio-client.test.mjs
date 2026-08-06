import assert from "node:assert/strict";
import { test, beforeEach, afterEach } from "node:test";

// F-09 regression coverage: startSavedPortfolioRecommendation() must fetch the
// current Mandate and populate mandate_version_id/policy_hash before restarting
// a saved-draft recommendation. See:
//   ai-office/app/ops/portfolioClient.ts (startSavedPortfolioRecommendation)
//   ai-office/app/ops/governanceClient.ts (fetchCurrentMandate)

const ORIGINAL_FETCH = globalThis.fetch;
const ORIGINAL_WINDOW = globalThis.window;
const ORIGINAL_LOCAL_STORAGE = globalThis.localStorage;

function fakeLocalStorage(store) {
  return {
    getItem: (key) => (key in store ? store[key] : null),
    setItem: (key, value) => {
      store[key] = String(value);
    },
    removeItem: (key) => {
      delete store[key];
    },
  };
}

beforeEach(() => {
  // startSavedPortfolioRecommendation() guards on `typeof window === "undefined"`,
  // so a minimal window/localStorage shim is required to exercise the browser path.
  globalThis.window = {};
  globalThis.localStorage = fakeLocalStorage({});
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  if (ORIGINAL_WINDOW === undefined) delete globalThis.window;
  else globalThis.window = ORIGINAL_WINDOW;
  if (ORIGINAL_LOCAL_STORAGE === undefined) delete globalThis.localStorage;
  else globalThis.localStorage = ORIGINAL_LOCAL_STORAGE;
});

test("startSavedPortfolioRecommendation fetches the current Mandate and forwards mandate_version_id/policy_hash", async () => {
  const draft = {
    user_id: "web-user",
    mandate_id: "m-42",
    investment_amount: "10000000",
    universe_id: "KOREA_EQUITY_WATCHLIST",
  };
  globalThis.localStorage.setItem(
    "hgfinance.mandate-config.v1",
    JSON.stringify({ draft }),
  );

  const calls = [];
  globalThis.fetch = async (url, init) => {
    const href = String(url);
    calls.push({ href, init });
    if (href.includes("/ui/mandates/m-42/current")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({
          mandate_id: "m-42",
          case_id: null,
          current_version: 3,
          mandate_version_id: "mv-abc123",
          policy_hash: "sha256:deadbeef",
          status: "ACTIVE",
        }),
      };
    }
    if (href.includes("/ui/portfolio-recommendations")) {
      return {
        ok: true,
        status: 200,
        json: async () => ({ run_id: "run-1", status: "QUEUED" }),
      };
    }
    throw new Error(`unexpected fetch: ${href}`);
  };

  // Import lazily (after the fetch/window/localStorage shims are installed) so
  // any module-level environment reads see the test doubles.
  const { startSavedPortfolioRecommendation } = await import(
    "../app/ops/portfolioClient.ts"
  );

  const result = await startSavedPortfolioRecommendation();

  assert.equal(result.run_id, "run-1");
  assert.equal(result.status, "QUEUED");

  const mandateCall = calls.find((c) => c.href.includes("/ui/mandates/m-42/current"));
  assert.ok(mandateCall, "expected startSavedPortfolioRecommendation to call fetchCurrentMandate(mandate_id) before restarting");

  const startCall = calls.find((c) => c.href.includes("/ui/portfolio-recommendations") && c.init?.method === "POST");
  assert.ok(startCall, "expected a POST to /ui/portfolio-recommendations");
  const sentBody = JSON.parse(startCall.init.body);
  assert.equal(sentBody.mandate_version_id, "mv-abc123", "mandate_version_id must be populated from fetchCurrentMandate");
  assert.equal(sentBody.policy_hash, "sha256:deadbeef", "policy_hash must be populated from fetchCurrentMandate");
});

test("startSavedPortfolioRecommendation surfaces a clear error when the current Mandate is not bound (503)", async () => {
  const draft = {
    user_id: "web-user",
    mandate_id: "m-unbound",
    investment_amount: "10000000",
    universe_id: "KOREA_EQUITY_WATCHLIST",
  };
  globalThis.localStorage.setItem(
    "hgfinance.mandate-config.v1",
    JSON.stringify({ draft }),
  );

  globalThis.fetch = async (url) => {
    const href = String(url);
    if (href.includes("/ui/mandates/m-unbound/current")) {
      return {
        ok: false,
        status: 503,
        json: async () => ({ detail: "canonical_mandate_binding_unavailable" }),
      };
    }
    throw new Error(`unexpected fetch: ${href}`);
  };

  const { startSavedPortfolioRecommendation } = await import(
    "../app/ops/portfolioClient.ts"
  );

  await assert.rejects(
    () => startSavedPortfolioRecommendation(),
    (err) => {
      assert.ok(err instanceof Error);
      assert.notEqual(err.message.length, 0, "error message must not be empty");
      return true;
    },
    "expected startSavedPortfolioRecommendation to reject with a clear error, not silently omit mandate fields",
  );
});
