/**
 * HR Roster client — BFF `/ui/workforce/roster` 폴링.
 *
 * 등록된 Agent 전원의 고용 상태·현재 Profile Version·모델 좌표를 그대로
 * 보여준다. DATABASE_URL이 없으면 workforce-api가 501을 정직하게 돌려주고,
 * 이 client는 그 실패를 빈 목록으로 바꿔치기하지 않는다.
 */

import { BFF, bffFetch } from "./bffClient";

export type EmploymentStatus = "CANDIDATE" | "PROBATION" | "ACTIVE" | "SUSPENDED" | "RETIRED";
export type ProfileVersionStatus = "DRAFT" | "EVALUATING" | "APPROVED" | "ACTIVE" | "SUSPENDED" | "RETIRED";

export type RosterModelRef = {
  provider: string;
  model_name: string;
  model_version: string | null;
};

export type RosterProfileVersion = {
  profile_version_id: string;
  version: number;
  model: RosterModelRef;
  memory_namespace: string;
  status: ProfileVersionStatus | string;
};

export type RosterAgent = {
  agent_id: string;
  employee_code: string;
  display_name: string;
  department_code: string;
  role_code: string;
  employment_status: EmploymentStatus | string;
  current_version: number;
  current_profile_version: RosterProfileVersion | null;
  owner_user_id: string | null;
  backup_owner_user_id: string | null;
};

export type WorkforceRoster = {
  agents: RosterAgent[];
};

export class WorkforceRosterError extends Error {
  /** DB 미설정(501)과 실제 장애를 화면이 구분해서 안내한다. */
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
  return `Roster 조회 실패 (HTTP ${status})`;
}

function hasRosterShape(value: unknown): value is WorkforceRoster {
  if (typeof value !== "object" || value === null) return false;
  return Array.isArray((value as Record<string, unknown>).agents);
}

export async function fetchWorkforceRoster(): Promise<WorkforceRoster> {
  let response: Response;
  try {
    response = await bffFetch("/ui/workforce/roster", {
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch {
    throw new WorkforceRosterError(
      `BFF(${BFF})에 연결하지 못했습니다. FastAPI BFF가 실행 중인지 확인하세요.`,
      0,
    );
  }

  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new WorkforceRosterError(explain(body, response.status), response.status);
  if (!hasRosterShape(body)) {
    throw new WorkforceRosterError("Roster 응답 계약이 올바르지 않습니다.", response.status);
  }
  return body;
}
