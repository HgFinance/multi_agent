/**
 * CEO 오피스 Notion 리포트 읽기 — FastAPI BFF `/ui/notion/*`.
 *
 * 결과물 창고가 보여주는 것은 **Notion에 실제로 발행된 리포트**다. Hermes
 * Kanban의 done 카드가 아니다 - 둘은 다르다. Kanban은 카드가 끝났다는 사실만
 * 알고, 그 결과가 리포트로 남았는지는 모른다. 실제로 done 카드 다수는
 * `DepartmentNotionProjection`의 부서 필터에 걸려 Notion에 아무것도 남기지
 * 않는다(ceo·qa·accounting·hr). 그 카드를 목록에 섞으면 열어도 빈 리포트다.
 *
 * `NOTION_TOKEN`은 브라우저에 없다. 토큰은 BFF 프로세스에만 있고 화면은
 * 정규화된 목록과 마크다운 본문만 받는다.
 */

import { bffFetch } from "./bffClient";

export type NotionReportCard = {
  schema_version: "ui.notion-report-card.v1";
  page_id: string;
  url: string;
  title: string;
  category: string | null;
  state: string | null;
  published_at: string | null;
};

export type NotionReportListResponse = {
  schema_version: "ui.notion-reports.v1";
  source: "notion";
  /** 항상 false. 이 값은 원장도 Risk 판정도 아니다. */
  authoritative: false;
  database_id: string;
  reports: NotionReportCard[];
};

export type NotionReportDetail = {
  schema_version: "ui.notion-report.v1";
  source: "notion";
  authoritative: false;
  page_id: string;
  url: string;
  title: string;
  category: string | null;
  state: string | null;
  published_at: string | null;
  markdown: string;
  /** 본문이 100블록에서 잘렸는지. 잘렸으면 화면이 "Notion에서 열기"를 권한다. */
  truncated: boolean;
};

function explainError(body: unknown, status: number): string {
  if (typeof body === "object" && body !== null && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
  }
  return `Notion 리포트 연결 실패 (HTTP ${status})`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

/**
 * 카드가 가져야 할 필드가 다 있는지.
 *
 * 일부러 타입 가드(`value is NotionReportCard`)로 만들지 않는다. 본문 응답도
 * 같은 필드를 공유하는데, 가드로 좁히면 그 뒤에서 `markdown`을 읽을 수 없다.
 */
function hasReportCardFields(value: unknown): value is Record<string, unknown> {
  if (!isRecord(value)) return false;
  const nullableString = (key: string) =>
    value[key] === null || typeof value[key] === "string";
  return (
    typeof value.page_id === "string" &&
    typeof value.url === "string" &&
    typeof value.title === "string" &&
    nullableString("category") &&
    nullableString("state") &&
    nullableString("published_at")
  );
}

async function getJson(path: string, signal?: AbortSignal): Promise<unknown> {
  const response = await bffFetch(path, {
    cache: "no-store",
    headers: { Accept: "application/json" },
    signal,
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  return body;
}

export async function fetchNotionReports(limit = 20): Promise<NotionReportListResponse> {
  const body = await getJson(`/ui/notion/reports?limit=${encodeURIComponent(String(limit))}`);
  if (
    !isRecord(body) ||
    body.schema_version !== "ui.notion-reports.v1" ||
    body.source !== "notion" ||
    !Array.isArray(body.reports) ||
    !body.reports.every(hasReportCardFields)
  ) {
    throw new Error("Notion 리포트 목록 계약이 올바르지 않습니다.");
  }
  return body as unknown as NotionReportListResponse;
}

export async function fetchNotionReport(
  pageId: string,
  signal?: AbortSignal,
): Promise<NotionReportDetail> {
  const body = await getJson(
    `/ui/notion/reports/${encodeURIComponent(pageId)}`,
    signal,
  );
  if (
    !hasReportCardFields(body) ||
    body.schema_version !== "ui.notion-report.v1" ||
    typeof body.markdown !== "string"
  ) {
    throw new Error("Notion 리포트 계약이 올바르지 않습니다.");
  }
  return body as unknown as NotionReportDetail;
}
