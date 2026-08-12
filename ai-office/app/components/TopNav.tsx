import Link from "next/link";
import { COMPANY } from "../../company.config";

/**
 * 상단 네비게이션. DESIGN.md 토큰만 쓰고 색·간격을 직접 박지 않는다.
 *
 * 아직 화면이 없는 항목은 링크가 아니라 disabled 버튼으로 둔다.
 * 연결 안 된 걸 연결된 것처럼 보이지 않게 하는 게 이 앱의 원칙이다.
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

export default function TopNav({ current }: { current: NavKey }) {
  return (
    <nav className="bg-surface-container-lowest border-b border-outline-variant flex items-center w-full px-margin-mobile md:px-margin-desktop h-16 shrink-0 z-50 font-sans">
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
    </nav>
  );
}
