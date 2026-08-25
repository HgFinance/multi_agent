/**
 * Hermes Kanban 임베드 주소 계산.
 *
 * 화면(`app/dashboard/DashboardView.tsx`)이 아니라 여기 있는 이유는
 * `mandateClient.ts`와 같다 - 테스트 러너(`node --experimental-strip-types`)가
 * JSX를 파싱하지 못해서, `.tsx`에 두면 검증할 방법이 없다. 이 함수는 URL을
 * 해석하고 거부하는 판정을 하므로 검증 없이 두면 안 된다.
 */

/** Hermes Kanban 보드 주소. 로컬 기본값은 Hermes Dashboard 포트다. */
export const KANBAN_BASE_URL =
  process.env.NEXT_PUBLIC_HERMES_KANBAN_URL?.trim() ||
  process.env.NEXT_PUBLIC_HERMES_DASHBOARD_URL?.trim() ||
  "http://127.0.0.1:9119";

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "[::1]", "::1"]);

/**
 * 임베드할 보드 주소. `/kanban`으로 고정하고, **host를 이 페이지에 맞춘다.**
 *
 * ## 왜 host를 맞추는가 (iframe 안에서 보드 상태가 유지되지 않던 원인)
 *
 * Hermes 세션 쿠키는 `SameSite=Lax` 고정이다(설치본
 * `hermes_cli/dashboard_auth/cookies.py`의 `_common_attrs`, 설정으로 바꿀 수
 * 없다). Lax 쿠키는 **cross-site iframe에 저장·전송되지 않는다.** 그래서
 * 페이지가 `localhost:3002`인데 보드가 `127.0.0.1:9119`면 - 브라우저에게 이
 * 둘은 서로 다른 site라 브라우저가 보드의 SameSite 쿠키를 iframe에 유지하지
 * 않는다. 그 결과 보드가 매번 초기 상태로 돌아간다.
 *
 * SameSite 판정에 **포트는 들어가지 않는다.** host만 같으면 3002↔9119도 같은
 * site라 쿠키가 그대로 흐른다. 그래서 포트는 두고 host만 바꾼다.
 *
 * 둘 다 loopback일 때만 바꾼다 - 페이지가 LAN IP나 실제 도메인이면 그 host에
 * Hermes가 떠 있다는 보장이 없어서 설정값을 그대로 둔다. 그 배포에서는
 * 운영자가 `NEXT_PUBLIC_HERMES_KANBAN_URL`을 같은 host로 맞춰야 한다.
 *
 * 거부 조건(자격증명·query·hash·`/kanban` 외 경로)은 그대로 유지한다 - 설정값
 * 하나로 임의 주소를 이 화면에 임베드하지 못하게 막는 자리다.
 */
export function resolveKanbanUrl(value: string, pageHost?: string): string | null {
  try {
    const url = new URL(value);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
      return null;
    }
    const pathname = url.pathname.replace(/\/+$/, "");
    if (pathname && pathname !== "/kanban") return null;
    // 임베드 시 기본 화면은 항상 보드여야 한다.
    url.pathname = "/kanban";
    if (pageHost && pageHost !== url.hostname && LOOPBACK_HOSTS.has(pageHost) && LOOPBACK_HOSTS.has(url.hostname)) {
      url.hostname = pageHost;
    }
    return url.toString();
  } catch {
    return null;
  }
}
