"use client";

import type { HealthState } from "../lib/useBffHealth";

/**
 * 연결 상태 표시. 네 상태 모두 실제 확인 결과이며 기본값이 초록이 아니다.
 *
 * `unknown`은 "브라우저에서 확인할 수 없는 대상"이다. 예를 들어 Hermes 보드는
 * 다른 오리진이라 fetch가 CORS로 막혀, 살아 있어도 실패로 보인다. 그 경우
 * 초록도 빨강도 거짓이라 회색으로 둔다.
 */

export type BadgeState = HealthState | "unknown" | "local";

const VIEW: Record<BadgeState, { dot: string; wrap: string; pulse: boolean }> = {
  checking: { dot: "bg-outline", wrap: "border-outline-variant bg-surface-container text-on-surface-variant", pulse: true },
  online: { dot: "bg-tertiary-fixed-dim", wrap: "border-outline-variant bg-surface-container text-on-surface-variant", pulse: false },
  degraded: { dot: "bg-[#e0a020]", wrap: "border-[#e8cf9f] bg-[#fff4e0] text-[#8a5a00]", pulse: false },
  offline: { dot: "bg-error", wrap: "border-error/40 bg-error-container text-on-error-container", pulse: false },
  unknown: { dot: "bg-outline-variant", wrap: "border-outline-variant bg-surface-container text-on-surface-variant", pulse: false },
  local: { dot: "bg-outline-variant", wrap: "border-outline-variant bg-surface-container text-on-surface-variant", pulse: false },
};

/** 점만 필요한 자리(패널 헤더 옆 등). */
export function ConnectionDot({ state, title }: { state: BadgeState; title?: string }) {
  const view = VIEW[state];
  return (
    <span
      className={`w-2 h-2 rounded-full shrink-0 ${view.dot} ${view.pulse ? "animate-pulse" : ""}`}
      title={title}
      aria-hidden="true"
    />
  );
}

export default function ConnectionBadge({
  state,
  label,
  title,
}: {
  state: BadgeState;
  label: string;
  title?: string;
}) {
  const view = VIEW[state];
  return (
    <span
      className={`inline-flex items-center gap-2 px-3 py-1 rounded-full border text-xs font-medium shrink-0 ${view.wrap}`}
      title={title}
      role="status"
    >
      <ConnectionDot state={state} />
      {label}
    </span>
  );
}
