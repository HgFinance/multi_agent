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
 * 현재 사용자 프로필. `/ui/me`가 우선이고, 실패하면 고정 계정으로 떨어진다.
 *
 * `fixtureAuthEnabled` 분기를 없앴다(2026-08-19). 그 플래그는
 * `NEXT_PUBLIC_AUTH_MODE` 환경변수에서 나오는데, SSR과 클라이언트 번들이 env를
 * 따로 받는 구조(vite.config.ts) 탓에 브라우저에서 `false`로 평가돼 fallback
 * 경로가 통째로 막혔다 - `/ui/me`가 실패하면 그대로 throw 돼서 화면이
 * "계정에 연결된 Fund가 없습니다"에서 멈췄다.
 *
 * 서버 응답이 여전히 canonical이다. fallback은 PAPER 주문 권한을 만들지 않는다
 * (`books: []`) - 거래 권한은 서버 투영으로만 생긴다.
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
  // 고정 계정을 직접 쓴다 - `useAuth()`의 Supabase 세션 상태에 걸지 않는다.
  // 그 상태는 실제 인증을 안 붙이는 지금 `authenticated`가 될 수 없어, 조회가
  // 영영 `enabled: false`로 남고 프로필이 `null`인 채 멈췄다(2026-08-19).
  const userId = DEFAULT_ACCOUNT.userId;
  const query = useQuery({
    queryKey: ["portfolio-current-user", userId],
    queryFn: () => fetchCurrentUser(userId),
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
  // 로딩·에러·온보딩 게이트 화면을 없앴다(2026-08-19). 셋 다 `auth.status`가
  // `authenticated`인지에 걸려 있었는데, 실제 인증을 붙이지 않는 지금 그 값은
  // 그 상태가 될 수 없어 화면이 멈추거나 반대로 영영 안 뜨는 분기였다. 조회
  // 실패는 이제 각 화면이 자기 맥락에서 알린다.
  return <PortfolioSessionContext.Provider value={value}>{children}</PortfolioSessionContext.Provider>;
}

export function usePortfolioSession(): PortfolioSessionValue {
  const value = useContext(PortfolioSessionContext);
  if (!value) throw new Error("usePortfolioSession_must_be_inside_provider");
  return value;
}
