"use client";

import { useQuery } from "@tanstack/react-query";
import {
  fetchHiringRequests,
  fetchImprovementCandidates,
  fetchWorkforcePlans,
  WorkforceLifecycleError,
  type HiringRequest,
  type ImprovementCandidate,
  type WorkforcePlan,
} from "../lib/workforceLifecycleClient";

/**
 * HR 조직 구성·개선 산출물 카드 - 인력 계획(Plan)·채용 제안(Hiring)·자기 개선
 * 후보(Improvement) 세 컬럼을 나란히 그린다.
 *
 * 셋은 서로 행 단위로 엮이는 관계가 아니라 각자 독립된 생명주기다(칸반의 컬럼
 * 세 개와 같은 구성) - 그래서 백엔드에 하나로 조인한 API를 두지 않고, 컬럼마다
 * 각자의 BFF 프록시를 부른다. 읽기 전용이다 - 상태 전이(승인/배포/롤백)는 이
 * 화면에서 실행하지 않는다(브라우저가 워크포스 판정·전이 API를 직접 호출하지
 * 않는다는 원칙, ai-office/CLAUDE.md).
 */

const POLL_MS = 120_000;

type Tone = "neutral" | "progress" | "success" | "error";

const TONE_CLASS: Record<Tone, string> = {
  neutral: "border-outline-variant bg-surface-container text-on-surface-variant",
  progress: "border-primary/30 bg-secondary-container text-primary",
  success: "border-tertiary-fixed-dim bg-tertiary-fixed/30 text-on-tertiary-fixed-variant",
  error: "border-error/40 bg-error-container text-on-error-container",
};

/** 세 상태 집합(HiringRequestStatus/CandidateStatus/WorkforcePlanStatus)이 서로
 *  달라도 키워드로 같은 네 색으로 묶는다 - 도메인마다 다른 tone map을 세 벌
 *  유지하지 않는다. */
function toneFor(status: string): Tone {
  const upper = status.toUpperCase();
  if (/(REJECT|ROLLED_BACK|HOLD)/.test(upper)) return "error";
  if (/(ACTIVE|APPROVED|DEPLOYED|KEPT)/.test(upper)) return "success";
  if (/(EVALUATING|SHADOW|PENDING|^OPEN$|OBSERVING)/.test(upper)) return "progress";
  return "neutral";
}

function StatusBadge({ status }: { status: string }) {
  return (
    <span
      className={`inline-flex items-center whitespace-nowrap rounded-full border px-2 py-0.5 text-[10px] font-semibold ${TONE_CLASS[toneFor(status)]}`}
    >
      {status}
    </span>
  );
}

function LifecycleArtifactHeader() {
  return (
    <div className="flex items-center justify-between gap-3 border-b border-outline-variant bg-surface-container-low px-4 py-2.5">
      <span className="flex min-w-0 items-center gap-2 text-label-md font-label-md text-on-surface-variant">
        <span className="material-symbols-outlined text-[16px]" aria-hidden="true">
          view_kanban
        </span>
        <span className="truncate">workforce.lifecycle</span>
      </span>
      <span className="inline-flex items-center whitespace-nowrap rounded-full border border-outline-variant bg-surface-container-lowest px-2.5 py-0.5 text-[10px] font-semibold text-on-surface-variant">
        HR 관측
      </span>
    </div>
  );
}

