"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import {
  askCeo,
  buildCeoProgress,
  ceoWorkflowResult,
  ceoWorkflowStatus,
  type CardOutcome,
  type CeoQueryPlanning,
} from "../lib/ceoClient";
import { subscribeToAccountChange } from "../lib/currentAccount";
import { PanelBar } from "./PanelBar";

/**
 * "CEO Control Room" 패널 - **단발 질의 입력란**.
 *
 * ## 왜 채팅창을 되돌렸나 (2026-08-18, PR #265 되돌림)
 *
 * 채팅형은 대화 이력을 `GET /ui/ceo/tasks?owner_id=`로 받고 **종료된 항목마다**
 * `GET /ui/ceo/tasks/{id}/result`를 한 번씩 더 불렀다. 이력 N개면 1+N 왕복이고,
 * 그 `/result` 하나하나가 BFF에서 `hermes kanban show`를 subprocess로 띄운다
 * (`apps/api/ceo_kanban_read.py`). 병렬화·캐시로도 왕복 수 자체는 못 줄인다 -
 * 구조가 N에 비례하기 때문이다.
 *
 * 그래서 이력을 화면에서 들고 있지 않기로 했다. **과거 대화는 Discord 채널이
 * 보관**하고(`apps/api/discord_read.py`), 이 패널은 질문을 보내는 일만 한다.
 * 여기서 보낸 질의는 그대로 Hermes Kanban에 카드로 남으므로 추적 경로가
 * 사라지는 것은 아니다.
 *
 * ## 그래도 남긴 것
 *
 * **방금 보낸 요청 하나**의 접수 결과·본부별 진행·최종 답변은 남긴다. 이걸까지
 * 빼면 전송 후 아무 반응이 없어서 성공했는지 알 수 없다. 누적되지 않으므로
 * (다음 질문을 보내면 교체된다) N+1이 다시 생기지 않는다.
 *
 * ## 세로 크기
 *
 * 결과 영역을 `320px`로 **고정**한다(`h`/`min-h`/`max-h` 동일). 채팅창일 때는
 * `min-h-[320px] max-h-[480px]`로 내용에 따라 늘어났는데, 옆 패널과 높이가
 * 어긋나므로 최소값이던 320px에 고정한다.
 *
 * `DashboardView.tsx`에서 분리해 둔 이유는 그대로다 - 다른 팀원이 같은 파일의
 * 다른 패널을 동시에 건드릴 때 merge conflict를 줄이기 위해서다.
 */

/** 입력란 아래에 보여주는 빠른 질문. 누르면 그대로 전송한다. */
const QUICK_QUESTIONS = [
  "오늘 전체 업무 현황을 요약해줘",
  "지금 막혀 있는 업무와 이유를 알려줘",
  "리서치팀의 최신 진행 상황을 브리핑해줘",
];

/** 카드 결말별 표시. 보드의 status가 아니라 "답이 됐는가" 기준이다.
 *  특히 NO_ANSWER는 보드에서 done으로 보이는 카드라 성공으로 읽으면 안 된다. */
const OUTCOME_VIEW: Record<CardOutcome, { label: string; tone: string }> = {
  QUEUED: { label: "대기", tone: "border-outline-variant bg-surface-container text-on-surface-variant" },
  RUNNING: { label: "진행 중", tone: "border-primary/30 bg-secondary-container text-primary" },
  ANSWERED: { label: "답변 완료", tone: "border-tertiary-fixed-dim bg-tertiary-fixed/30 text-on-tertiary-fixed-variant" },
  NO_ANSWER: { label: "결과 없음", tone: "border-error/40 bg-error-container text-on-error-container" },
  BLOCKED: { label: "막힘", tone: "border-error/40 bg-error-container text-on-error-container" },
  FAILED: { label: "실패", tone: "border-error/40 bg-error-container text-on-error-container" },
  STALE: { label: "정체", tone: "border-error/40 bg-error-container text-on-error-container" },
  NO_ASSIGNEE: { label: "담당 없음", tone: "border-error/40 bg-error-container text-on-error-container" },
};

/** 방금 보낸 요청 하나. 누적하지 않는다 - 다음 전송이 이 값을 교체한다. */
type SubmittedRequest = {
  taskId: string;
  query: string;
  answer: string;
  planning: CeoQueryPlanning | null;
};

