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
