"use client";

import { useQuery } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useSyncExternalStore } from "react";
import { fixtureAuthEnabled } from "./authMode";
import { useAuth } from "./AuthProvider";
import { bffFetch } from "./bffClient";
import { accountFor } from "./currentAccount";
import {
  clearAuthorizedFunds,
  configureAuthorizedFunds,
  readCurrentFundId,
  selectAuthorizedFund,
  subscribeToPortfolioScope,
} from "./currentFund";
import { parseCurrentUser, type CurrentUserProfile } from "./currentUserContract";

interface PortfolioSessionValue {
  profile: CurrentUserProfile | null;
  activeFundId: string | null;
  loading: boolean;
  error: string | null;
  selectFund(fundId: string): void;
}

const PortfolioSessionContext = createContext<PortfolioSessionValue | null>(null);

async function fetchCurrentUser(userId: string): Promise<CurrentUserProfile> {
  if (fixtureAuthEnabled) {
    // Prefer the BFF projection when a local control DB is available. Only that
    // response can grant PAPER books; the offline demo fallback remains
    // intentionally read-only and never invents a book identifier.
    try {
      const response = await bffFetch("/ui/me", { headers: { Accept: "application/json" } });
      if (response.ok) {
        const body: unknown = await response.json().catch(() => null);
        const projected = parseCurrentUser(body);
        if (projected.userId !== userId) throw new Error("current_user_subject_mismatch");
        return projected;
      }
    } catch (error) {
      if (error instanceof Error && error.message === "current_user_subject_mismatch") throw error;
      // Fixture/demo may intentionally run without a control DB. Continue with
      // a no-book profile, which cannot submit a PAPER command.
    }
    const account = accountFor(userId);
    return {
      schemaVersion: "portfolio.current-user.v1",
      userId: account.userId,
      displayName: account.label,
      status: "ACTIVE",
      // Fixture identity alone never invents trading authority.
      funds: account.fundId ? [{ fundId: account.fundId, roles: ["OWNER"], books: [] }] : [],
      onboardingRequired: !account.fundId,
    };
  }
  const response = await bffFetch("/ui/me", { headers: { Accept: "application/json" } });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(`current_user_failed_http_${response.status}`);
  const profile = parseCurrentUser(body);
  if (profile.userId !== userId) throw new Error("current_user_subject_mismatch");
  return profile;
}

export function PortfolioSessionProvider({ children }: { children: React.ReactNode }) {
  const auth = useAuth();
  const query = useQuery({
    queryKey: ["portfolio-current-user", auth.userId],
    queryFn: () => fetchCurrentUser(auth.userId as string),
    enabled: auth.status === "authenticated" && Boolean(auth.userId),
    retry: false,
  });
  const profile = query.data ?? null;
  const configuredFundId = useSyncExternalStore(
    subscribeToPortfolioScope,
    readCurrentFundId,
    () => "",
  );

  useEffect(() => {
    if (!profile) {
      clearAuthorizedFunds();
      return;
    }
    configureAuthorizedFunds(profile.userId, profile.funds.map((fund) => fund.fundId));
  }, [profile]);

  const activeFundId = profile
    ? profile.funds.some((fund) => fund.fundId === configuredFundId)
      ? configuredFundId
      : profile.funds[0]?.fundId ?? null
    : null;

  const value = useMemo<PortfolioSessionValue>(
    () => ({
      profile,
      activeFundId,
      loading: query.isLoading,
      error: query.error instanceof Error ? query.error.message : query.error ? String(query.error) : null,
      selectFund: selectAuthorizedFund,
    }),
    [activeFundId, profile, query.error, query.isLoading],
  );
  let content = children;
  if (auth.status === "authenticated" && (query.isLoading || (profile && profile.funds.length > 0 && !activeFundId))) {
    content = (
      <main className="min-h-screen grid place-items-center bg-surface px-6 text-on-surface">
        <p className="text-body-md text-on-surface-variant">사용자와 펀드 권한을 확인하는 중입니다.</p>
      </main>
    );
  } else if (auth.status === "authenticated" && query.error) {
    content = (
      <main className="min-h-screen grid place-items-center bg-surface px-6 text-on-surface">
        <section className="max-w-lg rounded-lg border border-error bg-error-container p-6 text-on-error-container">
          <h1 className="text-headline-md font-bold">사용자 권한을 확인할 수 없습니다</h1>
          <p className="mt-3 text-body-md">{value.error}</p>
          <button type="button" onClick={() => void auth.signOut()} className="mt-5 rounded-md border border-current px-3 py-2 font-bold">
            로그아웃
          </button>
        </section>
      </main>
    );
  } else if (auth.status === "authenticated" && profile?.onboardingRequired) {
    content = (
      <main className="min-h-screen grid place-items-center bg-surface px-6 text-on-surface">
        <section className="max-w-lg rounded-lg border border-outline-variant bg-surface-container-lowest p-6">
          <h1 className="text-headline-md font-bold text-primary">펀드 접근 권한 설정이 필요합니다</h1>
          <p className="mt-3 text-body-md text-on-surface-variant">
            계정은 활성화됐지만 유효한 Fund membership이 없습니다. 관리자가 권한을 부여한 뒤 다시 로그인하세요.
          </p>
          <button type="button" onClick={() => void auth.signOut()} className="mt-5 rounded-md border border-outline-variant px-3 py-2 font-bold text-secondary">
            로그아웃
          </button>
        </section>
      </main>
    );
  }
  return <PortfolioSessionContext.Provider value={value}>{content}</PortfolioSessionContext.Provider>;
}

export function usePortfolioSession(): PortfolioSessionValue {
  const value = useContext(PortfolioSessionContext);
  if (!value) throw new Error("usePortfolioSession_must_be_inside_provider");
  return value;
}
