"use client";

import Link from "next/link";
import { COMPANY } from "../../company.config";
import { DEFAULT_ACCOUNT, type TestAccount } from "../lib/currentAccount";

/**
 * 상단 네비게이션. DESIGN.md 토큰만 쓰고 색·간격을 직접 박지 않는다.
 *
 * 아직 화면이 없는 항목은 링크가 아니라 disabled 버튼으로 둔다.
 * 연결 안 된 걸 연결된 것처럼 보이지 않게 하는 게 이 앱의 원칙이다.
 *
 * `"use client"`인 이유: `AccountDot`의 색 토큰 계산과 훗날의 상호작용을 위해
 * 클라이언트 컴포넌트로 둔다.
 *
 * 계정은 데모용 Fund Owner 하나로 고정한다. 사용자 전환 UI나 세션 메뉴는
 * 제공하지 않는다.
 */

export type NavKey = "dashboard" | "ai-office" | "mandate" | "agent-logs";

const ITEMS: { key: NavKey; label: string; href?: string }[] = [
  { key: "mandate", label: "Mandate Configuration", href: "/mandate" },
  { key: "ai-office", label: "AI Office", href: "/" },
  { key: "dashboard", label: "Dashboard", href: "/dashboard" },
  { key: "agent-logs", label: "Agent Logs", href: "/agent-logs" },
];

const BASE = "text-body-md px-3 py-2 transition-colors duration-200";
const IDLE = "text-secondary font-medium rounded hover:bg-surface-container";
const ACTIVE = "text-primary font-bold border-b-2 border-primary rounded-t";

/** 아바타 원. 이름·이니셜 없이 색으로만 구별한다(요구사항). */
function AccountDot({ account, size }: { account: TestAccount; size: "sm" | "md" }) {
  const box = size === "md" ? "w-8 h-8" : "w-5 h-5";
  return (
    <span
      aria-hidden="true"
      className={`${box} ${account.colorClass} rounded-full border border-outline-variant shrink-0`}
    />
  );
}

/**
 * 고정 계정 표시. 전환 UI가 아니다 - 계정이 Fund Owner 하나뿐이라 "전환"할
 * 대상이 없다(2026-08-19). 클릭 가능한 드롭다운을 남겨두면 눌러도 아무 일도
 * 안 일어나는 죽은 UI가 되므로, 정적 표시로 바꿨다.
 */
function FixedAccountBadge() {
  const account: TestAccount = DEFAULT_ACCOUNT;
  return (
    <div
      className="flex items-center gap-2 rounded-full p-1 pr-3"
      aria-label={`현재 계정: ${account.label}`}
      title={account.label}
    >
      <AccountDot account={account} size="md" />
      <span className="hidden text-label-md font-medium text-on-surface-variant lg:inline">
        {account.label}
      </span>
    </div>
  );
}

export default function TopNav({ current }: { current: NavKey }) {
  return (
    <nav className="bg-surface-container-lowest border-b border-outline-variant flex items-center justify-between w-full px-margin-mobile md:px-margin-desktop h-16 shrink-0 z-50 font-sans">
      <div className="flex items-center gap-6">
        <div className="flex items-center gap-2 shrink-0">
          <span className="text-headline-md font-headline-md font-bold text-primary tracking-tight whitespace-nowrap">
            {COMPANY.name}
          </span>
          <span
            className="border border-outline text-secondary rounded-full px-3 py-1 text-label-md font-label-md whitespace-nowrap"
            title="시뮬레이션 데모 화면입니다"
          >
            Demo
          </span>
        </div>
        <div className="hidden md:flex gap-4">
          {ITEMS.map((item) => {
            const cls = `${BASE} ${item.key === current ? ACTIVE : IDLE}`;
            if (!item.href) {
              return (
                <button key={item.key} type="button" disabled title="준비 중" className={`${cls} opacity-45 cursor-not-allowed`}>
                  {item.label}
                </button>
              );
            }
            return (
              <Link
                key={item.key}
                href={item.href}
                aria-current={item.key === current ? "page" : undefined}
                className={cls}
              >
                {item.label}
              </Link>
            );
          })}
        </div>
      </div>
      <FixedAccountBadge />
    </nav>
  );
}