function ColumnShell({
  icon,
  title,
  subtitle,
  count,
  error,
  loading,
  empty,
  children,
}: {
  icon: string;
  title: string;
  subtitle: string;
  count?: number;
  error: WorkforceLifecycleError | null;
  loading: boolean;
  empty: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="flex min-w-0 flex-col gap-1.5 rounded-lg border border-outline-variant bg-surface p-2.5">
      <div className="flex items-center justify-between gap-2">
        <span className="flex min-w-0 items-center gap-1.5 text-body-sm font-body-sm font-bold text-on-surface">
          <span className="material-symbols-outlined text-[16px] text-on-surface-variant" aria-hidden="true">
            {icon}
          </span>
          {title}
        </span>
        {count !== undefined ? (
          <span className="shrink-0 font-data-mono text-[11px] text-on-surface-variant">{count}건</span>
        ) : null}
      </div>
      <p className="m-0 text-[11px] text-on-surface-variant">{subtitle}</p>

      {error ? (
        <p
          className={`m-0 rounded border p-2 text-[11px] ${
            error.status === 501 || error.status === 503
              ? "border-outline-variant bg-surface-container-low text-on-surface-variant"
              : "border-error/40 bg-error-container text-on-error-container"
          }`}
        >
          {error.message}
        </p>
      ) : null}

      {loading && !error ? <p className="m-0 text-[11px] text-on-surface-variant">불러오는 중입니다…</p> : null}

      {!loading && !error && empty ? (
        <p className="m-0 text-[11px] text-on-surface-variant">등록된 항목이 없습니다.</p>
      ) : null}

      {!error ? <div className="flex flex-col gap-2">{children}</div> : null}
    </div>
  );
}

function HiringColumn() {
  const query = useQuery<{ hiring_requests: HiringRequest[] }, WorkforceLifecycleError>({
    queryKey: ["workforce-hiring-requests"],
    queryFn: () => fetchHiringRequests(),
    refetchInterval: POLL_MS,
    staleTime: 0,
    retry: false,
  });
  const requests = query.data?.hiring_requests ?? [];

  return (
    <ColumnShell
      icon="person_add"
      title="채용 제안 (Hiring)"
      subtitle="DRAFT/OPEN → EVALUATING → APPROVED/REJECTED → CLOSED"
      count={query.data ? requests.length : undefined}
      error={query.error ?? null}
      loading={query.isPending}
      empty={requests.length === 0}
    >
      {requests.map((request) => (
        <article key={request.request_id} className="rounded-md border border-outline-variant bg-surface-container-low p-2">
          <div className="flex items-start justify-between gap-2">
            <span className="min-w-0 truncate font-data-mono text-[11px] text-on-surface-variant" title={request.department_id}>
              {request.department_id}
            </span>
            <StatusBadge status={request.status} />
          </div>
          <p className="m-0 mt-1 line-clamp-2 text-xs text-on-surface">{request.business_problem}</p>
          <p className="m-0 mt-1 text-[10px] text-on-surface-variant">
            제안 {request.requested_by} · {new Date(request.created_at).toLocaleDateString("ko-KR")}
            {request.decided_by ? ` · 결정 ${request.decided_by}` : ""}
          </p>
        </article>
      ))}
    </ColumnShell>
  );
}

function ImprovementColumn() {
  const query = useQuery<{ candidates: ImprovementCandidate[] }, WorkforceLifecycleError>({
    queryKey: ["workforce-improvements"],
    queryFn: () => fetchImprovementCandidates(),
    refetchInterval: POLL_MS,
    staleTime: 0,
    retry: false,
  });
  const candidates = query.data?.candidates ?? [];

  return (
    <ColumnShell
      icon="auto_fix_high"
      title="자기 개선 후보 (Improvement)"
      subtitle="PROPOSED → EVALUATING/SHADOW → PENDING_APPROVAL → APPROVED → DEPLOYED → OBSERVING"
      count={query.data ? candidates.length : undefined}
      error={query.error ?? null}
      loading={query.isPending}
      empty={candidates.length === 0}
    >
      {candidates.map((candidate) => (
        <article key={candidate.candidate_id} className="rounded-md border border-outline-variant bg-surface-container-low p-2">
          <div className="flex items-start justify-between gap-2">
            <span className="min-w-0 truncate font-data-mono text-[11px] text-on-surface-variant" title={candidate.target_ref}>
              {candidate.target_type} · {candidate.target_ref}
            </span>
            <StatusBadge status={candidate.status} />
          </div>
          <p className="m-0 mt-1 line-clamp-2 text-xs text-on-surface">{candidate.expected_effect}</p>
          <p className="m-0 mt-1 text-[10px] text-on-surface-variant">
            제안 {candidate.author} · 위험도 {candidate.risk_class} · 롤백 v{candidate.rollback_target_version}
          </p>
        </article>
      ))}
    </ColumnShell>
  );
}

