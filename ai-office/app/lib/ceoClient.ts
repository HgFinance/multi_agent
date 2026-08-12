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

/** v2(PR #226)는 `status`와 `planning`을 더한다. 배포 순서를 강제하지 않으려고
 *  둘 다 받는다 — BFF가 먼저 올라가든 프런트가 먼저 올라가든 안 깨진다. */
export const ACCEPTED_QUERY_VERSIONS = ["ceo.query-accepted.v1", "ceo.query-accepted.v2"] as const;

export type CeoQueryPlanning = {
  selected_departments: string[];
  steps: string[];
  qa_required: boolean;
  summary: string | null;
};

export type CeoQueryResult = {
  schema_version: (typeof ACCEPTED_QUERY_VERSIONS)[number];
  department: "ceo-agent";
  binding: false;
  task_id: string;
  answer: string;
  session_id: string | null;
  /** v2 전용. v1 응답에는 없다. */
  status?: "planned" | "accepted";
  /** v2 전용. 어느 본부가 선택됐고 QA가 필요한지. */
  planning?: CeoQueryPlanning;
  task: {
    task_id: string | null;
    status: string;
    source: "hermes-kanban";
  } | null;
};

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
  /** 질문 자체를 붙들어 두는 뿌리 카드. 본부의 답이 아니라서 숫자에서 빠진다. */
  is_root: boolean;
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
  const known = ACCEPTED_QUERY_VERSIONS.includes(result.schema_version as (typeof ACCEPTED_QUERY_VERSIONS)[number]);
  if (!known || typeof result.answer !== "string" || typeof result.task_id !== "string") {
    throw new Error(
      `CEO Hermes 응답 계약이 올바르지 않습니다. (받은 schema_version: ${String(result.schema_version)})`,
    );
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
