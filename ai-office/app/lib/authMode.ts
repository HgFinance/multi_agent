export type FrontendAuthMode = "supabase" | "fixture";

/**
 * 이 앱의 인증 모드. **환경변수를 읽지 않는다**(2026-08-19).
 *
 * 예전에는 `NEXT_PUBLIC_AUTH_MODE`로 정했는데, 이 앱은 Cloudflare Worker(SSR)와
 * Vite 클라이언트 번들이 환경변수를 서로 다른 경로로 받는다(`vite.config.ts`).
 * 브라우저에서 그 값이 안 잡히면 같은 코드가 서버에서는 `fixture`, 클라이언트
 * 에서는 `supabase`로 평가돼 화면이 게이트에서 멈추거나 hydration mismatch가
 * 났다 - `.dev.vars` 수정도 dev 서버 재시작도 못 고치는 종류의 문제였다.
 *
 * 실제 Supabase 인증을 붙이지 않기로 했으므로(계정은 Fund Owner 하나로 고정,
 * `currentAccount.ts`) 값을 상수로 못박는다. 서버·클라이언트가 다르게 평가할
 * 여지 자체를 없애는 것이 목적이다.
 *
 * 실제 로그인을 다시 붙일 때는 이 상수를 되돌리는 것만으로는 부족하다 - 위
 * env 주입 경로 문제부터 해결해야 한다.
 */
export const AUTH_MODE: FrontendAuthMode = "fixture";

export const fixtureAuthEnabled = true;

/** 과거 호환용. 인자를 무시하고 항상 고정 모드를 준다. */
export function resolveAuthMode(): FrontendAuthMode {
  return AUTH_MODE;
}
