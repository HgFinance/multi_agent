"use client";

import {
  readableRuntimeStatus,
  type OperationsDepartment,
  type OperationsView,
} from "../lib/operationsClient";
import AccountingLedgerPanel from "../components/AccountingLedgerPanel";
import AlphaStrategyPanel from "../components/AlphaStrategyPanel";
import LivePortfolioPanel from "../components/LivePortfolioPanel";
import ResearchPanel from "../components/ResearchPanel";
import WorkerPerformancePanel from "../components/WorkerPerformancePanel";
import WorkforceIdleAgentsPanel from "../components/WorkforceIdleAgentsPanel";
import WorkforceLifecyclePanel from "../components/WorkforceLifecyclePanel";
import WorkforceRosterPanel from "../components/WorkforceRosterPanel";

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

/** HR 산출물 3단 그룹(직원 현황/성과·효율성/조직 구성·개선) 구분용 소제목. */
function GroupHeading({ index, title, hint }: { index: number; title: string; hint: string }) {
  return (
    <div className="flex items-baseline gap-2 px-1">
      <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-primary/15 text-[11px] font-bold text-primary">
        {index}
      </span>
      <h3 className="m-0 text-body-md font-body-md font-bold text-on-surface">{title}</h3>
      <span className="text-[11px] text-on-surface-variant">{hint}</span>
    </div>
  );
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

  const running = workers.filter((w) => String(w.status).toUpperCase() === "RUNNING").length;

  const meta = [
    { label: "등록 Worker", value: workers.length },
    { label: "업무 중", value: running },
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

      {department.domain === "trading" ? <LivePortfolioPanel /> : null}
      {/* 회계는 주문 상태가 아니라 확정된 거래와 비용을 본다. 같은 패널을
          돌려 쓰면 미확정 주문이 장부 화면에 올라온다. */}
      {department.domain === "accounting" ? <AccountingLedgerPanel /> : null}
      {department.domain === "workforce" ? (
        <div className="flex flex-col gap-6">
          <div className="flex flex-col gap-3">
            <GroupHeading index={1} title="직원 현황" hint="Roster · 행을 클릭하면 Access가 펼쳐집니다" />
            <WorkforceRosterPanel />
          </div>
          <div className="flex flex-col gap-3">
            <GroupHeading
              index={2}
              title="성과 및 효율성"
              hint="Quality+Capacity+Cost 통합 Scorecard 연동 예정"
            />
            <WorkerPerformancePanel metrics={data.metrics} />
            <WorkforceIdleAgentsPanel />
          </div>
          <div className="flex flex-col gap-3">
            <GroupHeading index={3} title="조직 구성 및 개선" hint="Plan · Hiring · Improvement" />
            <WorkforceLifecyclePanel />
          </div>
        </div>
      ) : null}
      {/* 퀀트는 채택된 알파 전략 - 지금 실제로 도는 페이퍼 컨테이너 1개를 본다
          (`strategy.strategies` 레지스트리는 아직 아무도 안 쓴다). */}
      {department.domain === "quant" ? <AlphaStrategyPanel /> : null}
      {/* 내용은 비워 두고 프레임만 유지 - 채울 내용은 추후 결정. */}
      {department.domain === "research" ? <ResearchPanel /> : null}
    </section>
  );
}
