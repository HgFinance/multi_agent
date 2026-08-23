/**
 * HR Worker 유휴 관측 client — BFF `/ui/workforce/idle-agents` 폴링.
 *
 * 6개 투자본부(research/trading/risk/quant-backtest/accounting-portfolio/qa)에
 * 등록된 Worker 전원의 ACTIVE/IDLE/UNOBSERVED/UNAVAILABLE 판정을 그대로 보여준다.
 * 판정 로직(Langfuse 조회, 임계시간 비교)은 workforce-api가 갖고 있고 이 화면은
 * 그 결과만 표시한다 — 원문 프롬프트·응답은 이 경로에 애초에 실리지 않는다.
 *
 * 네 상태의 뜻은 서로 다르다(색만으로 구분하지 않는다):
 *   - ACTIVE: 임계시간 안에 실행이 관측됨.
 *   - IDLE: 관측은 됐지만 임계시간보다 오래 전 — 조치 검토 대상.
 *   - UNOBSERVED: 조건부 Worker의 trigger가 이 창(lookback) 안에 발화하지 않았을
 *     수 있음 — 유휴로 단정하지 않는다.
 *   - UNAVAILABLE: Langfuse 조회 실패/자격증명 없음 — "쉬고 있다"가 아니라 "모른다".
 */

import { BFF, bffFetch } from "./bffClient";

export type IdleStatus = "ACTIVE" | "IDLE" | "UNOBSERVED" | "UNAVAILABLE";

export type WorkerIdleReport = {
  department: string;
  worker_id: string;
  trigger: string;
  status: IdleStatus | string;
  last_seen_at: string | null;
  idle_hours: number | null;
};

export type WorkforceIdleAgents = {
  idle_agents: WorkerIdleReport[];
  head_profiles_unavailable?: string;
};

export class WorkforceIdleAgentsError extends Error {
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
  return `Worker 유휴 상태 조회 실패 (HTTP ${status})`;
}

function hasIdleAgentsShape(value: unknown): value is WorkforceIdleAgents {
  if (typeof value !== "object" || value === null) return false;
  return Array.isArray((value as Record<string, unknown>).idle_agents);
}

export type WorkforceIdleWindow = {
  /** 관측 창(시간). 이 창 안에 한 번도 안 잡히면 UNOBSERVED다. */
  lookbackHours: number;
  /** 이 시간보다 오래 전 관측이면 IDLE, 안이면 ACTIVE. */
  idleThresholdHours: number;
};

export async function fetchWorkforceIdleAgents(window?: WorkforceIdleWindow): Promise<WorkforceIdleAgents> {
  const query = window
    ? `?lookback_hours=${window.lookbackHours}&idle_threshold_hours=${window.idleThresholdHours}`
    : "";
  let response: Response;
  try {
    response = await bffFetch(`/ui/workforce/idle-agents${query}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch {
    throw new WorkforceIdleAgentsError(
      `BFF(${BFF})에 연결하지 못했습니다. FastAPI BFF가 실행 중인지 확인하세요.`,
      0,
    );
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new WorkforceIdleAgentsError(explain(body, response.status), response.status);
  if (!hasIdleAgentsShape(body)) {
    throw new WorkforceIdleAgentsError("Worker 유휴 상태 응답 계약이 올바르지 않습니다.", response.status);
  }
  return body;
}
