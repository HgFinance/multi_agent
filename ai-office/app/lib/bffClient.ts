import { readStoredAccount } from "./currentAccount";

const configuredBff = process.env.NEXT_PUBLIC_BFF_URL?.trim();
export const BFF = (configuredBff || "http://127.0.0.1:8001").replace(/\/+$/, "");

export interface BffRequestInit extends RequestInit {
  /** Mutations are only replayable when the caller supplies an idempotency key. */
  retryIdempotentMutation?: boolean;
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

export function buildBffIdentityHeaders(
  input: HeadersInit | undefined,
  userId: string,
): Headers {
  const headers = new Headers(input);
  headers.delete("X-User-Id");
  headers.delete("Authorization");
  if (userId) headers.set("X-User-Id", userId);
  return headers;
}

/**
 * 이 요청에 실을 신원 헤더.
 *
 * 브라우저는 고정 데모 계정 ID만 보낸다. 사용자 자격증명이나 세션은 다루지
 * 않는다.
 */
function identityHeaders(init: BffRequestInit): Headers {
  return buildBffIdentityHeaders(init.headers, readStoredAccount().userId);
}

/** Current demo identity for legacy request bodies that still require an actor field. */
export function getCurrentUserId(): string {
  return readStoredAccount().userId;
}

/**
 * The only HTTP transport allowed to call the production portfolio BFF.
 *
 * 요청은 한 번만 전송한다. 재시도는 호출부가 멱등 키와 함께 별도로 관리한다.
 */
export async function bffFetch(path: string, init: BffRequestInit = {}): Promise<Response> {
  const { retryIdempotentMutation, timeoutMs, ...requestInit } = init;
  void retryIdempotentMutation;
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
      headers: identityHeaders(init),
    });
  } catch (cause) {
    throw cause;
  }
  return response;
}
