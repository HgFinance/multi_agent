"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useMemo, useState } from "react";
import {
  askCeo,
  buildCeoProgress,
  ceoWorkflowResult,
  ceoWorkflowStatus,
  paperOrderWorkflowStatus,
  type CardOutcome,
  type CeoQueryPlanning,
} from "../lib/ceoClient";
import { usePortfolioSession } from "../lib/PortfolioSessionProvider";
import { DEFAULT_ACCOUNT } from "../lib/currentAccount";
import {
  authorizedBooksForFund,
  selectedAuthorizedBook,
} from "../lib/paperOrderClient";
import { PanelBar } from "./PanelBar";

/**
 * "CEO Control Room" 패널 - **단발 질의 입력란**.
 *
 * 과거 대화는 Discord가 보관하고, 이 패널은 방금 보낸 요청 하나만 보여준다.
 * 종료된 이력마다 Kanban 결과를 다시 읽던 N+1 구조를 되살리지 않으면서도,
 * 현재 요청의 본부별 진행과 PAPER 주문 상태는 끝까지 추적한다.
 *
 * 사용자 주문은 일반 CEO 질의와 같은 입력창을 사용한다. 화면은 인증된 현재
 * Fund에 속한 Book만 전달하며, 실제 권한과 PAPER 전용 제약은 BFF와 Trading이
 * 다시 검증한다. LIVE 주문 경로는 제공하지 않는다.
 */

/** 입력란 아래에 보여주는 빠른 질문. 누르면 그대로 전송한다. */
const PAPER_ORDER_LANGUAGE = /(?:매수|매도|주문|사줘|팔아줘|지정가|시장가)/;

const QUICK_QUESTIONS = [
  "오늘 전체 업무 현황을 요약해줘",
  "지금 막혀 있는 업무와 이유를 알려줘",
  "리서치팀의 최신 진행 상황을 브리핑해줘",
];

const PAPER_ORDER_TERMINAL_STATES = new Set([
  "CLARIFICATION_REQUIRED",
  "NOT_ORDER",
  "REJECTED",
  "COMPLETED",
  "FAILED",
]);

/** 카드 결말별 표시. 보드의 status가 아니라 "답이 됐는가" 기준이다. */
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
  orderRequestId: string | null;
  orderState: string | null;
};

export function CeoControlRoomChat() {
  const portfolio = usePortfolioSession();
  const scopeKey = `${DEFAULT_ACCOUNT.userId}:${portfolio.activeFundId ?? "no-fund"}`;

  // 계정이나 Fund가 바뀌면 이전 사용자의 단발 결과와 주문 ID를 모두 폐기한다.
  return <CeoControlRoomChatSession key={scopeKey} />;
}

