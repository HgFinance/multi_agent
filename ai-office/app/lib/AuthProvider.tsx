"use client";

import type { Session } from "@supabase/supabase-js";
import { useQueryClient } from "@tanstack/react-query";
import { usePathname, useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { AUTH_MODE, fixtureAuthEnabled } from "./authMode";
import { AUTHENTICATION_REQUIRED_EVENT } from "./bffClient";
import { readStoredAccountId } from "./currentAccount";
import { clearAuthorizedFunds } from "./currentFund";
import { clearLocalSupabaseSession, getSupabaseBrowserClient } from "./supabaseBrowser";

export type AuthStatus = "loading" | "authenticated" | "unauthenticated" | "error";

interface AuthContextValue {
  mode: typeof AUTH_MODE;
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

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const queryClient = useQueryClient();
  // 계정이 하나로 고정돼(currentAccount.ts) 상수를 반환한다 - 구독이 필요
  // 없다. 예전엔 useSyncExternalStore로 localStorage를 구독했는데, 그 저장값이
  // 서버 렌더와 달라 계정 전환을 한 번이라도 한 브라우저에서 hydration
  // mismatch가 났다(2026-08-19). 상수가 되면서 그 불일치 자체가 없어졌다.
  const fixtureUserId = readStoredAccountId();
  const [session, setSession] = useState<Session | null>(null);
  const [status, setStatus] = useState<AuthStatus>(fixtureAuthEnabled ? "authenticated" : "loading");
  const [error, setError] = useState<string | null>(null);
  const previousSubject = useRef<string | null>(fixtureAuthEnabled ? fixtureUserId : null);

  useEffect(() => {
    if (fixtureAuthEnabled) return undefined;
    let active = true;
    let client: ReturnType<typeof getSupabaseBrowserClient>;
    try {
      client = getSupabaseBrowserClient();
    } catch (cause) {
      const message = cause instanceof Error ? cause.message : String(cause);
      queueMicrotask(() => {
        if (!active) return;
        setError(message);
        setStatus("error");
      });
      return undefined;
    }

    client.auth.getSession().then(({ data, error: sessionError }) => {
      if (!active) return;
      if (sessionError) {
        setError(sessionError.message);
        setStatus("error");
        return;
      }
      setSession(data.session);
      setStatus(data.session ? "authenticated" : "unauthenticated");
    });
    const { data: listener } = client.auth.onAuthStateChange((_event, nextSession) => {
      if (!active) return;
      setSession(nextSession);
      setError(null);
      setStatus(nextSession ? "authenticated" : "unauthenticated");
    });
    return () => {
      active = false;
      listener.subscription.unsubscribe();
    };
  }, []);

  const userId = fixtureAuthEnabled ? fixtureUserId : session?.user.id ?? null;
  const email = fixtureAuthEnabled ? null : session?.user.email ?? null;

  useEffect(() => {
    if (previousSubject.current === userId) return;
    previousSubject.current = userId;
    clearAuthorizedFunds();
    queryClient.clear();
  }, [queryClient, userId]);

  useEffect(() => {
    if (fixtureAuthEnabled) return undefined;
    const requireAuthentication = () => {
      setSession(null);
      setStatus("unauthenticated");
      clearAuthorizedFunds();
      queryClient.clear();
      void clearLocalSupabaseSession().catch((cause) => {
        setError(cause instanceof Error ? cause.message : "supabase_local_session_clear_failed");
      });
    };
    window.addEventListener(AUTHENTICATION_REQUIRED_EVENT, requireAuthentication);
    return () => window.removeEventListener(AUTHENTICATION_REQUIRED_EVENT, requireAuthentication);
  }, [queryClient]);

  const signInWithPassword = useCallback(async (loginEmail: string, password: string) => {
    if (fixtureAuthEnabled) throw new Error("fixture_mode_does_not_support_login");
    setStatus("loading");
    setError(null);
    const result = await getSupabaseBrowserClient().auth.signInWithPassword({
      email: loginEmail.trim(),
      password,
    });
    if (result.error || !result.data.session) {
      setStatus("unauthenticated");
      setError(result.error?.message ?? "supabase_login_failed");
      throw result.error ?? new Error("supabase_login_failed");
    }
    setSession(result.data.session);
    setStatus("authenticated");
  }, []);

  const signOut = useCallback(async () => {
    clearAuthorizedFunds();
    queryClient.clear();
    if (!fixtureAuthEnabled) await getSupabaseBrowserClient().auth.signOut();
    setSession(null);
    setStatus(fixtureAuthEnabled ? "authenticated" : "unauthenticated");
  }, [queryClient]);

  const refreshSession = useCallback(async () => {
    if (fixtureAuthEnabled) return;
    const result = await getSupabaseBrowserClient().auth.refreshSession();
    if (result.error || !result.data.session) {
      setSession(null);
      setStatus("unauthenticated");
      throw result.error ?? new Error("supabase_session_refresh_failed");
    }
    setSession(result.data.session);
    setStatus("authenticated");
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ mode: AUTH_MODE, status, session, userId, email, error, signInWithPassword, signOut, refreshSession }),
    [email, error, refreshSession, session, signInWithPassword, signOut, status, userId],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (!value) throw new Error("useAuth_must_be_inside_AuthProvider");
  return value;
}

export function AuthGate({ children }: { children: React.ReactNode }) {
  const auth = useAuth();
  const pathname = usePathname();
  const router = useRouter();
  const loginRoute = pathname === "/login";

  useEffect(() => {
    if (fixtureAuthEnabled || loginRoute || auth.status === "loading" || auth.status === "error") return;
    if (auth.status === "unauthenticated") {
      const next = pathname && pathname !== "/" ? `?next=${encodeURIComponent(pathname)}` : "";
      router.replace(`/login${next}`);
    }
  }, [auth.status, loginRoute, pathname, router]);

  if (loginRoute || fixtureAuthEnabled) return children;
  if (auth.status === "authenticated") return children;
  return (
    <main className="min-h-screen grid place-items-center bg-surface text-on-surface px-6">
      <section className="max-w-md rounded-lg border border-outline-variant bg-surface-container-lowest p-6 text-center">
        <h1 className="text-headline-md font-bold text-primary">HgFinance 인증</h1>
        <p className="mt-3 text-body-md text-on-surface-variant">
          {auth.status === "error" ? auth.error ?? "인증 설정을 확인할 수 없습니다." : "안전한 세션을 확인하는 중입니다."}
        </p>
      </section>
    </main>
  );
}
