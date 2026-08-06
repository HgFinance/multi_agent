"use client";

import { BffProvider } from "../ops/bffClient";
import { DashboardRouteView } from "../page";

/**
 * Dashboard는 초기 픽셀 오피스 hydration과 독립적으로 운영 상태를 열 수 있다.
 */
export default function DashboardPage() {
  return (
    <main className="page-shell">
      <div className="wrap">
        <BffProvider>
          <DashboardRouteView />
        </BffProvider>
      </div>
    </main>
  );
}
