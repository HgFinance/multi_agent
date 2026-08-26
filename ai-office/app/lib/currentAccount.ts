/**
 * AI Office 화면과 로컬 BFF 통신에 사용하는 고정 데모 계정.
 *
 * `X-User-Id`는 서명도 만료도 없는 평범한 헤더다. 누구나 아무 UUID나 보낼 수
 * 있으므로 공개 배포의 사용자 식별 수단으로 사용하지 않는다. 폐쇄망 데모와
 * 결정론 테스트 전제다.
 *
 * 실제 Fund·Book 데이터는 `/ui/me`가 보강한다.
 *
 * ## 왜 계정이 하나로 고정됐나 (2026-08-19)
 *
 * 계정 전환 UI가 있던 시절에는 선택값을 `localStorage`에 저장하고
 * `useSyncExternalStore`로 읽었다. 서버 렌더는 항상 기본 계정을 그리고
 * 브라우저는 저장된 값을 그려서, 사용자가 계정을 한 번이라도 바꾼 브라우저에서는
 * 매번 hydration mismatch가 났다(서버가 모르는 값을 클라이언트가 즉시 아는
 * 전형적인 패턴 - React 공식 문서가 hydration mismatch 사례로 꼽는 것과 같다).
 *
 * 계정 전환 계획이 없어졌으므로 그 저장·구독 배선을 통째로 없앤다. 이제
 * `readStoredAccount()`는 코드에 고정된 같은 fixture만 돌려준다. 빌드 환경이나
 * 브라우저 상태로 사용자를 교체하는 경로는 없다.
 */

import { FIXED_DEMO_FUND_ID, FIXED_DEMO_USER_ID } from "./demoIdentity";

export interface TestAccount {
  /** `governance.user_profiles.user_id`. 실 DB에 이미 있는 값이다. */
  userId: string;
  /** 표시 이름. DB의 `display_name`을 짧게 줄인 것이다. */
  label: string;
  /**
   * 이 사용자의 Fund. `accounting.funds`에 실제 행이 있다(2026-08-13 추가).
   * 서버에 `user_id -> fund_id` 역참조가 없다는 사실은 그대로라 여전히 화면이
   * 짝을 들고 다닌다.
   */
  fundId: string | null;
  /**
   * 아바타 색. 임의 hex가 아니라 `globals.css @theme`의 색 토큰 이름이다
   * (`bg-primary` 등으로 쓰인다). 이름·이니셜 없이 색으로만 구별한다.
   */
  colorClass: string;
}

export const DEFAULT_ACCOUNT: TestAccount = {
  userId: FIXED_DEMO_USER_ID,
  label: "Fund Owner",
  fundId: FIXED_DEMO_FUND_ID,
  colorClass: "bg-primary",
};

/**
 * 계정을 항상 코드에 고정한 값으로 준다. 인자는 시그니처 호환용으로만 남아 있다
 * (`PortfolioSessionProvider.tsx`가 여전히 값을 넘겨 부른다) - 계정이 하나뿐이므로
 * "찾는다"는 개념 자체가 없다. 호출부가 전달하는 값과 무관하게 고정 계정을
 * 반환한다.
 */
export function accountFor(userId?: string | null): TestAccount {
  void userId;
  return DEFAULT_ACCOUNT;
}

/** 환경에서 정한 고정 계정을 그대로 준다. 서버·클라이언트 어디서 불러도 같은 값이다. */
export function readStoredAccount(): TestAccount {
  return DEFAULT_ACCOUNT;
}

/**
 * `readStoredAccount().userId`. 문자열을 돌려주는 이유는 과거
 * `useSyncExternalStore`용 스냅샷 계약을 그대로 유지해 호출부를 안 건드리기
 * 위해서다 - 지금은 상수라 구독이 필요 없다.
 */
export function readStoredAccountId(): string {
  return DEFAULT_ACCOUNT.userId;
}
