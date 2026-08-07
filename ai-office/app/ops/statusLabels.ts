export const RUNTIME_STATUS_LABEL: Record<string, string> = {
  CONNECTED: "연결됨",
  STALE: "지연 수신",
  UNKNOWN: "확인 중",
  OFFLINE: "미연결",
  IDLE: "대기",
  REGISTERED: "등록됨",
  QUEUED: "실행 대기",
  RUNNING: "업무 중",
  COMPLETED: "완료",
  WAITING_APPROVAL: "승인 대기",
  BLOCKED: "실행 차단",
  DEGRADED: "안전 보류",
  ERROR: "오류",
  PENDING: "검토 대기",
  USER_APPROVAL: "대표 승인 필요",
  USER_APPROVED: "대표 승인 완료",
  SKIPPED: "미호출",
  IMPLEMENTED: "구현됨",
  IMPLEMENTED_INTERNAL: "내부 구현",
  PLANNED: "계획됨",
  LIVE: "실시간",
  FAST_APPLIED: "안전 조정 적용",
  APPROVED: "승인됨",
  APPROVE: "승인됨",
  REJECTED: "거절됨",
  REJECT: "거절됨",
  REVOKED: "철회됨",
  EXPIRED: "기한 만료",
  PASS: "통과",
  FAIL: "실패",
  WARN: "주의",
  MANUAL_REVIEW_REQUIRED: "수동 검토 필요",
  LEGACY_ALIAS: "호환 별칭",
};

export function readableRuntimeStatus(value: unknown): string {
  const status = String(value ?? "UNKNOWN").toUpperCase().replace(/\s+/g, "_");
  return RUNTIME_STATUS_LABEL[status] ?? status;
}

const RUNTIME_EVENT_LABEL: Record<string, string> = {
  run_degraded: "실행 안전 보류",
  run_error: "실행 오류",
  department_started: "부서 실행 시작",
  department_blocked: "부서 실행 보류",
  department_skipped: "부서 미호출",
  worker_started: "직원 작업 시작",
  worker_completed: "직원 작업 완료",
  worker_summary: "직원 결과 취합",
};

export function readableRuntimeKind(value: unknown): string {
  const kind = String(value ?? "runtime");
  return RUNTIME_EVENT_LABEL[kind] ?? kind;
}

export function readableRuntimeMessage(text: string): { summary: string; action?: string } {
  if (text.includes("PIT 입력이 준비되지 않아")) {
    return { summary: "시점 고정 데이터가 부족해 분석을 안전하게 보류했습니다.", action: "국내 종목과 시장 스냅샷을 준비한 뒤 다시 실행하세요." };
  }
  if (text.includes("Worker 또는 Gate 검증")) {
    return { summary: "실행은 끝났지만 Worker 또는 Gate 검증이 완료되지 않았습니다.", action: "Risk·QA 결과와 대표 승인을 확인하세요." };
  }
  if (text.includes("task plan에 따라") && text.includes("호출하지 않습니다")) {
    return { summary: "이번 요청의 작업 계획에 포함되지 않아 호출하지 않았습니다." };
  }
  return { summary: text };
}

export type PitReadiness = {
  quality_status?: string;
  reasons?: string[];
  candidate_count?: number;
  research_document_count?: number;
  market_snapshot_count?: number;
  domestic_instrument_count?: number;
  as_of?: string;
};

const PIT_REASON_LABEL: Record<string, string> = {
  NO_VALID_PORTFOLIO_CANDIDATES: "검토 가능한 포트폴리오 후보가 없습니다",
  NO_PIT_DOMESTIC_EQUITY_INSTRUMENTS: "시점 고정 국내 종목 데이터가 없습니다",
  NO_PIT_MARKET_SNAPSHOTS: "시점 고정 시장 스냅샷이 없습니다",
  PIT_DATA_NOT_READY: "시점 고정 데이터가 아직 준비되지 않았습니다",
};

export function readablePitReason(value: string): string {
  return PIT_REASON_LABEL[value] ?? value;
}

export function readPitReadiness(runtime: unknown): PitReadiness | null {
  if (typeof runtime !== "object" || runtime === null) return null;
  const result = (runtime as { result?: unknown }).result;
  if (typeof result !== "object" || result === null) return null;
  const context = (result as { data_context?: unknown }).data_context;
  if (typeof context !== "object" || context === null) return null;
  const typedContext = context as {
    quality_status?: unknown;
    reasons?: unknown;
    pit_readiness?: unknown;
    data_diagnostics?: unknown;
  };
  const diagnostics = typedContext.data_diagnostics;
  const nestedReadiness = typeof diagnostics === "object" && diagnostics !== null
    ? (diagnostics as { pit_readiness?: unknown }).pit_readiness
    : null;
  const readiness = typeof typedContext.pit_readiness === "object" && typedContext.pit_readiness !== null
    ? typedContext.pit_readiness
    : nestedReadiness;
  if (typeof readiness === "object" && readiness !== null) {
    const value = readiness as PitReadiness;
    return {
      ...value,
      quality_status: value.quality_status ?? (typeof typedContext.quality_status === "string" ? typedContext.quality_status : undefined),
      reasons: value.reasons ?? (Array.isArray(typedContext.reasons) ? typedContext.reasons.filter((reason): reason is string => typeof reason === "string") : undefined),
    };
  }
  return typeof typedContext.quality_status === "string"
    ? {
        quality_status: typedContext.quality_status,
        reasons: Array.isArray(typedContext.reasons) ? typedContext.reasons.filter((reason): reason is string => typeof reason === "string") : undefined,
      }
    : null;
}

export type RuntimeMessageLike = {
  id: string;
  occurred_at: string;
  kind: string;
  department_code: string | null;
  worker_id: string | null;
  text: string;
};

export function groupRuntimeMessages(messages: RuntimeMessageLike[]): Array<RuntimeMessageLike & { count: number }> {
  const grouped = new Map<string, RuntimeMessageLike & { count: number }>();
  for (const message of messages.slice(-12)) {
    const summary = readableRuntimeMessage(message.text).summary;
    const key = [message.kind, message.department_code ?? "", summary].join("|");
    const current = grouped.get(key);
    if (current) current.count += 1;
    else grouped.set(key, { ...message, count: 1 });
  }
  return [...grouped.values()].reverse();
}
