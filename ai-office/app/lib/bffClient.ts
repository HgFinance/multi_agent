import { type FrontendAuthMode } from "./authMode";
import { readStoredAccount } from "./currentAccount";
import { AuthenticationRequiredError } from "./supabaseBrowser";

const configuredBff = process.env.NEXT_PUBLIC_BFF_URL?.trim();
export const BFF = (configuredBff || "http://127.0.0.1:8001").replace(/\/+$/, "");

export const AUTHENTICATION_REQUIRED_EVENT = "hgfinance:authentication-required";

export interface BffRequestInit extends RequestInit {
  /** Mutations are never replayed unless the caller explicitly proves idempotency. */
  retryMutationAfterRefresh?: boolean;
  /** 이 요청만 다른 데드라인을 쓸 때. 0 이하면 데드라인을 걸지 않는다. */
  timeoutMs?: number;
}

/**
 * 요청 데드라인.
 *
 * `fetch`에는 기본 타임아웃이 없다. AWS에서 BFF가 응답을 멈추면(스레드풀
 * 고갈, 죽은 upstream 소켓 등) 브라우저는 오류도 없이 무한히 pending 상태로
 * 남고, react-query는 응답을 기다리느라 재시도조차 하지 않는다 - "요청이
 * 영원히 pending인데 타임아웃도 안 난다"의 클라이언트 쪽 절반이 이것이다.
 * 서버(apps/api/main.py의 `_fail_on_request_deadline`)보다 넉넉하게 잡아,
 * 서버가 살아 있으면 이쪽이 아니라 서버의 504를 보게 한다.
 */
const DEFAULT_TIMEOUT_MS = 45_000;

/**
 * 브라우저가 쓰는 동일 출처 프록시 prefix (`worker/bffProxy.ts`가 받는다).
 *
 * 이 값을 바꾸면 Worker의 `BFF_PROXY_PREFIX`도 같이 바꿔야 한다.
 */
export const BFF_PROXY_PREFIX = "/bff";

/**
 * 브라우저에서는 절대 cross-origin으로 나가지 않는다.
 *
 * `X-User-Id`/`Idempotency-Key`는 비표준 헤더라 cross-origin이면 매 요청이
 * preflight를 동반하고, 그 성패가 BFF의 `APP_ENV`·`PORTFOLIO_CORS_ALLOW_ORIGINS`와
 * dev 서버가 그날 잡은 포트(3000 점유 시 3001…), `localhost`/`127.0.0.1`
 * 표기에 따라 갈렸다 — 랜덤하게 보이던 CORS 오류의 원인이다. 동일 출처
 * `/bff/*`로 보내면 CORS 규칙 자체가 적용되지 않는다. SSR/Worker 실행 시에는
 * 브라우저가 아니므로 설정된 절대 주소를 그대로 쓴다.
 */
function requestUrl(path: string): string {
  if (!path.startsWith("/")) throw new Error("bff_path_must_be_absolute");
  if (typeof window !== "undefined") return `${BFF_PROXY_PREFIX}${path}`;
  return `${BFF}${path}`;
}

export function shouldRetryAfterAuthenticationFailure(method: string, explicitlyIdempotent = false): boolean {
  return ["GET", "HEAD", "OPTIONS"].includes(method.toUpperCase()) || explicitlyIdempotent;
}

export function buildBffAuthHeaders(
  mode: FrontendAuthMode,
  input: HeadersInit | undefined,
  credential: string,
): Headers {
  const headers = new Headers(input);
  headers.delete("X-User-Id");
  headers.delete("Authorization");
  if (mode === "fixture") headers.set("X-User-Id", credential);
  else headers.set("Authorization", `Bearer ${credential}`);
  return headers;
}

/**
 * 이 요청에 실을 신원 헤더.
 *
 * `AUTH_MODE`/`fixtureAuthEnabled`로 분기하지 않는다(2026-08-19) - 실제
 * Supabase 인증을 붙이지 않기로 했는데, 그 두 값은 `NEXT_PUBLIC_AUTH_MODE`
 * 환경변수에서 나오고 이 값이 SSR·클라이언트 번들에서 다르게 읽히는 문제가
 * 반복됐다(vite.config.ts: Cloudflare Worker와 Vite 클라이언트 빌드가 env를
 * 따로 받는다). `AUTH_MODE`가 잘못 "supabase"로 평가되면 `X-User-Id` 대신
 * `Authorization: Bearer <uuid>`를 보내버려 서버가 그걸 서명된 JWT로 검증하려다
 * 실패한다. 고정 계정 하나뿐이니 무조건 `fixture` 헤더를 만든다.
 */
async function authenticatedHeaders(init: BffRequestInit, forceRefresh = false): Promise<Headers> {
  void forceRefresh; // Supabase 재발급 경로가 없어져 더 이상 쓰이지 않는다.
  return buildBffAuthHeaders("fixture", init.headers, readStoredAccount().userId);
}

/** Verified browser identity for legacy request bodies that still require an actor field. */
export async function getAuthenticatedSubject(): Promise<string> {
  return readStoredAccount().userId;
}

function notifyAuthenticationRequired(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(AUTHENTICATION_REQUIRED_EVENT));
  }
}

/**
 * The only HTTP transport allowed to call the production portfolio BFF.
 *
 * 401 재시도 경로를 없앴다(2026-08-19). 그 경로의 목적은 만료된 Supabase
 * access token을 재발급받아 한 번 더 보내는 것이었는데, 고정 계정 헤더
 * (`X-User-Id`)는 만료되지 않으므로 두 번째 시도가 첫 번째와 **완전히 같은
 * 요청**이 된다 - 같은 401을 두 번 받고 서버 부하만 두 배가 된다.
 */
export async function bffFetch(path: string, init: BffRequestInit = {}): Promise<Response> {
  const { retryMutationAfterRefresh, timeoutMs, ...requestInit } = init;
  void retryMutationAfterRefresh;
  // 호출자가 signal을 직접 넘겼으면(SSE 구독처럼 수명을 스스로 관리하는 경우)
  // 그 수명을 그대로 존중한다. 데드라인이 스트림을 끊으면 안 된다.
  const deadlineMs = timeoutMs ?? (requestInit.signal ? 0 : DEFAULT_TIMEOUT_MS);
  const signal =
    deadlineMs > 0 ? AbortSignal.timeout(deadlineMs) : requestInit.signal;
  let response: Response;
  try {
    response = await fetch(requestUrl(path), {
      ...requestInit,
      cache: init.cache ?? "no-store",
      signal,
      headers: await authenticatedHeaders(init),
    });
  } catch (cause) {
    if (cause instanceof AuthenticationRequiredError) notifyAuthenticationRequired();
    throw cause;
  }
  if (response.status === 401) notifyAuthenticationRequired();
  return response;
}
