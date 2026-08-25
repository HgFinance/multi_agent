"use client";

import type { Session } from "@supabase/supabase-js";
import { useQueryClient } from "@tanstack/react-query";
import { usePathname } from "next/navigation";
import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { AUTHENTICATION_REQUIRED_EVENT } from "./bffClient";
import { AUTH_MODE } from "./authMode";
import { DEFAULT_ACCOUNT } from "./currentAccount";
import { clearAuthorizedFunds } from "./currentFund";
import {
  clearLocalSupabaseSession,
  getSupabaseBrowserClient,
} from "./supabaseBrowser";

/**
 * 인증 신원 공급자.
 *
 * fixture mode에서는 기존 폐쇄망 고정 계정을 유지하고, supabase mode에서는
 * 브라우저 Supabase 세션을 단일 공급원으로 사용한다. 모드 자체는 Vite가
 * 서버·클라이언트에 같은 상수로 주입하므로 hydration mismatch를 만들지 않는다.
 */

export type AuthStatus = "loading" | "authenticated" | "unauthenticated" | "error";

interface AuthContextValue {
  mode: "supabase" | "fixture";
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

type AuthState = Pick<AuthContextValue, "status" | "session" | "userId" | "email" | "error">;

function sessionState(session: Session | null): AuthState {
  return {
    status: session ? "authenticated" : "unauthenticated",
    session,
    userId: session?.user.id ?? null,
    email: session?.user.email ?? null,
    error: null,
  };
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  const [state, setState] = useState<AuthState>(() =>
    AUTH_MODE === "fixture"
      ? {
          status: "authenticated",
          session: null,
          userId: DEFAULT_ACCOUNT.userId || null,
          email: null,
          error: null,
        }
      : {
          status: "loading",
          session: null,
          userId: null,
          email: null,
          error: null,
        },
  );

  useEffect(() => {
    if (AUTH_MODE === "fixture") return;

    let active = true;
    let client: ReturnType<typeof getSupabaseBrowserClient>;
    try {
      client = getSupabaseBrowserClient();
    } catch (error) {
      queueMicrotask(() => setState({
          status: "error",
          session: null,
          userId: null,
          email: null,
          error: error instanceof Error ? error.message : "supabase_configuration_invalid",
        }));
      return;
    }

    const applySession = (session: Session | null) => {
      if (active) setState(sessionState(session));
    };

    void client.auth.getSession().then(({ data, error }) => {
      if (error) throw error;
      applySession(data.session);
    }).catch((error: unknown) => {
      if (!active) return;
      setState({
        status: "error",
        session: null,
        userId: null,
        email: null,
        error: error instanceof Error ? error.message : "supabase_session_unavailable",
      });
    });

    const { data } = client.auth.onAuthStateChange((_event, session) => {
      applySession(session);
    });
    return () => {
      active = false;
      data.subscription.unsubscribe();
    };
  }, []);

  useEffect(() => {
    const requireAuthentication = () => {
      clearAuthorizedFunds();
      queryClient.clear();
    };
    window.addEventListener(AUTHENTICATION_REQUIRED_EVENT, requireAuthentication);
    return () => window.removeEventListener(AUTHENTICATION_REQUIRED_EVENT, requireAuthentication);
  }, [queryClient]);

  const value = useMemo<AuthContextValue>(() => ({
    mode: AUTH_MODE,
    status: state.status,
    session: state.session,
    userId: state.userId,
    email: state.email,
    error: state.error,
    signInWithPassword: async (email, password) => {
      if (AUTH_MODE === "fixture") return unsupported();
      const { data, error } = await getSupabaseBrowserClient().auth.signInWithPassword({ email, password });
      if (error || !data.session) throw error ?? new Error("supabase_sign_in_failed");
      setState(sessionState(data.session));
    },
    signOut: async () => {
      if (AUTH_MODE === "fixture") return unsupported();
      await clearLocalSupabaseSession();
      setState(sessionState(null));
    },
    refreshSession: async () => {
      if (AUTH_MODE === "fixture") return;
      const { data, error } = await getSupabaseBrowserClient().auth.refreshSession();
      if (error || !data.session) throw error ?? new Error("supabase_session_refresh_failed");
      setState(sessionState(data.session));
    },
  }), [state]);

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth_must_be_inside_AuthProvider");
  return value;
}

/** Supabase mode는 로그인되지 않은 화면을 데이터 요청까지 통과시키지 않는다. */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const auth = useAuth();
  if (AUTH_MODE === "fixture" || pathname === "/login") return children;
  if (auth.status === "loading") {
    return <main className="min-h-screen grid place-items-center bg-surface px-6 text-on-surface">인증 세션을 확인하는 중입니다…</main>;
  }
  if (auth.status !== "authenticated") {
    return <main className="min-h-screen grid place-items-center bg-surface px-6 text-on-surface"><div className="text-center"><p className="text-body-md">로그인이 필요합니다.</p><a className="text-primary underline" href="/login">로그인으로 이동</a>{auth.error ? <p className="mt-2 text-body-sm text-error">인증 상태를 확인하지 못했습니다.</p> : null}</div></main>;
  }
  return children;
}
