"use client";

import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { BFF, parseSnapshot, type TradingSnapshot } from "./readModel";

export type BffConnection = "connecting" | "connected" | "stale" | "offline";

export type BffFeed = {
  snapshot: TradingSnapshot | null;
  connection: BffConnection;
  error: string;
  lastUpdated: string | null;
  refresh: () => Promise<void>;
};

// Worker runs are short in TEST mode; a 5s interval only showed the final batch.
// Keep the projection read-only while polling often enough to render live workers.
const POLL_INTERVAL_MS = 400;
const BffContext = createContext<BffFeed | null>(null);

function explainBffError(cause: unknown): string {
  const message = cause instanceof Error ? cause.message : String(cause);
  if (message === "Not Found" || message.includes("404")) {
    return "BFF 경로를 찾지 못했습니다. 저장소 루트에서 FastAPI BFF를 8000 포트로 실행하세요.";
  }
  if (message.includes("Failed to fetch") || message.includes("NetworkError")) {
    return "BFF 연결 대기 중입니다. 저장소 루트에서 FastAPI BFF를 8000 포트로 실행하세요.";
  }
  return message;
}

export async function fetchBffSnapshot(): Promise<TradingSnapshot> {
  const response = await fetch(`${BFF}/ui/snapshot`, {
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    const detail =
      typeof body === "object" && body !== null && "detail" in body
        ? String((body as { detail?: unknown }).detail)
        : `HTTP ${response.status}`;
    throw new Error(detail);
  }
  return parseSnapshot(body);
}

export function BffProvider({ children }: { children: React.ReactNode }) {
  const [snapshot, setSnapshot] = useState<TradingSnapshot | null>(null);
  const snapshotRef = useRef<TradingSnapshot | null>(null);
  const [connection, setConnection] = useState<BffConnection>("connecting");
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setConnection((current) => (current === "connected" ? "connected" : "connecting"));
    try {
      const next = await fetchBffSnapshot();
      snapshotRef.current = next;
      setSnapshot(next);
      setLastUpdated(new Date().toISOString());
      setError("");
      setConnection("connected");
    } catch (cause) {
      setError(explainBffError(cause));
      setConnection(snapshotRef.current ? "stale" : "offline");
    }
  }, []);

  useEffect(() => {
    let active = true;
    const initialTimer = window.setTimeout(() => {
      if (active) void refresh();
    }, 0);
    const timer = window.setInterval(() => {
      if (active) void refresh();
    }, POLL_INTERVAL_MS);
    return () => {
      active = false;
      window.clearTimeout(initialTimer);
      window.clearInterval(timer);
    };
  }, [refresh]);

  const value = useMemo(
    () => ({ snapshot, connection, error, lastUpdated, refresh }),
    [snapshot, connection, error, lastUpdated, refresh],
  );
  return <BffContext.Provider value={value}>{children}</BffContext.Provider>;
}

export function useBffFeed(): BffFeed {
  const value = useContext(BffContext);
  if (!value) throw new Error("useBffFeed는 BffProvider 안에서 사용해야 합니다");
  return value;
}
