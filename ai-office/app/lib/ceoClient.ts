/**
 * CEO Hermes 질의 클라이언트 — FastAPI BFF `/ui/ceo/ask`.
 *
 * 삭제 전 app/ops/ceoClient.ts 를 그대로 되살린 것이다. 달라진 건 BFF 주소
 * 상수를 여기 같이 둔 것뿐 — 그것 하나 때문에 readModel(371줄)을 되살릴
 * 이유는 없다.
 *
 * 응답은 binding: false 다. 이 경로로 오는 문장은 참고용이고 주문·원장·한도를
 * 바꾸지 않는다. 화면에서도 수치를 여기서 뽑아 확정하지 않는다.
 */

/** FastAPI BFF 주소. 배포 Origin이 정해지면 환경변수로 넘긴다. */
const configuredBff = process.env.NEXT_PUBLIC_BFF_URL?.trim();
export const BFF = (configuredBff || "http://127.0.0.1:8001").replace(/\/+$/, "");

export type CeoQueryResult = {
  schema_version: "ceo.query-accepted.v1";
  department: "ceo-agent";
  binding: false;
  task_id: string;
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
  let response: Response;
  try {
    response = await fetch(`${BFF}/ui/ceo/ask`, {
      method: "POST",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        Accept: "application/json",
        ...(requestId ? { "X-Request-Id": requestId } : {}),
      },
      body: JSON.stringify({ query, request_id: requestId }),
    });
  } catch {
    // fetch 자체가 실패하면 브라우저는 "Failed to fetch"만 준다. BFF가 꺼져
    // 있는 게 지금의 기본 상태라, 어디에 무엇을 띄워야 하는지 그대로 알린다.
    throw new Error(`CEO Hermes(BFF ${BFF})에 연결하지 못했습니다. FastAPI BFF가 실행 중인지 확인하세요.`);
  }
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  if (typeof body !== "object" || body === null) {
    throw new Error("CEO Hermes 응답 계약이 올바르지 않습니다.");
  }
  const result = body as Partial<CeoQueryResult>;
  if (result.schema_version !== "ceo.query-accepted.v1" || typeof result.answer !== "string" || typeof result.task_id !== "string") {
    throw new Error("CEO Hermes 응답 계약이 올바르지 않습니다.");
  }
  return result as CeoQueryResult;
}