export function CeoControlRoomChat() {
  const [draft, setDraft] = useState("");
  const [submitted, setSubmitted] = useState<SubmittedRequest | null>(null);

  // 계정을 바꾸면 방금 보낸 요청은 그 계정 것이 아니다. 이력이 없어졌으므로
  // 계정별 재조회도 없고, 표시 중인 결과만 비우면 된다.
  useEffect(
    () => subscribeToAccountChange(() => setSubmitted(null)),
    [],
  );

  const activeTaskId = submitted?.taskId ?? null;

  // 본부별 진행 — 10초 간격. `all_terminal`은 status+graph만으로 계산되므로
  // (요약 유무는 ANSWERED/NO_ANSWER만 가르고 둘 다 terminal) 아직 안 온
  // `resultQuery` 없이도 멈춤 여부를 판정할 수 있다.
  const statusQuery = useQuery({
    queryKey: ["ceo", "status", activeTaskId],
    queryFn: () => ceoWorkflowStatus(activeTaskId as string),
    enabled: Boolean(activeTaskId),
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return 10_000;
      return buildCeoProgress(data, null).all_terminal ? false : 10_000;
    },
  });

  // 최종 답변 — 15초 간격. Synthesis가 끝나 summary가 오면 멈춘다.
  const resultQuery = useQuery({
    queryKey: ["ceo", "result", activeTaskId],
    queryFn: () => ceoWorkflowResult(activeTaskId as string),
    enabled: Boolean(activeTaskId),
    refetchInterval: (query) => (query.state.data?.result?.summary ? false : 15_000),
  });

  const progress = useMemo(
    () => (statusQuery.data ? buildCeoProgress(statusQuery.data, resultQuery.data ?? null) : null),
    [statusQuery.data, resultQuery.data],
  );

  const sendMutation = useMutation({ mutationFn: (text: string) => askCeo(text) });

  async function send(text: string) {
    const value = text.trim();
    if (!value || sendMutation.isPending) return;
    setDraft("");
    try {
      const response = await sendMutation.mutateAsync(value);
      setSubmitted({
        taskId: response.task_id,
        query: value,
        answer: response.answer,
        planning: response.planning ?? null,
      });
    } catch {
      // 실패 원인은 `sendMutation.error`로 이미 들고 있다 - 아래 에러 배너가 보여준다.
    }
  }

  const busy = sendMutation.isPending;
  const error = sendMutation.isError
    ? sendMutation.error instanceof Error
      ? sendMutation.error.message
      : String(sendMutation.error)
    : "";

  return (
    <section className="lg:col-span-1 bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden shadow-sm flex flex-col">
      <PanelBar icon="terminal" title="CEO Control Room">
        <span className="flex items-center gap-1.5 text-xs text-on-surface-variant">
          <span className="w-2 h-2 rounded-full bg-tertiary-fixed-dim" aria-hidden="true" />
          Hermes endpoint
        </span>
      </PanelBar>

      {/* 세로 크기를 고정한다 - 내용이 많아도 늘어나지 않고 안에서 스크롤한다. */}
      <div
        className="p-4 flex flex-col gap-3 overflow-y-auto h-[320px] min-h-[320px] max-h-[320px]"
        aria-live="polite"
        aria-label="CEO 질의 결과"
      >
        {!submitted && !error ? (
          <div className="m-auto text-center px-2">
            <p className="text-body-sm font-body-sm text-on-surface-variant m-0">
              자연어로 질문하면 CEO Hermes가 업무를 만들고, 결과는 Kanban에서 추적됩니다.
            </p>
            <p className="text-xs text-outline mt-2 m-0">지난 대화는 Discord 채널에서 확인합니다.</p>
          </div>
        ) : null}

        {submitted ? (
          <>
            <div className="self-end max-w-[92%] rounded-lg border border-secondary-container bg-secondary-container p-3">
              <div className="font-bold text-body-sm font-body-sm text-primary mb-1">대표님</div>
              <p className="text-body-sm font-body-sm text-on-surface m-0 whitespace-pre-line">
                {submitted.query}
              </p>
            </div>

            <div className="self-start max-w-[92%] rounded-lg border border-outline-variant bg-surface-container-low p-3">
              <div className="font-bold text-body-sm font-body-sm text-primary mb-1">CEO Hermes</div>
              <p className="text-body-sm font-body-sm text-on-surface m-0 whitespace-pre-line">
                {submitted.answer}
              </p>
              {submitted.planning ? (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {submitted.planning.selected_departments.map((dept) => (
                    <span
                      key={dept}
                      className="px-2 py-0.5 rounded-full border border-outline-variant bg-surface-container text-[11px] text-on-surface-variant"
                    >
                      {dept}
                    </span>
                  ))}
                  {submitted.planning.qa_required ? (
                    <span className="px-2 py-0.5 rounded-full border border-primary/30 bg-secondary-container text-[11px] text-primary">
                      QA 필요
                    </span>
                  ) : null}
                </div>
              ) : null}
              <code className="block text-right text-[10px] text-outline mt-1">{submitted.taskId}</code>
            </div>

            {progress?.final_answer ? (
              <div
                className="self-start max-w-[92%] border-2 border-primary/40 rounded p-3 bg-secondary-container/30"
                aria-live="polite"
              >
                <div className="flex items-center justify-between gap-2 mb-1">
                  <span className="text-label-md font-label-md text-primary uppercase">CEO 최종 답변</span>
                  {!progress.answer_grounded ? (
                    <span className="text-[10px] text-error">⚠️ 근거 미확인</span>
                  ) : null}
                </div>
                <p className="text-body-sm font-body-sm text-on-surface whitespace-pre-line m-0">
                  {progress.final_answer}
                </p>
              </div>
            ) : null}

            {progress ? (
              <div className="self-start max-w-[92%] border border-outline-variant rounded p-3" aria-live="polite">
                <div className="flex items-center justify-between gap-2 mb-2">
                  <span className="text-label-md font-label-md text-on-surface-variant uppercase">본부별 진행</span>
                  <span className="text-xs font-data-mono text-on-surface-variant">
                    {progress.finished}/{progress.total} 종료{progress.all_terminal ? "" : " · 확인 중"}
                  </span>
                </div>
                <ul className="m-0 p-0 list-none flex flex-col gap-1.5">
                  {progress.cards
                    .filter((card) => !card.is_root)
                    .map((card) => {
                      const view = OUTCOME_VIEW[card.outcome];
                      return (
                        <li key={card.task_id} className="text-xs">
                          <span className={`inline-block px-2 py-0.5 rounded-full border mr-2 ${view.tone}`}>{view.label}</span>
                          <span className="text-on-surface">{card.department}</span>
                          {card.summary ? (
                            <span className="block text-on-surface-variant mt-0.5 ml-1">{card.summary}</span>
                          ) : null}
                        </li>
                      );
                    })}
                </ul>
                {progress.unusable.length > 0 ? (
                  <p className="text-xs text-error mt-2 m-0">
                    ⚠️ {progress.unusable.length}개 본부가 사용 가능한 결과를 내지 못했습니다.
                  </p>
                ) : null}
                {progress.all_terminal && !progress.answer_grounded ? (
                  <p className="text-xs text-error mt-1 m-0">
                    ⚠️ 근거가 확인되지 않은 답변입니다. 그대로 결정에 쓰지 마세요.
                  </p>
                ) : null}
                {/* BFF 연결이 끊기면 폴링이 조용히 실패하지 않고 여기 남는다. */}
                {statusQuery.isError || resultQuery.isError ? (
                  <p className="text-xs text-error mt-1 m-0">
                    ⚠️ BFF에서 진행 상황을 가져오지 못했습니다. 재시도 중입니다.
                  </p>
                ) : null}
              </div>
            ) : null}
          </>
        ) : null}

        {error ? (
          <p role="alert" className="self-start max-w-[92%] text-xs text-error border border-error-container bg-error-container rounded p-2">
            ⚠️ {error}
          </p>
        ) : null}
      </div>

      <div className="px-4 pb-4 border-t border-outline-variant pt-3">
        <label className="block">
          <textarea
            value={draft}
            maxLength={2000}
            rows={3}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                event.preventDefault();
                void send(draft);
              }
            }}
            placeholder="예: 오늘 리스크가 큰 업무만 먼저 브리핑해줘"
            className="w-full p-3 bg-surface rounded border border-outline-variant focus:border-primary focus:ring-1 focus:ring-primary focus:outline-none text-body-sm font-body-sm resize-none"
          />
        </label>

        <div className="mt-2 flex flex-wrap gap-1.5">
          {QUICK_QUESTIONS.map((question) => (
            <button
              key={question}
              type="button"
              onClick={() => void send(question)}
              disabled={busy}
              className="px-2 py-0.5 rounded-full border border-outline-variant bg-surface-container-low text-[11px] leading-4 text-on-surface-variant hover:bg-surface-container transition-colors disabled:opacity-40"
            >
              {question}
            </button>
          ))}
        </div>

        <div className="flex justify-between items-center gap-3 mt-2">
          <span className="text-xs text-outline">{draft.length}/2000 · 조회와 자문만 수행합니다</span>
          <button
            type="button"
            onClick={() => void send(draft)}
            disabled={busy || !draft.trim()}
            className="px-4 py-2 bg-surface-container-high border border-outline-variant text-primary rounded font-bold text-label-md font-label-md hover:bg-surface-container-highest transition-colors disabled:opacity-40 shrink-0"
          >
            {busy ? "전달 중…" : "CEO에게 전달"}
          </button>
        </div>
      </div>
    </section>
  );
}
