/**
 * 동일 출처 BFF reverse proxy.
 *
 * 브라우저가 `http://127.0.0.1:8001`처럼 다른 출처의 BFF를 직접 부르면
 * `X-User-Id`/`Idempotency-Key` 같은 비표준 헤더 때문에 매 요청이 preflight를
 * 동반한다. 그 preflight는 BFF의 `PORTFOLIO_CORS_ALLOW_ORIGINS`·`APP_ENV`,
 * dev 서버가 실제로 잡은 포트(3000이 점유되면 3001…), `localhost`냐
 * `127.0.0.1`이냐에 모두 의존해서 성공/실패가 갈린다 — 이게 "랜덤 CORS"의
 * 정체다. 브라우저 요청을 전부 `/bff/*` 동일 출처로 받고 Worker가 서버
 * 사이에서 전달하면 CORS 규칙 자체가 적용되지 않는다.
 */

export const BFF_PROXY_PREFIX = "/bff";

const DEFAULT_BFF_ORIGIN = "http://127.0.0.1:8001";

/** BFF가 실제로 읽는 헤더만 전달한다(쿠키 등은 넘기지 않는다). */
const FORWARDED_REQUEST_HEADERS = [
  "accept",
  "accept-language",
  "content-type",
  "x-user-id",
  "idempotency-key",
  "x-request-id",
  "last-event-id",
] as const;

/**
 * 동일 출처 응답에 CORS 헤더가 남아 있을 이유가 없고, 본문을 다시 감싸는
 * 순간 upstream의 인코딩·길이 헤더는 더 이상 사실이 아니다.
 */
const STRIPPED_RESPONSE_HEADERS = new Set([
  "access-control-allow-credentials",
  "access-control-allow-headers",
  "access-control-allow-methods",
  "access-control-allow-origin",
  "access-control-expose-headers",
  "access-control-max-age",
  "content-encoding",
  "content-length",
  "set-cookie",
  "transfer-encoding",
]);

const BODYLESS_METHODS = new Set(["GET", "HEAD"]);

export interface BffProxyEnv {
  /** 서버 전용 BFF 주소. 없으면 기존 공개 변수를 그대로 쓴다. */
  BFF_ORIGIN?: string;
  NEXT_PUBLIC_BFF_URL?: string;
}

/** 신뢰할 수 있는 BFF base URL만 통과시킨다. */
export function resolveBffBase(env: BffProxyEnv | undefined): string {
  // Cloudflare supplies `env`; vinext's Node production server invokes the
  // Worker entry with an undefined binding object.  Keep the same allowlist
  // in both runtimes and use the private process env only on the Node side.
  const processEnv = typeof process !== "undefined" ? process.env : undefined;
  const configured = (
    env?.BFF_ORIGIN ??
    env?.NEXT_PUBLIC_BFF_URL ??
    processEnv?.BFF_ORIGIN ??
    processEnv?.NEXT_PUBLIC_BFF_URL ??
    ""
  ).trim();
  const candidate = configured || DEFAULT_BFF_ORIGIN;
  let parsed: URL;
  try {
    parsed = new URL(candidate);
  } catch {
    throw new Error("invalid_bff_origin");
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") throw new Error("invalid_bff_origin");
  if (parsed.username || parsed.password) throw new Error("invalid_bff_origin");
  if (parsed.search || parsed.hash) throw new Error("invalid_bff_origin");
  return `${parsed.origin}${parsed.pathname.replace(/\/+$/, "")}`;
}

/** `/bff/ui/snapshot` → BFF의 `/ui/snapshot`. */
export function bffTargetUrl(requestUrl: string, base: string): string {
  const url = new URL(requestUrl);
  const suffix = url.pathname.slice(BFF_PROXY_PREFIX.length) || "/";
  // 정규화된 pathname이므로 `..`는 이미 접혀 있지만, prefix 밖으로 나가는
  // 경로는 명시적으로 막는다.
  if (!suffix.startsWith("/")) throw new Error("invalid_bff_path");
  return `${base}${suffix}${url.search}`;
}

export function isBffProxyPath(pathname: string): boolean {
  return pathname === BFF_PROXY_PREFIX || pathname.startsWith(`${BFF_PROXY_PREFIX}/`);
}

export async function proxyBffRequest(request: Request, env: BffProxyEnv): Promise<Response> {
  let target: string;
  try {
    target = bffTargetUrl(request.url, resolveBffBase(env));
  } catch (cause) {
    return Response.json({ error: "bff_proxy_misconfigured", detail: String(cause) }, { status: 500 });
  }

  const headers = new Headers();
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = request.headers.get(name);
    if (value) headers.set(name, value);
  }
  const origin = new URL(request.url);
  headers.set("x-forwarded-host", origin.host);
  headers.set("x-forwarded-proto", origin.protocol.replace(":", ""));

  let upstream: Response;
  try {
    upstream = await fetch(target, {
      method: request.method,
      headers,
      body: BODYLESS_METHODS.has(request.method.toUpperCase()) ? undefined : request.body,
      // upstream 리다이렉트를 따라가면 의도치 않은 출처로 자격 헤더가 새어나간다.
      redirect: "manual",
      // 스트리밍 본문(SSE·업로드)을 그대로 흘려보내기 위한 표준 플래그.
      duplex: "half",
    } as RequestInit);
  } catch (cause) {
    // 여기서 502를 만들어야 프론트가 "네트워크 실패"가 아닌 실제 상태를 본다.
    return Response.json({ error: "bff_unreachable", detail: String(cause) }, { status: 502 });
  }

  const responseHeaders = new Headers();
  upstream.headers.forEach((value, name) => {
    if (!STRIPPED_RESPONSE_HEADERS.has(name.toLowerCase())) responseHeaders.set(name, value);
  });
  return new Response(upstream.body, {
    status: upstream.status,
    statusText: upstream.statusText,
    headers: responseHeaders,
  });
}