function PlanColumn() {
  const query = useQuery<{ workforce_plans: WorkforcePlan[] }, WorkforceLifecycleError>({
    queryKey: ["workforce-plans"],
    queryFn: () => fetchWorkforcePlans(),
    refetchInterval: POLL_MS,
    staleTime: 0,
    retry: false,
  });
  const plans = query.data?.workforce_plans ?? [];

  return (
    <ColumnShell
      icon="calendar_month"
      title="인력 계획 (Plan)"
      subtitle="DRAFT → APPROVED(CEO 승인 필요) → ACTIVE → RETIRED"
      count={query.data ? plans.length : undefined}
      error={query.error ?? null}
      loading={query.isPending}
      empty={plans.length === 0}
    >
      {plans.map((plan) => (
        <article key={plan.plan_id} className="rounded-md border border-outline-variant bg-surface-container-low p-2">
          <div className="flex items-start justify-between gap-2">
            <span className="min-w-0 truncate font-data-mono text-[11px] text-on-surface-variant" title={plan.department_id}>
              {plan.department_id}
            </span>
            <StatusBadge status={plan.status} />
          </div>
          <p className="m-0 mt-1 text-[10px] text-on-surface-variant">
            {new Date(plan.period_start).toLocaleDateString("ko-KR")} ~{" "}
            {new Date(plan.period_end).toLocaleDateString("ko-KR")}
          </p>
          {Object.keys(plan.skill_gaps).length > 0 ? (
            <p className="m-0 mt-1 line-clamp-2 text-xs text-on-surface">
              스킬 격차: {Object.entries(plan.skill_gaps).map(([k, v]) => `${k} ${v}`).join(", ")}
            </p>
          ) : null}
        </article>
      ))}
    </ColumnShell>
  );
}

/**
 * 조직 구성 및 개선(그룹3) - Plan/Hiring/Improvement 세 컬럼.
 * 데이터는 HR이 항상 회사 전체를 보는 관측이라 부서 필터 없이 그대로 표시한다.
 */
export default function WorkforceLifecyclePanel() {
  return (
    <section
      className="min-w-0 overflow-hidden rounded-lg border border-outline-variant bg-surface-container-lowest shadow-sm"
      aria-labelledby="workforce-lifecycle-title"
    >
      <LifecycleArtifactHeader />
      <div className="space-y-2 px-4 py-3">
        <div className="min-w-0">
          <h2 id="workforce-lifecycle-title" className="m-0 text-title-sm font-title-sm font-bold text-primary">
            조직 구성 및 개선
          </h2>
          <p className="mt-0.5 max-w-3xl text-[11px] leading-snug text-on-surface-variant">
            인력 계획(Plan)에 따라 채용(Hiring)을 제안하고, 기존 인력의 개선 후보(Improvement)를 검토하는 흐름입니다.
            읽기 전용이며, 승인·전이는 이 화면에서 실행하지 않습니다.
          </p>
        </div>

        <div className="grid grid-cols-1 gap-2 lg:grid-cols-3">
          <PlanColumn />
          <HiringColumn />
          <ImprovementColumn />
        </div>

        <div className="flex flex-wrap items-center justify-between gap-x-4 gap-y-1 border-t border-outline-variant pt-2 text-[11px] text-on-surface-variant">
          <span>workforce.workforce_plans / hiring_requests / improvement_candidates 기준</span>
          <span>{POLL_MS / 1000}초마다 자동 갱신</span>
        </div>
      </div>
    </section>
  );
}
