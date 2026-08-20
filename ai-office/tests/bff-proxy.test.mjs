import assert from "node:assert/strict";
import test from "node:test";

import {
  BFF_PROXY_PREFIX,
  bffTargetUrl,
  isBffProxyPath,
  proxyBffRequest,
  resolveBffBase,
} from "../worker/bffProxy.ts";

test("proxy prefix matches only the BFF namespace", () => {
  assert.equal(BFF_PROXY_PREFIX, "/bff");
  assert.ok(isBffProxyPath("/bff"));
  assert.ok(isBffProxyPath("/bff/ui/snapshot"));
  assert.equal(isBffProxyPath("/bffalo"), false);
  assert.equal(isBffProxyPath("/api/report"), false);
});

test("BFF base falls back, trims, and rejects unsafe origins", () => {
  assert.equal(resolveBffBase({}), "http://127.0.0.1:8001");
  assert.equal(resolveBffBase({ NEXT_PUBLIC_BFF_URL: "http://127.0.0.1:8001/" }), "http://127.0.0.1:8001");
  assert.equal(resolveBffBase({ BFF_ORIGIN: "https://bff.example.com", NEXT_PUBLIC_BFF_URL: "http://127.0.0.1:8001" }), "https://bff.example.com");
  for (const bad of ["ftp://bff.example.com", "http://user:pw@bff.example.com", "http://bff.example.com/?x=1", "not-a-url"]) {
    assert.throws(() => resolveBffBase({ BFF_ORIGIN: bad }), /invalid_bff_origin/, bad);
  }
});

test("proxy path maps onto the BFF path with its query string", () => {
  assert.equal(bffTargetUrl("http://localhost:3002/bff/ui/snapshot", "http://127.0.0.1:8001"), "http://127.0.0.1:8001/ui/snapshot");
  assert.equal(bffTargetUrl("http://localhost:3002/bff/ui/portfolio/live?fund=f1", "http://127.0.0.1:8001"), "http://127.0.0.1:8001/ui/portfolio/live?fund=f1");
  assert.equal(bffTargetUrl("http://localhost:3002/bff", "http://127.0.0.1:8001"), "http://127.0.0.1:8001/");
});

test("proxy forwards identity headers server-side and never a cookie", async () => {
  const seen = [];
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    seen.push({ url, init });
    return new Response('{"ok":true}', {
      status: 200,
      headers: {
        "content-type": "application/json",
        "content-length": "11",
        "access-control-allow-origin": "http://elsewhere.example",
      },
    });
  };
  try {
    const response = await proxyBffRequest(
      new Request("http://localhost:3002/bff/ui/portfolio/live", {
        headers: {
          "x-user-id": "user-1",
          "idempotency-key": "key-1",
          cookie: "session=secret",
        },
      }),
      {},
    );
    assert.equal(response.status, 200);
    // 동일 출처 응답이므로 CORS 헤더가 남아 있으면 안 되고, 본문을 다시 감싼
    // 뒤의 content-length는 더 이상 사실이 아니다.
    assert.equal(response.headers.get("access-control-allow-origin"), null);
    assert.equal(response.headers.get("content-length"), null);
    assert.deepEqual(await response.json(), { ok: true });

    assert.equal(seen.length, 1);
    assert.equal(seen[0].url, "http://127.0.0.1:8001/ui/portfolio/live");
    const forwarded = new Headers(seen[0].init.headers);
    assert.equal(forwarded.get("x-user-id"), "user-1");
    assert.equal(forwarded.get("idempotency-key"), "key-1");
    assert.equal(forwarded.get("cookie"), null);
    assert.equal(forwarded.get("x-forwarded-host"), "localhost:3002");
    assert.equal(seen[0].init.redirect, "manual");
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("an unreachable BFF becomes a 502 instead of a browser network error", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new TypeError("connect ECONNREFUSED");
  };
  try {
    const response = await proxyBffRequest(new Request("http://localhost:3002/bff/ui/snapshot"), {});
    assert.equal(response.status, 502);
    assert.equal((await response.json()).error, "bff_unreachable");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
