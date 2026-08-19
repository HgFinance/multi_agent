import { type FrontendAuthMode } from "./authMode";
import { readStoredAccount } from "./currentAccount";
import { AuthenticationRequiredError } from "./supabaseBrowser";

const configuredBff = process.env.NEXT_PUBLIC_BFF_URL?.trim();
export const BFF = (configuredBff || "http://127.0.0.1:8001").replace(/\/+$/, "");

export const AUTHENTICATION_REQUIRED_EVENT = "hgfinance:authentication-required";

export interface BffRequestInit extends RequestInit {
  /** Mutations are never replayed unless the caller explicitly proves idempotency. */
  retryMutationAfterRefresh?: boolean;
}

function requestUrl(path: string): string {
  if (!path.startsWith("/")) throw new Error("bff_path_must_be_absolute");
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
  const { retryMutationAfterRefresh, ...requestInit } = init;
  void retryMutationAfterRefresh;
  let response: Response;
  try {
    response = await fetch(requestUrl(path), {
      ...requestInit,
      cache: init.cache ?? "no-store",
      headers: await authenticatedHeaders(init),
    });
  } catch (cause) {
    if (cause instanceof AuthenticationRequiredError) notifyAuthenticationRequired();
    throw cause;
  }
  if (response.status === 401) notifyAuthenticationRequired();
  return response;
}
