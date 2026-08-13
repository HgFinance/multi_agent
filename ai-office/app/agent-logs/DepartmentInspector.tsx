"use client";

import {
  departmentStage,
  readableRuntimeStatus,
  type OperationsDepartment,
  type OperationsView,
} from "../lib/operationsClient";

/**
 * 선택한 부서 한 곳의 직원 Registry·내부 메시지·LLM 성과.
 *
 * 파생 규칙은 main `ops/DepartmentCommunicationPanel.tsx` 그대로다 —
 * Registry의 Worker에 실시간 이벤트(agent.status.v1)와 runtime active_workers를
 * 겹쳐 상태를 정한다. 이벤트가 없으면 REGISTERED("등록됨")이고, 그건
 * "실행 중"이 아니라 "아직 실행 이벤트를 못 받았다"는 뜻이다.
 */

const STATUS_TONE: Record<string, string> = {
  RUNNING: "border-primary/30 bg-secondary-container text-primary",
  QUEUED: "border-outline-variant bg-surface-container text-on-surface-variant",
  REGISTERED: "border-outline-variant bg-surface-container-lowest text-on-surface-variant",
  IDLE: "border-outline-variant bg-surface-container text-on-surface-variant",
  WAITING_APPROVAL: "border-primary/30 bg-secondary-container text-primary",
  COMPLETED: "border-tertiary-fixed-dim bg-tertiary-fixed/30 text-on-tertiary-fixed-variant",
  OFFLINE: "border-outline-variant bg-surface-container-high text-on-surface-variant",
  DEGRADED: "border-error/40 bg-error-container text-on-error-container",
  BLOCKED: "border-error/40 bg-error-container text-on-error-container",
  ERROR: "border-error/40 bg-error-container text-on-error-container",
};

function tone(status: string) {
  return STATUS_TONE[String(status).toUpperCase()] ?? STATUS_TONE.IDLE;
}

