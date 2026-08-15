"use client";

import { useMutation, useQueries, useQuery } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  askCeo,
  buildCeoProgress,
  ceoWorkflowResult,
  ceoWorkflowStatus,
  listCeoTasks,
  TERMINAL_WORKFLOW_STATUSES,
  type CardOutcome,
  type CeoQueryPlanning,
} from "../lib/ceoClient";
import { readStoredAccountId, subscribeToAccountChange } from "../lib/currentAccount";
import { PanelBar } from "./PanelBar";

/**
 * "CEO Control Room" 패널 - 채팅형 CEO 질의창.
 *
 * `DashboardView.tsx`에서 분리한 이유: 다른 팀원이 같은 파일의 다른 패널
 * (Hermes Kanban, 결과물 창고 등)을 동시에 건드리면 한 파일에서 두 사람의
 * 변경이 겹쳐 merge conflict가 난다. 이 컴포넌트는 자기 state·쿼리·핸들러를
 * 전부 안에 갖고 있어 `<CeoControlRoomChat />` 한 줄만 `DashboardView.tsx`에
 * 남는다.
 */

/** 대화 첫머리에 사용자 질문 예시처럼 보여주는 빠른 질문. */
const QUICK_QUESTIONS = [
  "오늘 전체 업무 현황을 요약해줘",
  "지금 막혀 있는 업무와 이유를 알려줘",
  "리서치팀의 최신 진행 상황을 브리핑해줘",
];

/** 채팅창의 안내 말풍선. 항상 첫 메시지로 떠 있다. */
const INITIAL_AI_MESSAGE: ChatMessage = {
  id: "ai-intro",
  role: "ai",
  text: "자연어로 질문하면 CEO Hermes가 업무를 만들고, 결과는 Kanban에서 추적됩니다.",
};

/** 워크플로 단계 상태 -> 이력 말풍선에 보여줄 한국어. */
const WORKFLOW_STATUS_LABEL: Record<string, string> = {
  queued: "대기 중",
  running: "진행 중",
  blocked: "막힘",
  failed: "실패",
  completed: "완료",
  archived: "보관됨",
};

type ChatMessage = {
  id: string;
  role: "user" | "ai";
  text: string;
  /** 이 메시지가 만든/가리키는 root task. 진행 중 폴링 결과를 여기에 붙인다. */
  taskId?: string | null;
  planning?: CeoQueryPlanning | null;
};

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

