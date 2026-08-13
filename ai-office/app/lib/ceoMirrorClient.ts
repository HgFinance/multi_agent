/**
 * Web client adapter for the shared Web/Discord CEO timeline.
 * UI components can consume this without knowing Hermes or Kanban internals.
 */

import { BFF } from "./ceoClient";

export type MirrorSource = "web" | "discord";
export type MirrorLane = "execution" | "evaluation";
export type MirrorEventType =
  | "USER_MESSAGE"
  | "CEO_PLAN_CREATED"
  | "TASK_CREATED"
  | "TASK_ASSIGNED"
  | "TASK_STARTED"
  | "TASK_PROGRESS"
  | "TOOL_STARTED"
  | "TOOL_COMPLETED"
  | "TASK_COMPLETED"
  | "TASK_FAILED"
  | "RETRY_STARTED"
  | "CEO_SYNTHESIS_STARTED"
  | "CEO_FINAL"
  | "QA_STARTED"
  | "QA_RESULT"
  | "HR_EVALUATION"
  | "IMPROVEMENT_CANDIDATE"
  | "REGRESSION_RESULT"
  | "PROMOTION_RESULT";

export type CeoMirrorEvent = {
  schema_version: "ui.ceo-mirror-event.v1";
  event_id: string;
  request_id: string;
  task_id: string | null;
  parent_task_id: string | null;
  source: MirrorSource;
  source_message_id: string;
  actor_id: string;
  actor_type: "user" | "bot" | "agent" | "system";
  lane: MirrorLane;
  event_type: MirrorEventType;
  status: string;
  summary: string;
  payload: Record<string, unknown>;
  created_at: string;
};

export type CeoMirrorEventsResponse = {
  schema_version: "ui.ceo-mirror-events.v1";
  request_id: string;
  events: CeoMirrorEvent[];
  next_cursor: string | null;
};

export type CeoMirrorIngress = {
  query: string;
  request_id: string;
  source: MirrorSource;
  source_message_id: string;
  actor_id: string;
  actor_type?: "user" | "bot" | "agent" | "system";
  mirrored?: boolean;
};

export async function submitCeoMirror(input: CeoMirrorIngress) {
  const response = await fetch(`${BFF}/ui/ceo/ingress`, {
    method: "POST",
    cache: "no-store",
    headers: { "Content-Type": "application/json", Accept: "application/json" },
    body: JSON.stringify(input),
  });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(`CEO mirror ingress failed (HTTP ${response.status})`);
  return body as {
    request_id: string;
    source: MirrorSource;
    task_id: string | null;
    duplicate: boolean;
    ignored: boolean;
  };
}

export async function readCeoMirrorEvents(requestId: string, after?: string) {
  const params = new URLSearchParams({ request_id: requestId });
  if (after) params.set("after", after);
  const response = await fetch(`${BFF}/ui/ceo/events?${params}`, { cache: "no-store" });
  if (!response.ok) throw new Error(`CEO mirror events failed (HTTP ${response.status})`);
  return (await response.json()) as CeoMirrorEventsResponse;
}

export function subscribeCeoMirror(
  requestId: string,
  onEvent: (event: CeoMirrorEvent) => void,
  after?: string,
) {
  const params = new URLSearchParams({ request_id: requestId });
  if (after) params.set("after", after);
  const source = new EventSource(`${BFF}/ui/ceo/events/stream?${params}`);
  const seen = new Set<string>();
  const eventTypes: MirrorEventType[] = [
    "USER_MESSAGE", "CEO_PLAN_CREATED", "TASK_CREATED", "TASK_ASSIGNED",
    "TASK_STARTED", "TASK_PROGRESS", "TOOL_STARTED", "TOOL_COMPLETED",
    "TASK_COMPLETED", "TASK_FAILED", "RETRY_STARTED", "CEO_SYNTHESIS_STARTED",
    "CEO_FINAL", "QA_STARTED", "QA_RESULT", "HR_EVALUATION",
    "IMPROVEMENT_CANDIDATE", "REGRESSION_RESULT", "PROMOTION_RESULT",
  ];
  for (const eventType of eventTypes) {
    source.addEventListener(eventType, (message) => {
      const event = JSON.parse((message as MessageEvent).data) as CeoMirrorEvent;
      if (seen.has(event.event_id)) return;
      seen.add(event.event_id);
      onEvent(event);
    });
  }
  return () => source.close();
}
