"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { BFF, getSnapshotSequence, isValidSequence, parseSnapshot, type TradingSnapshot } from "./readModel";

export type BffConnection = "connecting" | "connected" | "stale" | "offline";
export function isCommandableConnection(connection: BffConnection): boolean {
  return connection === "connected";
}

export type BffFeed = {
  snapshot: TradingSnapshot | null;
  connection: BffConnection;
  error: string;
  lastUpdated: string | null;
  refresh: () => Promise<void>;
};

// REST는 canonical snapshot fallback, WebSocket은 변경 신호와 sequence를 전달한다.
const POLL_INTERVAL_MS = 2500;
const WS_RECONNECT_BASE_MS = 500;
const WS_RECONNECT_MAX_MS = 10000;
const BffContext = createContext<BffFeed | null>(null);

function operationsSocketUrl(): string {
  return `${BFF.replace(/^http/, "ws")}/ws/operations`;
}

function explainBffError(cause: unknown): string {
  const message = cause instanceof Error ? cause.message : String(cause);
  if (message === "Not Found" || message.includes("404")) {
    return "BFF 경로를 찾지 못했습니다. 저장소 루트에서 FastAPI BFF를 8001 포트로 실행하세요.";
  }
  if (message.includes("Failed to fetch") || message.includes("NetworkError")) {
    return "BFF 연결 대기 중입니다. 저장소 루트에서 FastAPI BFF를 8001 포트로 실행하세요.";
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

export function BffProvider({ children }: { children: ReactNode }) {
  const [snapshot, setSnapshot] = useState<TradingSnapshot | null>(null);
  const snapshotRef = useRef<TradingSnapshot | null>(null);
  const lastSequenceRef = useRef(0);
  const [connection, setConnection] = useState<BffConnection>("connecting");
  const [error, setError] = useState("");
  const [lastUpdated, setLastUpdated] = useState<string | null>(null);
  const refreshInFlightRef = useRef(false);

  const refresh = useCallback(async () => {
    if (refreshInFlightRef.current) return;
    refreshInFlightRef.current = true;
    setConnection((current) => (current === "connected" ? "connected" : "connecting"));
    try {
      const next = await fetchBffSnapshot();
      const nextSequence = getSnapshotSequence(next);
      // A newer WebSocket notification may arrive while REST is in flight.
      // Never roll the canonical projection backwards or accept an unknown sequence.
      if (nextSequence === null || nextSequence < lastSequenceRef.current) {
        setConnection(snapshotRef.current ? "stale" : "offline");
        return;
      }
      snapshotRef.current = next;
      setSnapshot(next);
      if (nextSequence >= lastSequenceRef.current) {
        lastSequenceRef.current = nextSequence;
      }
      setLastUpdated(new Date().toISOString());
      setError("");
      setConnection("connected");
    } catch (cause) {
      setError(explainBffError(cause));
      setConnection(snapshotRef.current ? "stale" : "offline");
    } finally {
      refreshInFlightRef.current = false;
    }
  }, []);

  useEffect(() => {
    let active = true;
    let reconnectDelay = WS_RECONNECT_BASE_MS;
    let reconnectTimer: number | undefined;
    let socket: WebSocket | null = null;

    const scheduleReconnect = () => {
      if (!active || reconnectTimer !== undefined) return;
      const delay = reconnectDelay;
      reconnectDelay = Math.min(reconnectDelay * 2, WS_RECONNECT_MAX_MS);
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = undefined;
        connect();
      }, delay);
    };

    function connect() {
      if (!active || typeof window.WebSocket === "undefined") return;
      try {
        socket = new window.WebSocket(operationsSocketUrl());
        socket.onopen = () => {
          reconnectDelay = WS_RECONNECT_BASE_MS;
          void refresh();
        };
        socket.onmessage = (event) => {
          try {
            const message = JSON.parse(String(event.data)) as {
              event_type?: unknown;
              sequence?: unknown;
            };
            const eventType = message.event_type;
            const sequence = message.sequence;
            if (typeof eventType !== "string" || !isValidSequence(sequence)) return;
            if (
              eventType !== "operations.snapshot_required.v1" &&
              eventType !== "operations.heartbeat.v1" &&
              eventType !== "agent.status.v1"
            ) {
              return;
            }
            if (eventType === "operations.heartbeat.v1") return;
            const previous = lastSequenceRef.current;
            // Gap 복구는 이벤트를 추측하지 않고 canonical REST snapshot을 다시 읽는다.
            if (sequence > previous + 1 && previous > 0) {
              void refresh();
            } else if (sequence > previous || eventType === "operations.snapshot_required.v1") {
              void refresh();
            }
            if (sequence > lastSequenceRef.current) {
              lastSequenceRef.current = sequence;
            }
          } catch {
            // 잘못된 WS 메시지는 화면 상태를 추측하지 않고 다음 REST poll에 맡긴다.
          }
        };
        socket.onerror = () => socket?.close();
        socket.onclose = () => {
          socket = null;
          setConnection((current) => (current === "connected" ? "stale" : current));
          scheduleReconnect();
        };
      } catch {
        scheduleReconnect();
      }
    }

    const initialTimer = window.setTimeout(() => {
      if (active) void refresh();
    }, 0);
    const pollTimer = window.setInterval(() => {
      if (active && document.visibilityState !== "hidden") void refresh();
    }, POLL_INTERVAL_MS);
    connect();

    return () => {
      active = false;
      window.clearTimeout(initialTimer);
      window.clearInterval(pollTimer);
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      socket?.close();
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
