import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { resolveAuthMode } from "../app/lib/authMode.ts";
import { safeInternalNextPath } from "../app/lib/authNavigation.ts";
import {
  buildBffAuthHeaders,
  shouldRetryAfterAuthenticationFailure,
} from "../app/lib/bffClient.ts";
import { withVerifiedCeoActor } from "../app/lib/ceoMirrorClient.ts";
import { parseCurrentUser } from "../app/lib/currentUserContract.ts";
import { createSseParser, sseReconnectDelay } from "../app/lib/sseClient.ts";
import {
  clearLocalSupabaseSession,
  isBrowserSafeSupabaseKey,
  validateSupabasePublishableKey,
} from "../app/lib/supabaseBrowser.ts";

function base64url(value) {
  return Buffer.from(JSON.stringify(value)).toString("base64url");
}

function legacySupabaseJwt(role) {
  return `${base64url({ alg: "HS256", typ: "JWT" })}.${base64url({ role })}.signature`;
}

test("identity is a fixed account and never depends on an environment variable", () => {
  // 2026-08-19: `NEXT_PUBLIC_AUTH_MODE` 기반 분기를 없앴다. 이 앱은 Cloudflare
  // Worker(SSR)와 Vite 클라이언트가 env를 서로 다른 경로로 받아, 같은 코드가
  // 서버에서는 fixture로 클라이언트에서는 supabase로 평가되는 일이 반복됐다.
  // 인자를 무엇으로 주든 결과가 같아야 그 갈림이 구조적으로 불가능하다.
  assert.equal(resolveAuthMode(), "fixture");
  assert.equal(resolveAuthMode("supabase", "production"), "fixture");
  assert.equal(resolveAuthMode(undefined, undefined), "fixture");
});

test("login next accepts only same-origin absolute paths", () => {
  assert.equal(safeInternalNextPath("/mandate?step=2"), "/mandate?step=2");
  assert.equal(safeInternalNextPath("//evil.example"), "/dashboard");
  assert.equal(safeInternalNextPath("/%2f%2fevil.example"), "/dashboard");
  assert.equal(safeInternalNextPath("/%2e%2e//evil.example"), "/dashboard");
  assert.equal(safeInternalNextPath("/\\evil.example"), "/dashboard");
  assert.equal(safeInternalNextPath("https://evil.example"), "/dashboard");
});

test("browser Supabase config accepts publishable or anon and rejects privileged keys", () => {
  assert.equal(isBrowserSafeSupabaseKey("sb_publishable_example"), true);
  assert.equal(isBrowserSafeSupabaseKey(legacySupabaseJwt("anon")), true);
  assert.equal(isBrowserSafeSupabaseKey("sb_secret_example"), false);
  assert.equal(isBrowserSafeSupabaseKey(legacySupabaseJwt("service_role")), false);
  assert.equal(isBrowserSafeSupabaseKey("malformed"), false);
  assert.throws(() => validateSupabasePublishableKey("sb_secret_example"), /publishable_or_anon/);
});

test("authentication-required cleanup removes the persisted local Supabase session", async () => {
  let observedScope = null;
  const fakeClient = {
    auth: {
      async signOut(options) {
        observedScope = options.scope;
        return { error: null };
      },
    },
  };
  await clearLocalSupabaseSession(fakeClient);
  assert.equal(observedScope, "local");
});

test("production BFF headers use Bearer and remove spoofed fixture identity", () => {
  const headers = buildBffAuthHeaders(
    "supabase",
    { "X-User-Id": "spoofed", Authorization: "old", Accept: "application/json" },
    "access-token",
  );
  assert.equal(headers.get("Authorization"), "Bearer access-token");
  assert.equal(headers.has("X-User-Id"), false);
  assert.equal(headers.get("Accept"), "application/json");

  const fixture = buildBffAuthHeaders("fixture", { Authorization: "old" }, "fixture-user");
  assert.equal(fixture.get("X-User-Id"), "fixture-user");
  assert.equal(fixture.has("Authorization"), false);
});

test("401 refresh retry never replays a mutation without explicit idempotency", () => {
  assert.equal(shouldRetryAfterAuthenticationFailure("GET"), true);
  assert.equal(shouldRetryAfterAuthenticationFailure("POST"), false);
  assert.equal(shouldRetryAfterAuthenticationFailure("PUT"), false);
  assert.equal(shouldRetryAfterAuthenticationFailure("POST", true), true);
});

