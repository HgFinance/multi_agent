"use client";

import { useQuery } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useSyncExternalStore } from "react";
import { bffFetch } from "./bffClient";
import { DEFAULT_ACCOUNT, accountFor } from "./currentAccount";
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

/**
 * 현재 데모 계정 프로필. `/ui/me`가 우선이고, 실패하면 고정 계정으로 떨어진다.
 *
 * 서버 응답이 여전히 canonical이다. 로컬 fixture에서는 서버가 고정된 PAPER
 * book을 투영하고, 제어 DB가 없을 때만 읽기 전용 fallback으로 내려간다.
 */
async function fetchCurrentUser(userId: string): Promise<CurrentUserProfile> {
  try {
    const response = await bffFetch("/ui/me", { headers: { Accept: "application/json" } });
    if (response.ok) {
      const body: unknown = await response.json().catch(() => null);
      const projected = parseCurrentUser(body);
      if (projected.userId !== userId) throw new Error("current_user_subject_mismatch");
      return projected;
    }
    if (response.status === 401) throw new Error("portfolio_current_user_unavailable");
  } catch (error) {
    if (error instanceof Error && error.message === "current_user_subject_mismatch") throw error;
    // 제어 DB 없이 도는 로컬 데모를 허용한다. 아래 fallback은 읽기 전용이고
    // book 식별자를 지어내지 않는다.
  }
  const account = accountFor(userId);
  return {
    schemaVersion: "portfolio.current-user.v1",
    userId: account.userId,
    displayName: account.label,
    status: "ACTIVE",
    // 고정 계정만으로 거래 권한을 만들지 않는다.
    funds: account.fundId ? [{ fundId: account.fundId, roles: ["OWNER"], books: [] }] : [],
    onboardingRequired: !account.fundId,
  };
}

export function PortfolioSessionProvider({ children }: { children: React.ReactNode }) {
  const userId = DEFAULT_ACCOUNT.userId;
  const query = useQuery({
    queryKey: ["portfolio-current-user", userId],
    queryFn: () => fetchCurrentUser(userId),
    enabled: Boolean(userId),
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
  // 프로필 조회 실패는 각 화면이 자기 맥락에서 알린다. 데모 화면 자체는
  // 프로필 API가 없어도 계속 렌더링한다.
  return <PortfolioSessionContext.Provider value={value}>{children}</PortfolioSessionContext.Provider>;
}

export function usePortfolioSession(): PortfolioSessionValue {
  const value = useContext(PortfolioSessionContext);
  if (!value) throw new Error("usePortfolioSession_must_be_inside_provider");
  return value;
}