function CeoControlRoomChatSession() {
  const portfolio = usePortfolioSession();
  const effectiveFundId = useMemo(() => {
    const activeFund = portfolio.profile?.funds.find(
      (fund) => fund.fundId === portfolio.activeFundId,
    );
    if (activeFund?.books.length) return activeFund.fundId;
    const tradeableFunds = portfolio.profile?.funds.filter((fund) => fund.books.length > 0) ?? [];
    return tradeableFunds.length === 1 ? tradeableFunds[0].fundId : portfolio.activeFundId;
  }, [portfolio.activeFundId, portfolio.profile]);
  const usingFallbackTradingFund = Boolean(
    effectiveFundId && effectiveFundId !== portfolio.activeFundId,
  );
  const authorizedBooks = useMemo(
    () => authorizedBooksForFund(portfolio.profile, effectiveFundId),
    [effectiveFundId, portfolio.profile],
  );
  const [requestedBookId, setRequestedBookId] = useState("");
  const selectedBook =
    authorizedBooks.length === 1
      ? authorizedBooks[0]
      : selectedAuthorizedBook(authorizedBooks, requestedBookId);
  const selectedBookId = selectedBook?.bookId ?? "";
  const [draft, setDraft] = useState("");
  const [submitted, setSubmitted] = useState<SubmittedRequest | null>(null);
  const [localError, setLocalError] = useState("");

  const activeTaskId = submitted?.taskId ?? null;
  const activeOrderRequestId = submitted?.orderRequestId ?? null;

  // 본부별 진행 — 10초 간격. 모든 카드가 끝나면 폴링을 멈춘다.
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

  // 최종 답변 — summary가 오면 더 이상 재조회하지 않는다.
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

  const orderStatusQuery = useQuery({
    queryKey: ["ceo", "paper-order", activeOrderRequestId],
    queryFn: () => paperOrderWorkflowStatus(activeOrderRequestId as string),
    enabled: Boolean(activeOrderRequestId),
    refetchInterval: (query) => {
      const data = query.state.data;
      const state = data?.state;
      if (state === "UNKNOWN") return data?.directive ? 10_000 : false;
      return state && PAPER_ORDER_TERMINAL_STATES.has(state) ? false : 2_000;
    },
  });

  const sendMutation = useMutation({
    mutationFn: ({ text, bookId, fundId }: { text: string; bookId?: string; fundId?: string }) =>
      askCeo(text, undefined, bookId, fundId),
  });

  async function send(text: string) {
    const value = text.trim();
    if (!value || sendMutation.isPending) return;
    setDraft("");
    setLocalError("");
    if (PAPER_ORDER_LANGUAGE.test(value) && !selectedBookId) {
      setLocalError(
        "매매 지시를 보내려면 거래 가능한 PAPER 계좌를 선택하세요. 요청은 아직 전송하지 않았습니다.",
      );
      return;
    }
    try {
      const response = await sendMutation.mutateAsync({
        text: value,
        fundId: effectiveFundId ?? undefined,
        ...(selectedBookId ? { bookId: selectedBookId } : {}),
      });
      setSubmitted({
        taskId: response.task_id,
        query: value,
        answer: response.answer,
        planning: response.planning ?? null,
        orderRequestId: response.order_request_id ?? null,
        orderState: response.order_state ?? null,
      });
    } catch {
      // 실패 원인은 mutation.error로 보존되고 아래 에러 배너가 보여준다.
    }
  }

  const busy = sendMutation.isPending;
  const error = localError || (sendMutation.isError
    ? sendMutation.error instanceof Error
      ? sendMutation.error.message
      : String(sendMutation.error)
    : "");

  return (
    <section className="lg:col-span-1 bg-surface-container-lowest border border-outline-variant rounded-lg overflow-hidden shadow-sm flex flex-col">
      <PanelBar icon="terminal" title="CEO Control Room" />

      <div className="border-b border-outline-variant bg-surface-container-low px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <p className="m-0 text-[11px] text-on-surface-variant">
            궁금한 점을 묻거나 매매를 지시하면 대표가 확인해 처리합니다. 입력창 하단의 예시 질문을 클릭해보세요.
          </p>

          {authorizedBooks.length > 1 ? (
            <label className="min-w-[190px] text-[11px] font-bold text-on-surface-variant">
              매매 지시 계좌
              <select
                value={selectedBookId}
                onChange={(event) => setRequestedBookId(event.target.value)}
                className="mt-1 block w-full rounded border border-outline-variant bg-surface px-2 py-1.5 text-xs font-normal text-on-surface focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
                aria-describedby="ceo-paper-book-help"
              >
                <option value="">계좌를 선택하세요</option>
                {authorizedBooks.map((book) => (
                  <option key={book.bookId} value={book.bookId}>
                    {book.name}
                  </option>
                ))}
              </select>
            </label>
          ) : null}
        </div>

        {usingFallbackTradingFund ? (
          <p id="ceo-paper-book-help" role="status" className="mt-2 mb-0 text-[11px] text-on-surface-variant">
            현재 선택된 Fund에 거래 계좌가 없어 연결된 유일한 PAPER 계좌로 매매 지시를 보냅니다.
          </p>
        ) : null}

        {authorizedBooks.length === 0 ? (
          <p id="ceo-paper-book-help" role="status" className="mt-2 mb-0 text-[11px] text-on-surface-variant">
            이 펀드에 연결된 계좌가 없어 매매 지시는 처리할 수 없습니다. 질문과 안내는 계속 사용할 수 있습니다.
          </p>
        ) : authorizedBooks.length > 1 && !selectedBook ? (
          <p id="ceo-paper-book-help" className="mt-2 mb-0 text-[11px] text-on-surface-variant">
            매매 지시를 보낼 때만 계좌를 선택하세요. 질문은 선택 없이 사용할 수 있습니다.
          </p>
        ) : null}
      </div>

      {/* 최신 UI의 단발 결과 구조를 유지한다. 내용이 많으면 이 영역 안에서만 스크롤한다. */}
      <div
        className="p-4 flex flex-col gap-3 overflow-y-auto h-[320px] min-h-[320px] max-h-[320px]"
        aria-live="polite"
        aria-label="CEO 질의 결과"
      >
        {!submitted && !error ? (
          <div className="m-auto text-center px-2">
            <p className="text-body-sm font-body-sm text-on-surface-variant m-0">
              자연어로 질문하거나 매매를 지시하면 CEO Hermes가 업무를 만들고 Kanban에서 추적합니다.
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
                  {!progress.answer_grounded ? <span className="text-[10px] text-error">⚠️ 근거 미확인</span> : null}
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
                {statusQuery.isError || resultQuery.isError ? (
                  <p className="text-xs text-error mt-1 m-0">
                    ⚠️ BFF에서 진행 상황을 가져오지 못했습니다. 재시도 중입니다.
                  </p>
                ) : null}
              </div>
            ) : null}

            {activeOrderRequestId ? (
              <div
                className="self-start max-w-[92%] border border-primary/30 rounded p-3 bg-secondary-container/20"
                aria-live="polite"
              >
                <div className="text-xs font-bold text-primary">PAPER 주문 상태</div>
                <p className="mt-1 mb-0 text-xs text-on-surface">
                  {orderStatusQuery.data?.state ?? submitted.orderState ?? "INTERPRETING"}
                </p>
                {orderStatusQuery.data?.clarification_code ? (
                  <p className="mt-1 mb-0 text-[11px] text-error">
                    확인 필요: {orderStatusQuery.data.clarification_code}
                  </p>
                ) : null}
                {orderStatusQuery.data?.error_code ? (
                  <p className="mt-1 mb-0 text-[11px] text-error">{orderStatusQuery.data.error_code}</p>
                ) : null}
                {orderStatusQuery.isError ? (
                  <p className="mt-1 mb-0 text-[11px] text-error">주문 상태를 다시 확인하고 있습니다.</p>
                ) : null}
                <code className="mt-1 block text-[10px] text-outline">{activeOrderRequestId}</code>
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
            placeholder="예: 오늘 리스크를 브리핑해줘 / 삼성전자 10주 시장가 매수"
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
          <span className="text-xs text-outline">{draft.length}/2000</span>
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
