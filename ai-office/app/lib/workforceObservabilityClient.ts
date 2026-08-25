/**
 * HR Langfuse 실측 관측 client — BFF `/ui/workforce/observability` 폴링.
 *
 * 2026-08-26 통합. 이전에는 idle-agents / capacity / llm-usage / trigger-rates
 * 네 개를 각각 불렀고, Workforce 화면 한 장이 그 넷을 동시에 띄웠다. 넷 다
 * workforce-api 쪽에서 **같은 Langfuse 실행 이벤트**를 훑는데도 각자 훑어서,
 * Worker 8명 기준 화면 1회당 왕복이 40회였다(capacity 와 llm-usage 는 아예
 * 글자 그대로 같은 질의였다). 60초 폴링이라 그게 그대로 분당 부하가 됐다.
 *
 * 그래서 창구를 하나로 합쳤다. 실제 왕복 절감은 workforce-api 쪽
 * `WindowedActivityReader` 가 하고(Worker 당 최대 2회), 이 client 는 그 결과를
 * 한 번만 받아 두 패널에 나눠준다 — `useWorkforceObservability()` 를 두 패널이
 * 같은 창(window)으로 부르면 React Query 가 queryKey 로 접어 요청은 한 번이다.
 *
 * ⚠ 두 패널이 **같은 창**을 써야 이 접힘이 성립한다. 창이 갈리면 요청이 둘로
 *   갈라지고(왕복이 조용히 두 배가 되고), 무엇보다 같은 화면에 서로 다른 창의
 *   숫자가 나란히 놓인다 — 통합 전에 실제로 그랬다(유휴 표는 패널이 고른 창,
 *   Capacity 표는 고정 24h). 그래서 창 상태는 DepartmentInspector 가 들고 있다.
 *
 * 판정/집계 로직은 전부 workforce-api(observability.py)에 있고 여기서 복제하지
 * 않는다 — 원문 프롬프트·응답은 이 경로에 애초에 실리지 않는다.
 */

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { BFF, bffFetch } from "./bffClient";

/** 유휴 판정 60초 주기. 유휴는 시간 단위로 바뀌는 값이라 더 자주 부를 이유가 없다. */
export const OBSERVABILITY_POLL_MS = 60_000;

/**
 * 네 상태의 뜻은 서로 다르다(색만으로 구분하지 않는다):
 *   - ACTIVE: 임계시간 안에 실행이 관측됨.
 *   - IDLE: 관측은 됐지만 임계시간보다 오래 전 — 조치 검토 대상.
 *   - UNOBSERVED: 조건부 Worker의 trigger가 이 창(lookback) 안에 발화하지 않았을
 *     수 있음 — 유휴로 단정하지 않는다.
 *   - UNAVAILABLE: Langfuse 조회 실패/자격증명 없음 — "쉬고 있다"가 아니라 "모른다".
 */
export type IdleStatus = "ACTIVE" | "IDLE" | "UNOBSERVED" | "UNAVAILABLE";

export type WorkerIdleReport = {
  department: string;
  worker_id: string;
  trigger: string;
  status: IdleStatus | string;
  last_seen_at: string | null;
  idle_hours: number | null;
};

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

/**
 * 같은 Langfuse 실행 이벤트를 읽되 지연·재시도가 아니라 **토큰·모델 축**을 집계한
 * 값이다(workforce-api `check_department_llm_usage`). Capacity와 부서 키가 같아
 * 화면에서 한 표로 합쳐 보여준다 — 별도 패널을 만들지 않는다.
 *
 * llm_calls/prompt_tokens/completion_tokens는 `begin_worker_metric()` 컨텍스트가
 * 열려 있었던 실행에서만 나온다 — `arrivals > 0`이어도 null일 수 있고, 그건
 * "0번 불렀다"가 아니라 "그 창의 실행이 전부 계측 컨텍스트 밖이었다"는 뜻이다.
 */
export type DepartmentLlmUsageReport = {
  department: string;
  window_start: string;
  window_end: string;
  status: CapacityObservationStatus | string;
  arrivals: number | null;
  llm_calls: number | null;
  prompt_tokens: number | null;
  completion_tokens: number | null;
  avg_attempts: number | null;
  status_counts: Record<string, number> | null;
};

/**
 * Worker별 발화율 — 실행 / (실행 + 미발화).
 *
 * idle 판정이 UNOBSERVED 하나로 뭉뚱그리는 두 상황을 나눠준다:
 *   fire_rate === null  이 창에 **기회 자체가 없었다**(분모 0) — 결함이 아니다
 *   fire_rate === 0     기회가 있었는데 **한 번도 안 켜졌다**(분모 > 0, 분자 0)
 *
 * 그래서 0과 null을 화면에서 같은 칸으로 만들면 안 된다.
 */
