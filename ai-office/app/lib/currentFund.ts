import { fixtureAuthEnabled } from "./authMode";
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
  if (fixtureAuthEnabled) return readStoredAccount().fundId ?? undefined;
  return activeFundId ?? undefined;
}

export function readCurrentFundId(): string {
  return currentFundId() ?? "";
}

export function subscribeToPortfolioScope(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  window.addEventListener(PORTFOLIO_SCOPE_CHANGED_EVENT, onChange);
  return () => window.removeEventListener(PORTFOLIO_SCOPE_CHANGED_EVENT, onChange);
}