export function CeoControlRoomChat() {
  const [draft, setDraft] = useState("");
  // 이번 방문에서 사용자가 보낸 대화만 담는다 - 서버 이력은 아래 `historyMessages`가
  // 쿼리 캐시에서 따로 만든다. 계정 전환 시 이 배열만 비우면 되고, 이력 조회
  // 자체가 실패해도 여기 담긴 대화는 지워지지 않는다(캐시된 `data`는 refetch가
  // 실패해도 이전 값을 유지하는 TanStack Query의 기본 동작).
  const [sessionMessages, setSessionMessages] = useState<ChatMessage[]>([]);
  const [accountId, setAccountId] = useState<string>(() => readStoredAccountId());
  const [activeTaskId, setActiveTaskId] = useState<string | null>(null);

  // 계정별 이력 목록. 계정을 전환했다가 다시 돌아오면 캐시 히트로 즉시 뜨고,
  // 재조회가 실패해도 `data`는 마지막 성공값 그대로 남는다 - 화면이 비는 대신
  // `isError`만 별도로 받는다.
  const tasksQuery = useQuery({
    queryKey: ["ceo", "tasks", accountId],
    queryFn: () => listCeoTasks(accountId),
  });

  // 서버는 최신순으로 준다 - 채팅은 오래된 것부터 쌓여야 한다.
  const historyItems = useMemo(
    () => [...(tasksQuery.data?.items ?? [])].reverse(),
    [tasksQuery.data],
  );
  const terminalItems = useMemo(
    () => historyItems.filter((item) => TERMINAL_WORKFLOW_STATUSES.has(item.status)),
    [historyItems],
  );

  // 완료된 이력 항목의 최종 결과를 병렬로 가져온다 - 예전엔 `for` 루프 안에서
  // 하나씩 `await`해서 이력 N개면 N번 왕복을 직렬로 기다렸다. `staleTime:
  // Infinity`인 이유: 종료된 Task의 결과는 이후에 바뀌지 않으므로 재조회할
  // 이유가 없다 - 이 캐시 항목은 진행 중 Task의 15초 폴링(`resultQuery`, 아래)과
  // 쿼리 키(`["ceo", "result", taskId]`)를 공유해서, 방금 끝난 대화가 다음 이력
  // 조회에서 다시 네트워크를 타지 않는다.
  const historyResultQueries = useQueries({
    queries: terminalItems.map((item) => ({
      queryKey: ["ceo", "result", item.task_id],
      queryFn: () => ceoWorkflowResult(item.task_id),
      staleTime: Infinity,
    })),
  });

  const historyMessages = useMemo<ChatMessage[]>(() => {
    const out: ChatMessage[] = [];
    const resultByTaskId = new Map(
      terminalItems.map((item, index) => [item.task_id, historyResultQueries[index]?.data]),
    );
    for (const item of historyItems) {
      out.push({
        id: `u-${item.task_id}`,
        role: "user",
        text: item.query ?? "(질문 내용 없음)",
        taskId: item.task_id,
      });
      const summary = resultByTaskId.get(item.task_id)?.result?.summary;
      out.push({
        id: `a-${item.task_id}`,
        role: "ai",
        text: summary || `상태: ${WORKFLOW_STATUS_LABEL[item.status] ?? item.status}`,
        taskId: item.task_id,
      });
    }
    return out;
  }, [historyItems, terminalItems, historyResultQueries]);

  // 방금 보낸 대화가 `tasksQuery`의 다음 재조회(예: 창 포커스 복귀)로 이력에도
  // 잡히면 같은 Task가 두 번 보일 수 있다 - 세션에 이미 있는 task_id는 이력
  // 쪽에서 뺀다.
  const messages = useMemo<ChatMessage[]>(() => {
    const sessionTaskIds = new Set(
      sessionMessages.map((message) => message.taskId).filter((id): id is string => Boolean(id)),
    );
    const dedupedHistory = historyMessages.filter(
      (message) => !message.taskId || !sessionTaskIds.has(message.taskId),
    );
    return [INITIAL_AI_MESSAGE, ...dedupedHistory, ...sessionMessages];
  }, [historyMessages, sessionMessages]);

  const chatRef = useRef<HTMLDivElement>(null);
  const chatCount = messages.length;
  useEffect(() => {
    const el = chatRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [chatCount]);

  // 계정 전환 이벤트를 듣는다 — 같은 탭의 다른 컴포넌트가 바꾼 계정과 다른 탭
  // 양쪽 모두. `accountId`가 바뀌면 위 `tasksQuery`가 그 계정 쿼리 키로 다시
  // 구독되고, 진행 중이던 이번 세션 대화는 새 계정 것이 아니므로 비운다.
  // 이 setState들은 effect 본문이 아니라 이벤트 콜백 안에서만 실행되므로
  // 커밋 중 렌더 캐스케이드가 생기지 않는다.
  useEffect(
    () =>
      subscribeToAccountChange(() => {
        setAccountId(readStoredAccountId());
        setSessionMessages([]);
        setActiveTaskId(null);
      }),
    [],
  );

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

  // 최종 답변 — 15초 간격. Synthesis가 끝나 summary가 오면 멈춘다. 쿼리 키를
  // 이력 조회(`historyResultQueries`)와 공유하므로, 다음 계정 전환 때 이
  // 대화가 이력에 다시 나타나도 네트워크를 새로 타지 않는다.
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
    setSessionMessages((prev) => [...prev, { id: `u-${Date.now()}`, role: "user", text: value }]);
    try {
      const response = await sendMutation.mutateAsync(value);
      setSessionMessages((prev) => [
        ...prev,
        {
          id: `a-${response.task_id}-${Date.now()}`,
          role: "ai",
          text: response.answer,
          taskId: response.task_id,
          planning: response.planning ?? null,
        },
      ]);
      setActiveTaskId(response.task_id);
    } catch {
      // 실패 원인은 `sendMutation.error`로 이미 들고 있다 - 아래 에러 배너가 보여준다.
    }
  }

  const busy = sendMutation.isPending;
  const error = sendMutation.isError
    ? sendMutation.error instanceof Error
      ? sendMutation.error.message
      : String(sendMutation.error)
    : tasksQuery.isError
      ? tasksQuery.error instanceof Error
        ? tasksQuery.error.message
        : String(tasksQuery.error)
      : "";

  return (
    <section className="lg:col-span-1 bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden shadow-sm flex flex-col">
      <PanelBar icon="terminal" title="CEO Control Room">
        <span className="flex items-center gap-1.5 text-xs text-on-surface-variant">
          <span className="w-2 h-2 rounded-full bg-tertiary-fixed-dim" aria-hidden="true" />
          Hermes endpoint
        </span>
      </PanelBar>

      <div
        ref={chatRef}
        className="p-4 flex flex-col gap-3 overflow-y-auto min-h-[320px] max-h-[480px]"
        aria-live="polite"
        aria-label="CEO 질의 대화"
      >
        {messages.map((message) => (
          <div key={message.id} className="flex flex-col gap-2">
            <div
              className={`rounded-lg border p-3 max-w-[92%] ${
                message.role === "user"
                  ? "self-end bg-secondary-container border-secondary-container"
                  : "self-start bg-surface-container-low border-outline-variant"
              }`}
            >
              <div className="font-bold text-body-sm font-body-sm text-primary mb-1">
                {message.role === "user" ? "대표님" : "CEO Hermes"}
              </div>
              <p className="text-body-sm font-body-sm text-on-surface m-0 whitespace-pre-line">
                {message.text}
              </p>
              {message.planning ? (
                <div className="mt-2 flex flex-wrap gap-1.5">
                  {message.planning.selected_departments.map((dept) => (
                    <span
                      key={dept}
                      className="px-2 py-0.5 rounded-full border border-outline-variant bg-surface-container text-[11px] text-on-surface-variant"
                    >
                      {dept}
                    </span>
                  ))}
                  {message.planning.qa_required ? (
                    <span className="px-2 py-0.5 rounded-full border border-primary/30 bg-secondary-container text-[11px] text-primary">
                      QA 필요
                    </span>
                  ) : null}
                </div>
              ) : null}
              {message.taskId ? (
                <code className="block text-right text-[10px] text-outline mt-1">{message.taskId}</code>
              ) : null}
            </div>

            {/* 안내 말풍선 바로 아래 - 사용자가 물어볼 법한 질문 예시로 보여준다. */}
            {message.id === INITIAL_AI_MESSAGE.id ? (
              <div className="self-start max-w-[92%] flex flex-wrap gap-1.5">
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
            ) : null}

            {/* 진행 중인 대화의 AI 말풍선 아래에만 실시간 진행·최종 답변을 붙인다. */}
            {message.role === "ai" && message.taskId && message.taskId === activeTaskId ? (
              <>
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
          </div>
        ))}

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
