/**
 * 계정 전환 — 어느 테스트 유저로 BFF를 부를지 한 곳에서 정한다.
 *
 * ## ⚠️ 이건 인증이 아니다
 *
 * `X-User-Id`는 서명도 만료도 없는 평범한 헤더다. 누구나 아무 UUID나 보낼 수
 * 있으므로 **신원을 증명하지 않는다.** 폐쇄망 팀 테스트 전제이며, 공개 배포
 * 전에 실제 인증으로 교체해야 한다(서버 쪽 근거: `apps/api/current_user.py`).
 *
 * 그래서 화면에 "로그인"이라고 쓰지 않는다 — 로그인 기능이 없는데 있는 것처럼
 * 보이면 안 된다. 이 앱의 기존 원칙과 같다(`TopNav.tsx`: 연결 안 된 항목은
 * 링크가 아니라 disabled 버튼).
 *
 * ## 왜 fund_id를 화면이 들고 있나
 *
 * 서버에 `user_id -> fund_id` 역참조 경로가 없다
 * (`governance.fund_memberships`가 비어 있다). 그래서 CEO 질의처럼 Mandate가
 * 필요한 호출은 화면이 `fund_id`를 쌍으로 보낸다. 진짜 로그인이 붙으면 이
 * 매핑은 서버로 옮긴다.
 *
 * ## 헤더를 여기서만 만드는 이유
 *
 * 호출부마다 헤더를 붙이면 한 곳을 빠뜨렸을 때 그 경로만 다른 사용자로 나간다.
 * `withAccountHeaders()`를 거치게 두면 교체 지점이 하나다.
 */

export interface TestAccount {
  /** `governance.user_profiles.user_id`. 실 DB에 이미 있는 값이다. */
  userId: string;
  /** 드롭다운에 보이는 이름. DB의 `display_name`을 짧게 줄인 것이다. */
  label: string;
  /**
   * 이 사용자의 Fund. 세 계정 모두 `accounting.funds`에 실제 행이 있다(2026-08-13
   * 추가) - `null`을 남겨두는 계정은 이제 없다. 서버에 `user_id -> fund_id`
   * 역참조가 없다는 사실은 그대로라 여전히 화면이 짝을 들고 다닌다.
   */
  fundId: string | null;
  /**
   * 아바타 색. 임의 hex가 아니라 `globals.css @theme`의 색 토큰 이름이다
   * (`bg-primary` 등으로 쓰인다). 이름·이니셜 없이 색으로만 구별한다.
   */
  colorClass: string;
}

/**
 * 하드코딩 테스트 유저 3명.
 *
 * `supabase/seed.sql`이 심은 플레이스홀더 회원과 같은 UUID다 — 여기서 값을
 * 바꾸면 서버가 모르는 사용자가 되어 `POST /ui/investor-profiles`가 FK 위반으로
 * 실패한다. seed와 함께 고쳐야 한다.
 */
export const TEST_ACCOUNTS: readonly TestAccount[] = [
  {
    userId: "00000000-0000-4000-8000-00000000cec0",
    label: "Fund Owner",
    // TEST-CEO-MANDATE (KRW). 유일하게 활성 Mandate(v11, ACTIVE)를 가진 계정.
    fundId: "b13f5cd1-5df0-4025-92cf-9be03b1a0296",
    colorClass: "bg-primary",
  },
  {
    userId: "00000000-0000-4000-8000-00000000cec1",
    label: "User 2",
    // TEST-USER2-MANDATE (KRW). Fund는 있고 Mandate는 아직 제출 전(DRAFT).
    fundId: "50a3c28c-6cee-4bcf-ab07-fa97093dca8e",
    colorClass: "bg-tertiary-container",
  },
  {
    userId: "00000000-0000-4000-8000-00000000cec2",
    label: "User 3",
    // TEST-USER3-MANDATE (KRW). Fund는 있고 Mandate는 아직 제출 전(DRAFT).
    fundId: "3838f7d6-0c7c-4e54-85f3-316a451e7eeb",
    colorClass: "bg-secondary",
  },
] as const;

