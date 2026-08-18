"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { COMPANY } from "../../company.config";
import {
  DEFAULT_ACCOUNT,
  TEST_ACCOUNTS,
  type TestAccount,
  accountFor,
  announceAccountChange,
  readStoredAccountId,
  storeAccount,
  subscribeToAccountChange,
} from "../lib/currentAccount";
import { fixtureAuthEnabled } from "../lib/authMode";
import { useAuth } from "../lib/AuthProvider";
import { usePortfolioSession } from "../lib/PortfolioSessionProvider";

/**
 * 상단 네비게이션. DESIGN.md 토큰만 쓰고 색·간격을 직접 박지 않는다.
 *
 * 아직 화면이 없는 항목은 링크가 아니라 disabled 버튼으로 둔다.
 * 연결 안 된 걸 연결된 것처럼 보이지 않게 하는 게 이 앱의 원칙이다.
 *
 * `"use client"`인 이유: 오른쪽 계정 전환 드롭다운이 클릭·키보드·localStorage를
 * 쓴다. 이 컴포넌트는 Link와 정적 라벨뿐이라 클라이언트로 내려도 비용이 없다.
 *
 * **계정 전환은 로그인이 아니다.** 근거는 `app/lib/currentAccount.ts` 머리말에
 * 적어뒀다 - 요약하면 `X-User-Id`는 서명이 없어 신원을 증명하지 않는다.
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

function AccountSwitcher() {
  // 저장된 계정은 `useSyncExternalStore`로 읽는다. 첫 렌더에서 localStorage를
  // 바로 읽으면 서버가 모르는 값이 나와 hydration 불일치가 되는데, 이 훅은
  // 서버 스냅샷(getServerSnapshot)을 따로 받아 그 문제를 없앤다. useEffect +
  // setState로 맞추면 마운트마다 렌더가 한 번 더 돌고 lint가 막는다
  // (react-hooks/set-state-in-effect).
  const storedUserId = useSyncExternalStore(
    subscribeToAccountChange,
    readStoredAccountId,
    () => DEFAULT_ACCOUNT.userId,
  );
  const account = accountFor(storedUserId);
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement | null>(null);

  const select = useCallback((next: TestAccount) => {
    // setState가 없다 - storeAccount + announce가 외부 스토어를 갱신하고
    // useSyncExternalStore가 그 알림으로 리렌더한다(단일 진실 원본).
    storeAccount(next.userId);
    announceAccountChange(next.userId);
    setOpen(false);
  }, []);

  // 바깥 클릭·Esc로 닫는다. 열린 드롭다운이 다른 조작을 가리지 않게.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent) => {
      if (!boxRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div ref={boxRef} className="relative shrink-0">
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        aria-haspopup="menu"
        aria-expanded={open}
        // 색만으로 구별하므로 스크린리더·마우스오버에는 이름을 준다.
        aria-label={`계정 전환 (현재: ${account.label})`}
        title={`계정 전환 (현재: ${account.label})`}
        className="flex items-center rounded-full p-1 transition-colors duration-200 hover:bg-surface-container focus:outline-none focus-visible:ring-2 focus-visible:ring-primary"
      >
        <AccountDot account={account} size="md" />
      </button>

      {open && (
        <div
          role="menu"
          className="absolute right-0 mt-2 min-w-44 bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden"
        >
          {TEST_ACCOUNTS.map((item) => {
            const selected = item.userId === account.userId;
            return (
              <button
                key={item.userId}
                type="button"
                role="menuitemradio"
                aria-checked={selected}
                onClick={() => select(item)}
                className={`w-full flex items-center gap-3 px-3 py-2 text-left text-body-md transition-colors duration-200 hover:bg-surface-container ${
                  selected ? "text-primary font-bold" : "text-secondary"
                }`}
              >
                <AccountDot account={item} size="sm" />
                <span className="whitespace-nowrap">{item.label}</span>
              </button>
            );
          })}
        </div>
      )}
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
  return fixtureAuthEnabled ? <AccountSwitcher /> : <ProductionSessionMenu />;
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
