import { AUTH_MODE, fixtureAuthEnabled, type FrontendAuthMode } from "./authMode";
import { readStoredAccount } from "./currentAccount";
import {
  AuthenticationRequiredError,
  getSupabaseAccessToken,
  getSupabaseUserId,
} from "./supabaseBrowser";

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

async function authenticatedHeaders(init: BffRequestInit, forceRefresh = false): Promise<Headers> {
  if (fixtureAuthEnabled) {
    return buildBffAuthHeaders(AUTH_MODE, init.headers, readStoredAccount().userId);
  }
  return buildBffAuthHeaders(AUTH_MODE, init.headers, await getSupabaseAccessToken(forceRefresh));
}

/** Verified browser identity for legacy request bodies that still require an actor field. */
export async function getAuthenticatedSubject(): Promise<string> {
  if (fixtureAuthEnabled) return readStoredAccount().userId;
  return getSupabaseUserId();
}

function notifyAuthenticationRequired(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(AUTHENTICATION_REQUIRED_EVENT));
  }
}

/** The only HTTP transport allowed to call the production portfolio BFF. */
export async function bffFetch(path: string, init: BffRequestInit = {}): Promise<Response> {
  const method = (init.method ?? "GET").toUpperCase();
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

  if (
    response.status !== 401 ||
    fixtureAuthEnabled ||
    !shouldRetryAfterAuthenticationFailure(method, init.retryMutationAfterRefresh === true)
  ) {
    if (response.status === 401) notifyAuthenticationRequired();
    return response;
  }

  try {
    response = await fetch(requestUrl(path), {
      ...requestInit,
      cache: init.cache ?? "no-store",
      headers: await authenticatedHeaders(init, true),
    });
  } catch (cause) {
    notifyAuthenticationRequired();
    throw cause;
  }
  if (response.status === 401) notifyAuthenticationRequired();
  return response;
}
