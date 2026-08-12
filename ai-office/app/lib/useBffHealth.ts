"use client";

import { useEffect, useState } from "react";
import { BFF } from "./ceoClient";

/**
 * BFF 연결 상태를 실제로 확인한다.
 *
 * 화면에 붙어 있던 "CONNECTING" 초록 점은 고정 문자열이라 BFF가 죽어 있어도
 * 똑같이 초록이었다. 여기서는 `/health/ready`가 알려주는 의존성 상태를 그대로
 * 옮긴다 — 추측하지 않고, 못 물어봤으면 못 물어봤다고 둔다.
 */

export type HealthState = "checking" | "online" | "degraded" | "offline";

export type BffHealth = {
  state: HealthState;
  /** 사람이 읽을 사유. 배지 title에 그대로 쓴다. */
  detail: string;
  /** NOT_CONFIGURED·ERROR 인 의존성 이름 */
  notReady: string[];
};

type ReadyResponse = {
  status?: string;
  dependencies?: Record<string, { status?: string }>;
};

const POLL_MS = 15_000;

export function useBffHealth(): BffHealth {
  const [health, setHealth] = useState<BffHealth>({ state: "checking", detail: "BFF 상태를 확인하는 중입니다.", notReady: [] });

  useEffect(() => {
    let alive = true;

    async function probe() {
      try {
        const response = await fetch(`${BFF}/health/ready`, { cache: "no-store", headers: { Accept: "application/json" } });
        const body = (await response.json().catch(() => null)) as ReadyResponse | null;
        if (!alive) return;
        if (!response.ok || !body) {
          setHealth({ state: "offline", detail: `BFF가 오류를 반환했습니다 (HTTP ${response.status}).`, notReady: [] });
          return;
        }
        const notReady = Object.entries(body.dependencies ?? {})
          .filter(([, value]) => String(value?.status ?? "").toUpperCase() !== "READY")
          .map(([name]) => name);
        if (String(body.status).toLowerCase() === "ok" && notReady.length === 0) {
          setHealth({ state: "online", detail: `BFF(${BFF}) 연결됨 · 모든 의존성 READY`, notReady });
        } else {
          setHealth({
            state: "degraded",
            detail: `BFF는 연결됐지만 준비되지 않은 의존성이 있습니다: ${notReady.join(", ") || body.status}`,
            notReady,
          });
        }
      } catch {
        if (!alive) return;
        setHealth({
          state: "offline",
          detail: `BFF(${BFF})에 연결하지 못했습니다. 저장소 루트에서 FastAPI BFF를 8001 포트로 실행하세요.`,
          notReady: [],
        });
      }
    }

    void probe();
    const timer = window.setInterval(probe, POLL_MS);
    return () => {
      alive = false;
      window.clearInterval(timer);
    };
  }, []);

  return health;
}
