"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  addDepartmentLangsmithFeedback,
  fetchDepartmentLangsmithFeedback,
  type DepartmentFeedbackItem,
} from "../lib/langsmithClient";

function FeedbackItem({ department, item }: { department: string; item: DepartmentFeedbackItem }) {
  const [comment, setComment] = useState("");
  const queryClient = useQueryClient();
  const mutation = useMutation({
    mutationFn: () => addDepartmentLangsmithFeedback(department, item.artifact_id, comment),
    onSuccess: () => {
      setComment("");
      void queryClient.invalidateQueries({ queryKey: ["department-langsmith-feedback", department] });
    },
  });

  return (
    <article className="rounded-md border border-outline-variant bg-surface px-3 py-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <p className="m-0 text-sm font-semibold text-on-surface">{item.finding_codes.join(" · ") || "운영 finding"}</p>
          <p className="m-0 text-[11px] text-on-surface-variant">
            {new Date(item.created_at).toLocaleString("ko-KR")} · 부서 리뷰 {item.review_count}건
          </p>
        </div>
        <span className="font-data-mono text-[10px] text-outline">{item.artifact_id}</span>
      </div>
      <p className="m-0 mt-2 text-xs text-on-surface-variant">{item.summaries.join(" · ")}</p>
      {item.reviews.length > 0 ? (
        <div className="mt-2 space-y-1 border-l-2 border-primary/30 pl-2">
          {item.reviews.slice(-3).map((review) => (
            <p key={review.review_id} className="m-0 text-xs text-on-surface-variant">
              {review.comment} · {new Date(review.created_at).toLocaleString("ko-KR")}
            </p>
          ))}
        </div>
      ) : null}
      <div className="mt-2 flex gap-2">
        <textarea
          className="min-h-12 flex-1 rounded border border-outline-variant bg-surface-container-low px-2 py-1 text-xs"
          value={comment}
          maxLength={1200}
          onChange={(event) => setComment(event.target.value)}
          placeholder="이 부서의 trace에 대한 피드백을 남겨주세요"
          aria-label={`${department} feedback`}
        />
        <button
          type="button"
          className="self-end rounded bg-secondary-container px-3 py-2 text-xs font-semibold text-on-secondary-container disabled:opacity-50"
          disabled={mutation.isPending || !comment.trim()}
          onClick={() => mutation.mutate()}
        >
          저장
        </button>
      </div>
      {mutation.isError ? <p className="mt-2 text-xs text-error">{mutation.error.message}</p> : null}
    </article>
  );
}

export default function DepartmentLangsmithFeedbackPanel({ department }: { department: string }) {
  const query = useQuery({
    queryKey: ["department-langsmith-feedback", department],
    queryFn: () => fetchDepartmentLangsmithFeedback(department, 50),
    refetchInterval: 60_000,
    staleTime: 30_000,
    retry: false,
  });
  const items = query.data?.items ?? [];

  return (
    <section
      className="rounded-lg border border-outline-variant bg-surface-container-lowest p-4"
      aria-labelledby={`department-feedback-${department}`}
    >
      <div className="flex items-center justify-between gap-2">
        <h3 id={`department-feedback-${department}`} className="m-0 text-sm font-semibold text-on-surface">
          부서 LangSmith 피드백
        </h3>
        <span className="text-xs text-on-surface-variant">{items.length}건</span>
      </div>
      {query.isError ? (
        <p className="mt-3 text-xs text-on-surface-variant">{query.error.message}</p>
      ) : items.length === 0 ? (
        <p className="mt-3 text-xs text-on-surface-variant">이 부서에 기록된 redacted finding이 없습니다.</p>
      ) : (
        <div className="mt-3 space-y-2">
          {items.map((item) => <FeedbackItem key={item.artifact_id} department={department} item={item} />)}
        </div>
      )}
    </section>
  );
}