export type WorkerTriggerRateReport = {
  department: string;
  worker_id: string;
  trigger: string;
  window_start: string;
  window_end: string;
  status: "MEASURED" | "UNAVAILABLE" | string;
  execution_count: number | null;
  opportunity_count: number | null;
  fire_rate: number | null;
};

export type WorkforceObservability = {
  window_start: string;
  window_end: string;
  idle_agents: WorkerIdleReport[];
  capacity: DepartmentCapacityReport[];
  llm_usage: DepartmentLlmUsageReport[];
  trigger_rates: WorkerTriggerRateReport[];
  /** 이 호출이 Langfuse에 실제로 낸 논리 질의 수 — 중복 제거가 풀리면 먼저 는다. */
  langfuse_queries: number;
  head_profiles_unavailable?: string;
};

export class WorkforceObservabilityError extends Error {
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
  return `Worker 관측 조회 실패 (HTTP ${status})`;
}

function hasObservabilityShape(value: unknown): value is WorkforceObservability {
  if (typeof value !== "object" || value === null) return false;
  const record = value as Record<string, unknown>;
  // 네 배열이 다 있어야 계약이 맞다 - 하나라도 빠지면 그 표가 조용히 빈 채로
  // 그려지고, 화면은 "관측 결과 0건"과 구분하지 못한다.
  return (
    Array.isArray(record.idle_agents) &&
    Array.isArray(record.capacity) &&
    Array.isArray(record.llm_usage) &&
    Array.isArray(record.trigger_rates)
  );
}

export type WorkforceObservabilityWindow = {
  /** 관측 창(시간). 이 창 안에 한 번도 안 잡히면 UNOBSERVED다. */
  lookbackHours: number;
  /** 이 시간보다 오래 전 관측이면 IDLE, 안이면 ACTIVE. */
  idleThresholdHours: number;
};

export type WorkforceObservabilityWindowKey = "daily" | "weekly";

/**
 * 일간은 오늘 하루(4시간 넘게 안 잡히면 IDLE), 주간은 최근 7일(하루 넘게 안
 * 잡히면 IDLE) - 창이 넓어지면 "유휴"의 기준도 같이 넓어져야 한다. 그렇지
 * 않으면 주간 보기에서 정상 근무 패턴(야간·주말 공백)이 전부 IDLE로 뜬다.
 *
 * 패널이 아니라 여기 있는 이유: 유휴 패널과 Capacity 패널이 **같은 창 객체**를
 * 써야 요청이 하나로 접힌다(위 useWorkforceObservability 주석 참고). 창 정의가
 * 한쪽 패널 안에 있으면 다른 쪽이 자기 값을 따로 들고, 그게 통합 전 상태다.
 */
export const WORKFORCE_OBSERVABILITY_WINDOWS: Record<
  WorkforceObservabilityWindowKey,
  WorkforceObservabilityWindow & { label: string }
> = {
  daily: { label: "일간", lookbackHours: 24, idleThresholdHours: 4 },
  weekly: { label: "주간", lookbackHours: 24 * 7, idleThresholdHours: 24 },
};

export async function fetchWorkforceObservability(
  window: WorkforceObservabilityWindow,
): Promise<WorkforceObservability> {
  const query = `?lookback_hours=${window.lookbackHours}&idle_threshold_hours=${window.idleThresholdHours}`;
  let response: Response;
  try {
    response = await bffFetch(`/ui/workforce/observability${query}`, {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch {
    throw new WorkforceObservabilityError(
      `BFF(${BFF})에 연결하지 못했습니다. FastAPI BFF가 실행 중인지 확인하세요.`,
      0,
    );
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    throw new WorkforceObservabilityError(explain(body, response.status), response.status);
  }
  if (!hasObservabilityShape(body)) {
    throw new WorkforceObservabilityError("Worker 관측 응답 계약이 올바르지 않습니다.", response.status);
  }
  return body;
}

/**
 * 두 패널(유휴/Capacity)이 **같은 인자로** 부르면 요청은 한 번만 나간다.
 *
 * queryKey 에 창을 통째로 담는 이유: 창이 다르면 응답도 다른 관측이라 같은 캐시
 * 항목이면 안 되고, 창이 같으면 반드시 같은 항목이어야 한다(그게 접힘의 전부다).
 */
export function useWorkforceObservability(
  window: WorkforceObservabilityWindow,
): UseQueryResult<WorkforceObservability, WorkforceObservabilityError> {
  return useQuery<WorkforceObservability, WorkforceObservabilityError>({
    queryKey: ["workforce-observability", window.lookbackHours, window.idleThresholdHours],
    queryFn: () => fetchWorkforceObservability(window),
    refetchInterval: OBSERVABILITY_POLL_MS,
    staleTime: 0,
    retry: false,
  });
}