const STORAGE_KEY = "hgfinance.currentAccount.v1";

/** 기본값은 목록 첫 계정(Mandate를 가진 유일한 계정)이다. */
export const DEFAULT_ACCOUNT = TEST_ACCOUNTS[0];

export function accountFor(userId: string | null | undefined): TestAccount {
  return TEST_ACCOUNTS.find((account) => account.userId === userId) ?? DEFAULT_ACCOUNT;
}

/**
 * 저장된 선택을 읽는다. 서버 렌더링 중이거나 저장값이 목록에 없으면 기본값.
 *
 * 목록에 없는 값을 그대로 쓰지 않는 이유: 계정 목록을 줄였을 때 사라진 UUID가
 * 계속 헤더로 나가면 서버는 그 사용자를 모르고, 화면은 아무 계정도 선택되지
 * 않은 상태로 보인다.
 */
export function readStoredAccount(): TestAccount {
  if (typeof window === "undefined") return DEFAULT_ACCOUNT;
  try {
    return accountFor(window.localStorage.getItem(STORAGE_KEY));
  } catch {
    // Safari 사생활 보호 모드 등에서 localStorage 접근이 던진다. 저장을 못 하는
    // 것이 화면을 멈출 이유는 아니다.
    return DEFAULT_ACCOUNT;
  }
}

export function storeAccount(userId: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(STORAGE_KEY, userId);
  } catch {
    /* 저장 실패는 무시한다 - 이번 세션에만 선택이 유지된다. */
  }
}

/**
 * 같은 탭 안에서 계정 변경을 알린다.
 *
 * `storage` 이벤트는 **다른** 탭에서만 발생하므로 같은 탭의 다른 컴포넌트는 못
 * 받는다. 전역 상태 라이브러리를 새로 들이지 않고 이 한 가지만 해결한다.
 */
export const ACCOUNT_CHANGED_EVENT = "hgfinance:account-changed";

export function announceAccountChange(userId: string): void {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new CustomEvent(ACCOUNT_CHANGED_EVENT, { detail: userId }));
}

/**
 * `useSyncExternalStore`용 구독.
 *
 * 두 이벤트를 함께 듣는다 — `ACCOUNT_CHANGED_EVENT`는 **같은 탭**의 다른
 * 컴포넌트에, `storage`는 **다른 탭**의 변경에 반응한다. 둘 중 하나만 듣으면
 * 한쪽에서 바꾼 계정이 다른 쪽에 반영되지 않는다.
 */
export function subscribeToAccountChange(onChange: () => void): () => void {
  if (typeof window === "undefined") return () => {};
  window.addEventListener(ACCOUNT_CHANGED_EVENT, onChange);
  window.addEventListener("storage", onChange);
  return () => {
    window.removeEventListener(ACCOUNT_CHANGED_EVENT, onChange);
    window.removeEventListener("storage", onChange);
  };
}

/**
 * `useSyncExternalStore`의 `getSnapshot`. **문자열을 돌려준다.**
 *
 * 객체를 돌려주면 매 호출이 새 참조가 되어 React가 무한 렌더로 판단한다.
 * 호출부는 이 id를 `accountFor()`에 넣어 객체를 얻는다.
 */
export function readStoredAccountId(): string {
  return readStoredAccount().userId;
}

/**
 * BFF 호출에 붙일 헤더. **모든 호출이 이 함수를 거친다.**
 *
 * 서버는 `PORTFOLIO_AUTH_REQUIRED`가 켜져 있으면 헤더 없는 요청을 401로 막고,
 * 리소스 소유자와 다르면 403을 낸다(`apps/api/current_user.py`).
 */
export function withAccountHeaders(headers: HeadersInit = {}): HeadersInit {
  return { ...headers, "X-User-Id": readStoredAccount().userId };
}

/** Mandate가 필요한 요청 본문에 실을 `fund_id`. 없으면 `undefined`. */
export function currentFundId(): string | undefined {
  return readStoredAccount().fundId ?? undefined;
}
