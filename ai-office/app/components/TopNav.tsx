"use client";

import Link from "next/link";
import { COMPANY } from "../../company.config";
import { DEFAULT_ACCOUNT, type TestAccount } from "../lib/currentAccount";
import { fixtureAuthEnabled } from "../lib/authMode";
import { useAuth } from "../lib/AuthProvider";
import { usePortfolioSession } from "../lib/PortfolioSessionProvider";

/**
 * 상단 네비게이션. DESIGN.md 토큰만 쓰고 색·간격을 직접 박지 않는다.
 *
 * 아직 화면이 없는 항목은 링크가 아니라 disabled 버튼으로 둔다.
 * 연결 안 된 걸 연결된 것처럼 보이지 않게 하는 게 이 앱의 원칙이다.
 *
 * `"use client"`인 이유: fixture 모드에서 `useAuth`/`usePortfolioSession` 훅과
 * 오른쪽 세션 메뉴(`ProductionSessionMenu`)가 브라우저 상태를 쓴다.
 *
 * **계정 표시는 로그인이 아니다.** 근거는 `app/lib/currentAccount.ts` 머리말에
 * 적어뒀다 - 요약하면 `X-User-Id`는 서명이 없어 신원을 증명하지 않는다.
 * 계정이 Fund Owner 하나로 고정돼(2026-08-19) 전환 UI는 없다.
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
  return (
    <div
      className="flex items-center gap-2 rounded-full p-1 pr-3"
      aria-label={`현재 계정: ${DEFAULT_ACCOUNT.label}`}
      title={DEFAULT_ACCOUNT.label}
    >
      <AccountDot account={DEFAULT_ACCOUNT} size="md" />
      <span className="hidden text-label-md font-medium text-on-surface-variant lg:inline">
        {DEFAULT_ACCOUNT.label}
      </span>
    </div>
  );
}

function ProductionSessionMenu() {
  const auth = useAuth();
  const portfolio = usePortfolioSession();
  const funds = portfolio.profile?.funds ?? [];
  const label = portfolio.profile?.displayName || auth.email || "Authenticated user";

  return (
    <div className="flex min-w-0 items-center gap-2">
      <div className="hidden min-w-0 text-right lg:block">
        <p className="truncate text-label-md font-bold text-on-surface">{label}</p>
        <p className="truncate text-[10px] text-on-surface-variant">
          {portfolio.loading
            ? "권한 확인 중"
            : portfolio.profile?.onboardingRequired
              ? "펀드 권한 설정 필요"
              : "Supabase session"}
        </p>
      </div>
      {funds.length > 0 ? (
        <select
          aria-label="허가된 펀드 선택"
          value={portfolio.activeFundId ?? funds[0].fundId}
          onChange={(event) => portfolio.selectFund(event.target.value)}
          className="max-w-44 rounded-md border border-outline-variant bg-surface-container-lowest px-2 py-1.5 text-label-md text-on-surface"
        >
          {funds.map((fund) => (
            <option key={fund.fundId} value={fund.fundId}>
              {fund.fundId.slice(0, 8)} · {fund.roles.join(", ") || "MEMBER"}
            </option>
          ))}
        </select>
      ) : null}
      <button
        type="button"
        onClick={() => void auth.signOut()}
        className="rounded-md border border-outline-variant px-2.5 py-1.5 text-label-md font-semibold text-secondary hover:bg-surface-container"
      >
        로그아웃
      </button>
    </div>
  );
}

function IdentityControls() {
  return fixtureAuthEnabled ? <FixedAccountBadge /> : <ProductionSessionMenu />;
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
      <IdentityControls />
    </nav>
  );
}
