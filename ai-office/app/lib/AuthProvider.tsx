"use client";

import type { Session } from "@supabase/supabase-js";
import { useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo } from "react";
import { AUTHENTICATION_REQUIRED_EVENT } from "./bffClient";
import { DEFAULT_ACCOUNT } from "./currentAccount";
import { clearAuthorizedFunds } from "./currentFund";

/**
 * 고정 계정 신원 공급자.
 *
 * ## 왜 Supabase 세션 배선을 걷어냈나 (2026-08-19)
 *
 * 이 앱은 실제 Supabase 인증을 붙이지 않는다. 계정은 Fund Owner 하나로
 * 고정돼 있다(`currentAccount.ts`).
 *
 * 그 전까지 이 파일은 `fixtureAuthEnabled`(→ `NEXT_PUBLIC_AUTH_MODE`
 * 환경변수)로 "고정 계정" 경로와 "Supabase 세션" 경로를 갈랐다. 그런데 이 앱은
 * Cloudflare Worker(SSR)와 Vite 클라이언트 번들이 환경변수를 **서로 다른
 * 경로로** 받는다(`vite.config.ts`) - 브라우저 쪽에서 그 값이 안 잡히면
 * `AUTH_MODE`가 `supabase`로 평가되고, 그 순간 다음이 전부 무너졌다:
 *
 *   - `status`가 영영 `loading`/`unauthenticated`에 머물러 화면이 게이트에서 멈춤
 *   - `userId`가 `null`이라 `PortfolioSessionProvider` 조회가 아예 실행 안 됨
 *   - 그 결과 활성 Fund가 없어 "계정에 연결된 Fund가 없습니다"가 반복
 *   - SSR/클라이언트가 서로 다른 트리를 그려 hydration mismatch
 *
 * 그래서 **환경변수에 의존하는 분기를 전부 제거**한다. 이제 이 Provider는
 * 조건 없이 고정 계정을 `authenticated` 상태로 공급한다 - 서버·클라이언트가
 * 다르게 평가할 수 있는 값이 하나도 없으므로 위 실패들이 구조적으로 불가능하다.
 *
 * ## 이건 인증이 아니다
 *
 * `X-User-Id`는 서명이 없어 신원을 증명하지 않는다(`apps/api/current_user.py`
 * 머리말). 이 Provider는 "누구로 요청할지"를 정할 뿐 접근을 통제하지 않는다.
 * 실제 로그인을 다시 붙이려면 이 파일이 아니라 라우트 분리부터 다시 설계해야
 * 한다 - 같은 트리 안에서 env 파생 값으로 SSR 출력을 가르면 이 문제가 그대로
 * 재발한다.
 */

export type AuthStatus = "loading" | "authenticated" | "unauthenticated" | "error";

interface AuthContextValue {
  mode: "fixture";
  status: AuthStatus;
  session: Session | null;
  userId: string | null;
  email: string | null;
  error: string | null;
  signInWithPassword(email: string, password: string): Promise<void>;
  signOut(): Promise<void>;
  refreshSession(): Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

/** 고정 계정은 로그인·로그아웃·세션 갱신이라는 개념이 없다. */
async function unsupported(): Promise<void> {
  throw new Error("fixed_account_mode_does_not_support_session_operations");
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();

  // BFF가 401을 주면 캐시된 펀드 권한을 버린다. 고정 계정에서는 재발급할
  // 세션이 없으므로 여기서 할 수 있는 일은 상태 정리뿐이다.
  useEffect(() => {
    const requireAuthentication = () => {
      clearAuthorizedFunds();
      queryClient.clear();
    };
    window.addEventListener(AUTHENTICATION_REQUIRED_EVENT, requireAuthentication);
    return () => window.removeEventListener(AUTHENTICATION_REQUIRED_EVENT, requireAuthentication);
  }, [queryClient]);

  const value = useMemo<AuthContextValue>(
    () => ({
      mode: "fixture",
      status: "authenticated",
      session: null,
      userId: DEFAULT_ACCOUNT.userId,
      email: null,
      error: null,
      signInWithPassword: unsupported,
      signOut: unsupported,
      refreshSession: unsupported,
    }),
    [],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth_must_be_inside_AuthProvider");
  return value;
}

/**
 * 항상 children을 그대로 통과시킨다.
 *
 * 예전에는 인증 상태에 따라 게이트 화면을 그렸는데, 그 분기가 위 머리말의
 * 환경변수 문제로 SSR과 클라이언트에서 다르게 평가돼 hydration mismatch와
 * "인증 확인 중" 멈춤을 만들었다. 막아야 할 "인증 안 된 상태"가 애초에 없으므로
 * 분기 자체를 없앤다.
 */
export function AuthGate({ children }: { children: React.ReactNode }) {
  return children;
}
