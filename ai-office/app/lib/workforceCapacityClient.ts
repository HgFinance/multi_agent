/**
 * HR Capacity(용량) client — BFF `/ui/workforce/capacity` 폴링.
 *
 * `workforce.capacity_snapshots` writer가 아직 없어(P1-2 미착수) DB 기반
 * Scorecard의 capacity는 항상 null이다. 이 값은 그 대신 Langfuse 실행 이벤트를
 * 직접 집계한 것이다(WorkforceIdleAgentsPanel과 같은 원리) — 판정/집계 로직은
 * workforce-api의 observability.py에 있고 이 client는 결과만 표시한다.
 *
 * queue_p95_ms는 이 경로에서 항상 null이다 - 지금 계측은 "작업이 끝났다" 시점
 * 이벤트 하나뿐이라 대기열 진입 시점을 잴 기준이 없다.
 */

import { BFF, bffFetch } from "./bffClient";

export type CapacityObservationStatus = "MEASURED" | "UNAVAILABLE";

export type DepartmentCapacityReport = {
  department: string;
  window_start: string;
  window_end: string;
  status: CapacityObservationStatus | string;
  arrivals: number | null;
  duration_p95_ms: number | null;
  retry_rate: number | null;
  error_rate: number | null;
  utilization: number | null;
  queue_p95_ms: null;
};

export type WorkforceCapacity = {
  capacity: DepartmentCapacityReport[];
};

export class WorkforceCapacityError extends Error {
  /** 연동이 꺼진 것(503)과 실제 장애를 화면이 구분해서 안내한다. */
  readonly status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

function explain(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (typeof detail === "object" && detail !== null && "message" in detail) {
      const message = (detail as { message?: unknown }).message;
      if (typeof message === "string" && message.trim()) return message;
    }
  }
  return `Capacity 조회 실패 (HTTP ${status})`;
}

function hasCapacityShape(value: unknown): value is WorkforceCapacity {
  if (typeof value !== "object" || value === null) return false;
  return Array.isArray((value as Record<string, unknown>).capacity);
}

export async function fetchWorkforceCapacity(lookbackHours = 24): Promise<WorkforceCapacity> {
  let response: Response;
  try {
    response = await bffFetch(`/ui/workforce/capacity?lookback_hours=${lookbackHours}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch {
    throw new WorkforceCapacityError(
      `BFF(${BFF})에 연결하지 못했습니다. FastAPI BFF가 실행 중인지 확인하세요.`,
      0,
    );
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new WorkforceCapacityError(explain(body, response.status), response.status);
  if (!hasCapacityShape(body)) {
    throw new WorkforceCapacityError("Capacity 응답 계약이 올바르지 않습니다.", response.status);
  }
  return body;
}
