export type FrontendAuthMode = "supabase" | "fixture";

/**
 * 이 앱의 인증 모드.
 *
 * Vite가 서버·클라이언트 양쪽에 동일한 `NEXT_PUBLIC_AUTH_MODE` 상수를 주입한다.
 * 런타임에서 서로 다른 env를 읽지 않으므로 SSR/client hydration이 갈라지지
 * 않는다. `supabase_jwt`도 백엔드 설정명 그대로 허용한다.
 */
function normalizeAuthMode(value: unknown): FrontendAuthMode {
  const normalized = String(value ?? "").trim().toLowerCase();
  return normalized === "supabase" || normalized === "supabase_jwt"
    ? "supabase"
    : "fixture";
}

export const AUTH_MODE: FrontendAuthMode = normalizeAuthMode(
  process.env.NEXT_PUBLIC_AUTH_MODE,
);

export const fixtureAuthEnabled = AUTH_MODE === "fixture";

/** 과거 호출부 호환용. 모드는 빌드 시 주입된 단일 값만 사용한다. */
export function resolveAuthMode(
  _configured?: unknown,
  _runtimeEnvironment?: unknown,
): FrontendAuthMode {
  return AUTH_MODE;
}
