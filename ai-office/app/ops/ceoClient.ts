import { BFF } from "./readModel";

export type CeoQueryResult = {
  schema_version: "ceo.query-result.v1";
  department: "ceo-agent";
  binding: false;
  answer: string;
  session_id: string | null;
  task: {
    task_id: string | null;
    status: string;
    source: "hermes-kanban";
  } | null;
};

function explainError(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return `CEO Hermes 연결 실패 (HTTP ${status})`;
}

/** 카드 한 장의 **결말**. Kanban 의 status 와 일부러 다르다 - status 는 보드의
 *  사정이고, 이것은 "사용자 질문에 답이 됐는가"다. 특히 `NO_ANSWER` 는 보드에서
 *  `done` 으로 보이는 카드다(결과 본문이 비어 있는 완료). */
export type CardOutcome =
  | "QUEUED" | "RUNNING" | "ANSWERED" | "NO_ANSWER" | "BLOCKED" | "FAILED"
  | "STALE" | "NO_ASSIGNEE";

export type CeoQueryCard = {
  task_id: string;
  title: string;
  department: string;
  outcome: CardOutcome;
  summary: string;
  has_result: boolean;
  depends_on: string[];
};

export type CeoQueryProgress = {
  schema_version: "ceo.query-progress.v1";
  root_task_id: string;
  total: number;
  finished: number;
  all_terminal: boolean;
  answer_grounded: boolean;
  unusable: string[];
  stalled: string[];
  cards: CeoQueryCard[];
};

export async function askCeo(query: string, requestId?: string): Promise<CeoQueryResult> {
  const response = await fetch(`${BFF}/ui/ceo/ask`, {
    method: "POST",
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      Accept: "application/json",
      ...(requestId ? { "X-Request-Id": requestId } : {}),
    },
    body: JSON.stringify({ query, request_id: requestId }),
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  if (typeof body !== "object" || body === null) {
    throw new Error("CEO Hermes 응답 계약이 올바르지 않습니다.");
  }
  const result = body as Partial<CeoQueryResult>;
  if (result.schema_version !== "ceo.query-result.v1" || typeof result.answer !== "string") {
    throw new Error("CEO Hermes 응답 계약이 올바르지 않습니다.");
  }
  return result as CeoQueryResult;
}

/** 뿌리 카드에 매달린 본부 카드들의 진행·실패.
 *
 *  Kanban 임베드는 **보드 원본**을 보여준다. 거기서는 결과가 빈 완료도 `done`
 *  으로 보여 성공으로 읽힌다. 이 경로는 같은 카드를 "답이 됐는가" 기준으로
 *  다시 판정한 것이라 둘은 서로를 대체하지 않는다. */
export async function ceoProgress(rootTaskId: string): Promise<CeoQueryProgress> {
  const response = await fetch(`${BFF}/ui/ceo/ask/${encodeURIComponent(rootTaskId)}`, {
    cache: "no-store",
    headers: { Accept: "application/json" },
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  if (typeof body !== "object" || body === null) {
    throw new Error("CEO 진행 상태 응답 계약이 올바르지 않습니다.");
  }
  const result = body as Partial<CeoQueryProgress>;
  if (result.schema_version !== "ceo.query-progress.v1" || !Array.isArray(result.cards)) {
    throw new Error("CEO 진행 상태 응답 계약이 올바르지 않습니다.");
  }
  return result as CeoQueryProgress;
}