test("CEO ingress always replaces caller-supplied actor identity", () => {
  const ingress = withVerifiedCeoActor({
    query: "status",
    request_id: "request-1",
    source: "web",
    source_message_id: "message-1",
    actor_id: "spoofed",
    actor_type: "system",
  }, "verified-subject");
  assert.equal(ingress.actor_id, "verified-subject");
  assert.equal(ingress.actor_type, "user");
});

test("SSE parser survives CRLF chunk boundaries, multiline data, and comments", () => {
  const events = [];
  const parser = createSseParser((event) => events.push(event));
  parser.push(": heartbeat\r\n\r\nid: evt-1\r");
  parser.push("\nevent: TASK_COMPLETED\r\ndata: line one\r\ndata: line two\r\n\r\n");
  parser.push("event: message\ndata: tail");
  parser.finish();
  assert.deepEqual(events, [
    { id: "evt-1", event: "TASK_COMPLETED", data: "line one\nline two" },
    { id: null, event: "message", data: "tail" },
  ]);
});

test("SSE EOF/error reconnect delay is bounded and never storms at 250ms", () => {
  assert.equal(sseReconnectDelay(1), 1_000);
  assert.equal(sseReconnectDelay(2), 2_000);
  assert.equal(sseReconnectDelay(99), 10_000);
});

test("/ui/me v1 exposes only active authorized funds and onboarding state", () => {
  assert.deepEqual(parseCurrentUser({
    schema_version: "portfolio.current-user.v1",
    user_id: "user-1",
    display_name: "Operator",
    status: "ACTIVE",
    funds: [{
      fund_id: "fund-1",
      roles: ["OWNER"],
      books: [{ book_id: "book-1", name: "Primary PAPER" }],
    }],
    onboarding_required: false,
  }), {
    schemaVersion: "portfolio.current-user.v1",
    userId: "user-1",
    displayName: "Operator",
    status: "ACTIVE",
    funds: [{
      fundId: "fund-1",
      roles: ["OWNER"],
      books: [{ bookId: "book-1", name: "Primary PAPER" }],
    }],
    onboardingRequired: false,
  });
  assert.throws(() => parseCurrentUser({ schema_version: "portfolio.current-user.v1", user_id: "u", status: "SUSPENDED" }));
});

test("production clients have no raw EventSource, WebSocket, or direct BFF fetch", async () => {
  const files = [
    "ceoClient.ts",
    "ceoMirrorClient.ts",
    "discordClient.ts",
    "mandateClient.ts",
    "operationsClient.ts",
    "paperOrderClient.ts",
    "accountingLedgerClient.ts",
    "marketRankingClient.ts",
    "portfolioLiveClient.ts",
  ];
  for (const file of files) {
    const source = await readFile(new URL(`../app/lib/${file}`, import.meta.url), "utf8");
    assert.doesNotMatch(source, /new\s+(?:EventSource|WebSocket)\b/, file);
    assert.doesNotMatch(source, /\bfetch\s*\(/, file);
    assert.doesNotMatch(source, /withAccountHeaders/, file);
  }
  // authMode는 이제 상수다 - 런타임 검사(`assertSafeAuthMode`)가 지키던 것을
  // 타입/상수 자체가 지킨다. 대신 env를 다시 읽지 않는지를 고정한다.
  const authModeSource = await readFile(new URL("../app/lib/authMode.ts", import.meta.url), "utf8");
  assert.doesNotMatch(authModeSource, /process\.env/, "authMode must not read an environment variable");

  const currentFundSource = await readFile(new URL("../app/lib/currentFund.ts", import.meta.url), "utf8");
  const activeFundGuard = currentFundSource.indexOf("if (activeFundId) return activeFundId;");
  const fixedFallback = currentFundSource.indexOf("return readStoredAccount().fundId ?? undefined;");
  assert.notEqual(activeFundGuard, -1, "currentFund must return the authorized active fund");
  assert.notEqual(fixedFallback, -1, "currentFund must retain the offline fixed-account fallback");
  assert.ok(activeFundGuard < fixedFallback, "the server-authorized active fund must win over the fallback");
});