function Disclosure({
  title,
  meta,
  open,
  children,
}: {
  title: string;
  meta: string;
  open?: boolean;
  children: React.ReactNode;
}) {
  return (
    <details open={open} className="border border-outline-variant rounded-lg bg-surface-container-lowest">
      <summary className="cursor-pointer list-none px-4 py-3 flex justify-between items-center gap-2">
        <span className="text-body-md font-body-md font-bold text-on-surface">{title}</span>
        <span className="text-xs text-on-surface-variant shrink-0">{meta}</span>
      </summary>
      <div className="px-4 pb-4">{children}</div>
    </details>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return <p className="text-body-sm font-body-sm text-on-surface-variant m-0 py-2">{children}</p>;
}

export default function DepartmentInspector({
  department,
  data,
}: {
  department: OperationsDepartment;
  data: OperationsView;
}) {
  const code = department.department_code;

  const liveByWorker = new Map(
    data.agentStatuses.filter((agent) => agent.department_code === code).map((agent) => [agent.worker_id, agent]),
  );

  const workers = [...new Map((department.workers ?? []).map((w) => [w.worker_id, w])).values()].map((worker) => {
    const live = liveByWorker.get(worker.worker_id);
    const active = data.activeWorkers.some(
      (item) => item.worker_id === worker.worker_id && item.department_code === code,
    );
    return {
      ...worker,
      roleLabel:
        live?.role && live.role !== worker.worker_id
          ? live.role
          : worker.runtime_kind === "llm"
            ? "LLM 직원"
            : "결정론 Runner",
      status: live?.status ?? (active ? "RUNNING" : "REGISTERED"),
      reason:
        live?.reason ??
        (active
          ? "LangGraph runtime에서 실행 중입니다."
          : "Registry에 등록됨 · agent.status.v1 실행 이벤트 수신 전입니다."),
    };
  });

  const messages = data.messages.filter((m) => m.department_code === code && m.worker_id);
  const metrics = data.metrics.filter((m) => m.stage === departmentStage(department));
  const running = workers.filter((w) => String(w.status).toUpperCase() === "RUNNING").length;

  const meta = [
    { label: "등록 Worker", value: workers.length },
    { label: "업무 중", value: running },
    { label: "내부 메시지", value: messages.length },
    { label: "LLM 성과", value: metrics.length },
  ];

  return (
    <section
      id="department-inspector"
      aria-label={`${department.name} 상세`}
      className="bg-surface-container-lowest border border-outline-variant rounded-lg p-6 flex flex-col gap-4"
    >
      <div className="flex justify-between items-start gap-4">
        <div className="min-w-0">
          <span className="block text-label-md font-label-md text-on-surface-variant uppercase">Selected Department</span>
          <h2 className="text-headline-md font-headline-md text-primary mt-1">{department.name}</h2>
          <code className="block text-xs text-outline">{code}</code>
          <p className="text-xs text-outline m-0 mt-1">
            {department.executor ?? "LangGraph"} · {department.worker_model ?? "qwen3:1.7b"} ·{" "}
            {department.output_contract ?? "worker-context.v1"}
          </p>
        </div>
        <span className={`shrink-0 px-3 py-1 rounded-full border text-xs font-medium ${tone(department.status)}`}>
          {readableRuntimeStatus(department.status)}
        </span>
      </div>

      <div className="flex flex-wrap gap-2">
        {meta.map((item) => (
          <span
            key={item.label}
            className="px-3 py-1.5 rounded border border-outline-variant bg-surface text-body-sm font-body-sm text-on-surface-variant"
          >
            {item.label} <b className="font-data-mono text-on-surface">{item.value}</b>
          </span>
        ))}
      </div>

      <Disclosure title="직원 Registry + 실시간 상태" meta={`${workers.length}명 · 업무 중 ${running}명`} open>
        {workers.length > 0 ? (
          <div className="grid grid-cols-1 lg:grid-cols-2 gap-3">
            {workers.map((worker) => (
              <article key={worker.worker_id} className="border border-outline-variant rounded-lg p-4 bg-surface">
                <div className="flex justify-between items-start gap-2">
                  <strong className="text-body-lg font-body-lg font-bold text-on-surface break-all">
                    {worker.worker_id}
                  </strong>
                  <span className={`shrink-0 px-3 py-1 rounded-full border text-xs font-medium ${tone(worker.status)}`}>
                    {readableRuntimeStatus(worker.status)}
                  </span>
                </div>
                <small className="block text-body-sm font-body-sm text-on-surface-variant mt-1">
                  {worker.roleLabel} · {worker.trigger ?? "호출 조건 없음"}
                </small>
                <p className="text-body-sm font-body-sm text-on-surface-variant m-0 mt-2">{worker.reason}</p>
              </article>
            ))}
          </div>
        ) : (
          <Empty>이 부서의 Worker Registry를 기다리는 중입니다.</Empty>
        )}
      </Disclosure>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <Disclosure title="부서 내부 메시지" meta={`${messages.length}개`}>
          {messages.length > 0 ? (
            <div className="flex flex-col gap-2">
              {messages.map((message) => (
                <div key={message.id} className="flex gap-2 text-body-sm font-body-sm">
                  <code className="text-xs text-outline shrink-0">{message.worker_id}</code>
                  <span className="text-on-surface min-w-0">{message.text}</span>
                </div>
              ))}
            </div>
          ) : (
            <Empty>실제 내부 메시지가 아직 없습니다.</Empty>
          )}
        </Disclosure>

        <Disclosure title="LLM 성과 · 원문 비활성화" meta={`${metrics.length}개 metric`}>
          {metrics.length > 0 ? (
            <div className="flex flex-col gap-2">
              {metrics.map((metric) => (
                <div
                  key={`${metric.worker_id}-${metric.latency_ms}-${metric.attempts}`}
                  className="flex flex-wrap gap-x-3 gap-y-1 text-body-sm font-body-sm"
                >
                  <b className="text-on-surface">{metric.worker_id}</b>
                  <span className="text-on-surface-variant">{metric.model_name}</span>
                  <span className="font-data-mono text-on-surface-variant">{metric.latency_ms}ms</span>
                  <span className="text-on-surface-variant">
                    eval {metric.eval_score == null ? "—" : metric.eval_score.toFixed(2)}
                  </span>
                </div>
              ))}
            </div>
          ) : (
            <Empty>Worker 실행 후 정량 성과가 표시됩니다.</Empty>
          )}
        </Disclosure>
      </div>

      <p className="text-xs text-outline text-center m-0">
        LangSmith Input/Output 원문은 정책상 비활성화되어 있으며, 정량 메타데이터와 해시 식별자만 추적합니다.
      </p>
    </section>
  );
}
