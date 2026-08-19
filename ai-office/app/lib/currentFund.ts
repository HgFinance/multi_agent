import { readStoredAccount } from "./currentAccount";

export const PORTFOLIO_SCOPE_CHANGED_EVENT = "hgfinance:portfolio-scope-changed";

const STORAGE_PREFIX = "hgfinance.activeFund.v1.";
let activeUserId: string | null = null;
let activeFundId: string | null = null;
let authorizedFundIds = new Set<string>();

function storageKey(userId: string): string {
  return `${STORAGE_PREFIX}${userId}`;
}

function emitScopeChange(): void {
  if (typeof window !== "undefined") {
    window.dispatchEvent(new Event(PORTFOLIO_SCOPE_CHANGED_EVENT));
  }
}

export function configureAuthorizedFunds(userId: string, fundIds: readonly string[]): string | null {
  const normalized = fundIds.map((value) => value.trim()).filter(Boolean);
  authorizedFundIds = new Set(normalized);
  activeUserId = userId;

  let preferred: string | null = null;
  if (typeof window !== "undefined") {
    try {
      preferred = window.localStorage.getItem(storageKey(userId));
    } catch {
      preferred = null;
    }
  }
  const next = preferred && authorizedFundIds.has(preferred) ? preferred : normalized[0] ?? null;
  const changed = activeFundId !== next;
  activeFundId = next;
  if (next && typeof window !== "undefined") {
    try {
      window.localStorage.setItem(storageKey(userId), next);
    } catch {
      // A blocked localStorage only loses the preference, never authorization.
    }
  }
  if (changed) emitScopeChange();
  return activeFundId;
}

export function selectAuthorizedFund(fundId: string): void {
  if (!activeUserId || !authorizedFundIds.has(fundId)) {
    throw new Error("fund_not_authorized_for_current_session");
  }
  if (activeFundId === fundId) return;
  activeFundId = fundId;
  if (typeof window !== "undefined") {
    try {
      window.localStorage.setItem(storageKey(activeUserId), fundId);
    } catch {
      // The in-memory selection remains valid for this tab.
    }
  }
  emitScopeChange();
}

export function clearAuthorizedFunds(): void {
  const changed = activeFundId !== null || activeUserId !== null;
  activeUserId = null;
  activeFundId = null;
  authorizedFundIds = new Set();
  if (changed) emitScopeChange();
}

export function currentFundId(): string | undefined {
  // Once `/ui/me` has projected the authorized funds, that server-backed
  // selection is canonical. The hard-coded fixed account fund is only an
  // offline bootstrap fallback; preferring it here could route a PAPER
  // command to a fund that has no authorized trading book.
  if (activeFundId) return activeFundId;
  // `fixtureAuthEnabled` 분기를 없앴다(2026-08-19) - 실제 Supabase 인증을
  // 붙이지 않기로 했으므로, 이 값이 `undefined`일 이유가 없다. 예전에는 이
  // 판정이 `NEXT_PUBLIC_AUTH_MODE` 환경변수(`authMode.ts`)에 걸려 있었는데,
  // 그 값이 SSR과 클라이언트 번들에서 다르게 읽혀(vite.config.ts의 Cloudflare
  // Worker/Vite 클라이언트 분리 구조 때문) 계정을 계속 못 찾는 상태가 됐다
  // ("계정에 연결된 Fund가 없습니다"가 반복해서 뜬 원인). 고정 계정 하나뿐이니
  // 조건 없이 그 계정의 fundId를 쓴다.
  return readStoredAccount().fundId ?? undefined;
}

export function readCurrentFundId(): string {
  return currentFundId() ?? "";
}

export function subscribeToPortfolioScope(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  window.addEventListener(PORTFOLIO_SCOPE_CHANGED_EVENT, onChange);
  return () => window.removeEventListener(PORTFOLIO_SCOPE_CHANGED_EVENT, onChange);
}
