import { readStoredAccount } from "./currentAccount";

export const PORTFOLIO_SCOPE_CHANGED_EVENT = "hgfinance:portfolio-scope-changed";

const STORAGE_PREFIX = "hgfinance.activeFund.v1.";
let activeUserId: string | null = null;
let activeFundId: string | null = null;
let authorizedFundIds = new Set<string>();
// `activeFundId === null` is ambiguous on its own - it means both "`/ui/me`
// hasn't answered yet" (fall back to the fixed account's fund) and "`/ui/me`
// answered with zero funds" (the fixed account's fund is not authorized here
// and must not be used, per the currentFundId() comment below). This flag
// tells the two apart so a real zero-fund answer never gets silently
// overwritten by the offline bootstrap fallback.
let resolved = false;

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
  resolved = true;

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
  resolved = false;
  if (changed) emitScopeChange();
}

export function currentFundId(): string | undefined {
  // Once `/ui/me` has projected the authorized funds, that server-backed
  // selection is canonical. The hard-coded fixed account fund is only an
  // offline bootstrap fallback; preferring it here could route a PAPER
  // command to a fund that has no authorized trading book.
  if (activeFundId) return activeFundId;
  // `/ui/me`가 이미 답했는데(설령 0건이어도) 여기서 고정 계정의 fundId로
  // 떨어지면, 서버가 "이 계정엔 이 Fund 권한이 없다"고 말한 직후에 바로 그
  // Fund로 요청을 보내는 꼴이 된다(2026-08-23 실측: `portfolio_fund_forbidden`
  // 403 - 원인은 로컬 timescaledb의 fixture 데이터 누락이었지만, 이 코드는
  // 그 상황에서도 조용히 하드코딩된 fund로 계속 요청을 보내 오류를 반복시켰다).
  // "아직 답을 못 받음"과 "답을 받았는데 권한이 0건"은 다른 상태라 `resolved`로
  // 가른다.
  if (resolved) return undefined;
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
