"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  decideQaLangsmithFeedback,
  fetchQaLangsmithFeedback,
  type LangsmithFeedbackItem,
} from "../lib/langsmithClient";

function FeedbackCard({ item }: { item: LangsmithFeedbackItem }) {
  const [reason, setReason] = useState("QA reviewed metadata-only finding");
  const queryClient = useQueryClient();
  const decision = useMutation({
    mutationFn: (value: "APPROVED" | "REJECTED") =>
      decideQaLangsmithFeedback(item.artifact_id, value, reason),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ["qa-langsmith-feedback"] });
    },
  });

  return (
    <article className="rounded-md border border-outline-variant bg-surface px-3 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="m-0 text-sm font-semibold text-on-surface">{item.department}</p>
          <p className="m-0 text-[11px] text-on-surface-variant">
            {item.decision} · {new Date(item.created_at).toLocaleString("ko-KR")}
          </p>
        </div>
        <span className="font-data-mono text-[10px] text-outline">{item.artifact_id}</span>
      </div>
      <ul className="my-2 list-disc pl-5 text-xs text-on-surface-variant">
        {item.finding_codes.map((code) => <li key={code}>{code}</li>)}
      </ul>
      <p className="m-0 text-xs text-on-surface-variant">{item.summaries.join(" · ")}</p>
      <textarea
        className="mt-2 min-h-16 w-full rounded border border-outline-variant bg-surface-container-low px-2 py-1 text-xs"
        value={reason}
        maxLength={240}
        onChange={(event) => setReason(event.target.value)}
        aria-label="QA decision reason"
      />
      <div className="mt-2 flex gap-2">
        <button
          type="button"
          className="rounded bg-tertiary-container px-3 py-1.5 text-xs font-semibold text-on-tertiary-container disabled:opacity-50"
          disabled={decision.isPending || !reason.trim()}
          onClick={() => decision.mutate("APPROVED")}
        >
          승인
        </button>
        <button
          type="button"
          className="rounded bg-error-container px-3 py-1.5 text-xs font-semibold text-on-error-container disabled:opacity-50"
          disabled={decision.isPending || !reason.trim()}
          onClick={() => decision.mutate("REJECTED")}
        >
          반려
        </button>
      </div>
      {decision.isError ? <p className="mt-2 text-xs text-error">{decision.error.message}</p> : null}
    </article>
  );
}

export default function QaLangsmithFeedbackPanel() {
  const query = useQuery({
    queryKey: ["qa-langsmith-feedback"],
    queryFn: () => fetchQaLangsmithFeedback(50),
    refetchInterval: 30_000,
    staleTime: 15_000,
    retry: false,
  });

  const items = query.data?.items ?? [];
  return (
    <section
      className="rounded-lg border border-outline-variant bg-surface-container-lowest p-4"
      aria-labelledby="qa-langsmith-feedback-title"
    >
      <div className="flex items-center justify-between gap-2">
        <h3 id="qa-langsmith-feedback-title" className="m-0 text-sm font-semibold text-on-surface">
          QA observability review
        </h3>
        <span className="text-xs text-on-surface-variant">{items.length} pending</span>
      </div>
      {query.isError ? (
        <p className="mt-3 text-xs text-on-surface-variant">{query.error.message}</p>
      ) : items.length === 0 ? (
        <p className="mt-3 text-xs text-on-surface-variant">검토 대기 중인 redacted finding이 없습니다.</p>
      ) : (
        <div className="mt-3 space-y-2">
          {items.map((item) => <FeedbackCard key={item.artifact_id} item={item} />)}
        </div>
      )}
    </section>
  );
}
