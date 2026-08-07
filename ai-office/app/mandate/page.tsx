"use client";

import { BffProvider } from "../ops/bffClient";
import { MandateConfigView } from "../page";

/**
 * Mandate 설정은 초기 오피스 Projection hydration과 독립적으로 열려야 한다.
 * 설정 화면 자체는 기존 advisory-only 컴포넌트와 동일한 BFF 경계를 사용한다.
 */
export default function MandatePage() {
  return (
    <main className="page-shell">
      <div className="wrap">
        <BffProvider>
          <MandateConfigView onAnalyzed={() => undefined} />
        </BffProvider>
      </div>
    </main>
  );
}
