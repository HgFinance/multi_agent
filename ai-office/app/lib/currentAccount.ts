/**
 * 명시적 local/test fixture 모드에서만 쓰는 고정 계정.
 *
 * ## ⚠️ 이건 인증이 아니다
 *
 * `X-User-Id`는 서명도 만료도 없는 평범한 헤더다. 누구나 아무 UUID나 보낼 수
 * 있으므로 **신원을 증명하지 않는다.** 폐쇄망 팀 테스트 전제이며, 공개 배포
 * production에서는 `AuthProvider`와 JWT Bearer 경계가 이 모듈을 사용하지 않는다.
 *
 * 그래서 화면에 "로그인"이라고 쓰지 않는다 — 로그인 기능이 없는데 있는 것처럼
 * 보이면 안 된다. 이 앱의 기존 원칙과 같다(`TopNav.tsx`: 연결 안 된 항목은
 * 링크가 아니라 disabled 버튼).
 *
 * 실제 사용자의 허가된 펀드는 `/ui/me`와 `PortfolioSessionProvider`가 관리한다.
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
 * `readStoredAccount()`는 배포 시 주입된 같은 binding만 돌려준다 - 서버와
 * 클라이언트가 다를 수 있는 브라우저 상태가 없으니 mismatch가 구조적으로 생기지 않는다.
 */

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

/**
 * `DISCORD_ACTOR_MAP`의 첫 유효한 3칸 binding을 프론트 fixture 계정으로 쓴다.
 *
 * Vite는 저장소 루트의 이 변수만 클라이언트 번들에 주입한다(`vite.config.ts`).
 * Discord ID와 UUID는 fixture 식별자라 비밀값이 아니지만, 실제 인증 정보는
 * 절대 이 경로로 노출하지 않는다.
 */
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const DISCORD_ID_RE = /^\d{15,25}$/;

const UNCONFIGURED_ACCOUNT: TestAccount = {
  userId: "",
  label: "Fund Owner",
  fundId: null,
  colorClass: "bg-primary",
};

export function accountFromDiscordActorMap(raw: string | undefined): TestAccount {
  for (const entry of (raw ?? "").split(/[\s,]+/)) {
    const [discordId, userId, fundId] = entry.split(":").map((value) => value.trim());
    if (
      DISCORD_ID_RE.test(discordId ?? "")
      && UUID_RE.test(userId ?? "")
      && UUID_RE.test(fundId ?? "")
    ) {
      return {
        userId,
        label: "Fund Owner",
        fundId,
        colorClass: "bg-primary",
      };
    }
  }
  return UNCONFIGURED_ACCOUNT;
}

export const DEFAULT_ACCOUNT = accountFromDiscordActorMap(process.env.DISCORD_ACTOR_MAP);

/**
 * 계정을 항상 환경에서 정한 고정값으로 준다. 인자는 시그니처 호환용으로만 남아 있다
 * (`PortfolioSessionProvider.tsx`가 여전히 값을 넘겨 부른다) - 계정이 하나뿐이므로
 * "찾는다"는 개념 자체가 없다. Supabase 세션이 이 uuid와 다른 값을 들고 있어도
 * (비-fixture 경로에서는 애초에 호출되지 않는다) 조용히 고정 계정으로 떨어진다.
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
