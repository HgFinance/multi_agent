"use client";

import type { OperationsView } from "../lib/operationsClient";

function formatDateTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "확인 중";
  return date.toLocaleString("ko-KR", { dateStyle: "medium", timeStyle: "short" });
}
function EmptyState({ children }: { children: React.ReactNode }) {
  return <p className="m-0 text-body-sm font-body-sm text-on-surface-variant">{children}</p>;
}

/** CEO 상세는 Agent Logs가 받은 OperationsView를 대표 관점으로 요약한다. */
export default function CeoOperationsPanel({ data }: { data: OperationsView }) {
  const recentMessages = [...data.messages]
    .filter((message) => message.department_code === null || message.department_code === "ceo-agent")
    .sort((left, right) => Date.parse(right.occurred_at) - Date.parse(left.occurred_at))
    .slice(0, 6);

  return (
    <section aria-label="CEO 주의사항과 최근 판단" className="rounded-lg border border-outline-variant bg-surface-container-lowest p-4">
        <div>
          <h4 className="m-0 text-body-lg font-body-lg font-bold text-on-surface">주의할 점과 최근 판단</h4>
          <p className="m-0 mt-1 text-body-sm font-body-sm text-on-surface-variant">
            지금 확인이 필요한 경고와 최근에 내려진 판단을 함께 보여줍니다.
          </p>
        </div>

        <div className="mt-4 grid grid-cols-1 gap-4 lg:grid-cols-2">
          <article className="rounded-lg border border-outline-variant bg-surface p-4">
            <h5 className="m-0 text-body-md font-body-md font-bold text-on-surface">주의할 일</h5>
            <div className="mt-3">
              {data.warnings.length > 0 ? (
                <ul className="m-0 list-disc space-y-2 pl-5 text-body-sm font-body-sm text-on-surface">
                  {data.warnings.map((warning, index) => <li key={warning + "-" + index}>{warning}</li>)}
                </ul>
              ) : (
                <EmptyState>현재 snapshot에 운영 경고가 없습니다.</EmptyState>
              )}
            </div>
          </article>

          <article className="rounded-lg border border-outline-variant bg-surface p-4">
            <h5 className="m-0 text-body-md font-body-md font-bold text-on-surface">최근에 내려진 판단</h5>
            <div className="mt-3 space-y-3">
              {recentMessages.length > 0 ? (
                recentMessages.map((message) => (
                  <div key={message.id} className="border-b border-outline-variant pb-3 last:border-b-0 last:pb-0">
                    <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-on-surface-variant">
                      <time dateTime={message.occurred_at}>{formatDateTime(message.occurred_at)}</time>
                    </div>
                    <p className="m-0 mt-1 text-body-sm font-body-sm text-on-surface">{message.text}</p>
                  </div>
                ))
              ) : (
                <EmptyState>최근 대표 지시·판정 메시지가 없습니다.</EmptyState>
              )}
            </div>
          </article>
        </div>
      </section>
  );
}
