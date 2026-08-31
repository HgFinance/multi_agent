"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import type { UIEvent } from "react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  askCeo,
  approveStrategyDeployment,
  buildCeoProgress,
  ceoWorkflowResult,
  ceoWorkflowStatus,
  paperOrderWorkflowStatus,
  powerStrategyDeployment,
  removeStrategyDeployment,
  strategyResearchStatus,
  strategyDeploymentStatus,
  type CardOutcome,
  type CeoQueryPlanning,
  type StrategyDeploymentAccepted,
  type StrategyResearchAccepted,
  type StrategyResearchStatus,
} from "../lib/ceoClient";
import { usePortfolioSession } from "../lib/PortfolioSessionProvider";
import { DEFAULT_ACCOUNT } from "../lib/currentAccount";
import {
  authorizedBooksForFund,
  browserSessionStorage,
  clearRetryablePaperOrderAction,
  loadRetryablePaperOrderAction,
  persistRetryablePaperOrderAction,
  preparePaperOrderAction,
  selectedAuthorizedBook,
  type PaperOrderStorageScope,
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

/** CEO Control Room의 진행 폴링 간격. 종료 조건은 쿼리마다 따로 본다. */
const CHAT_POLL_MS = 5_000;

/** PAPER 주문만 예외 — 체결 반영이 늦게 보이면 안 되므로 2초를 유지한다. */
const ORDER_POLL_MS = 2_000;

/**
 * 자동 스크롤을 유지할 "바닥" 판정 여유(px).
 *
 * 폴링으로 카드가 늘어날 때마다 무조건 내리면 위로 올려 읽던 내용이 튕긴다.
 * 사용자가 바닥 근처에 있을 때만 따라 내려간다.
 */
const AUTOSCROLL_BOTTOM_SLACK_PX = 48;

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
  taskId: string | null;
  query: string;
  answer: string | null;
  planning: CeoQueryPlanning | null;
  orderRequestId: string | null;
  orderState: string | null;
  researchRequestId: string | null;
  deploymentId: string | null;
  deploymentStatus: StrategyDeploymentAccepted["status"] | null;
  deploymentBacktestSummary: Record<string, unknown> | null;
  deploymentRuntimeStatus: string | null;
  deploymentExecutionStatus: string | null;
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
  const activeResearchRequestId = submitted?.researchRequestId ?? null;
  const activeDeploymentId = submitted?.deploymentId ?? null;

  // 본부별 진행 — 모든 카드가 끝나면 폴링을 멈춘다.
  const statusQuery = useQuery({
    queryKey: ["ceo", "status", activeTaskId],
    queryFn: () => ceoWorkflowStatus(activeTaskId as string),
    enabled: Boolean(activeTaskId),
    refetchInterval: (query) => {
      const data = query.state.data;
      if (!data) return CHAT_POLL_MS;
      return buildCeoProgress(data, null).all_terminal ? false : CHAT_POLL_MS;
    },
  });

  // 최종 답변 — summary가 오면 더 이상 재조회하지 않는다.
  const resultQuery = useQuery({
    queryKey: ["ceo", "result", activeTaskId],
    queryFn: () => ceoWorkflowResult(activeTaskId as string),
    enabled: Boolean(activeTaskId),
    refetchInterval: (query) => (query.state.data?.result?.summary ? false : CHAT_POLL_MS),
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
      if (state === "UNKNOWN") return data?.directive ? CHAT_POLL_MS : false;
      return state && PAPER_ORDER_TERMINAL_STATES.has(state) ? false : ORDER_POLL_MS;
    },
  });

  const researchStatusQuery = useQuery<StrategyResearchStatus>({
    queryKey: ["autonomous-research", activeResearchRequestId],
    queryFn: () => strategyResearchStatus(activeResearchRequestId as string),
    enabled: Boolean(activeResearchRequestId),
    refetchInterval: (query) => {
      const data = query.state.data;
      return data?.status === "CANDIDATE" || data?.status === "COMPLETED" || data?.status === "BLOCKED" ? false : CHAT_POLL_MS;
    },
  });

  const deploymentStatusQuery = useQuery<StrategyDeploymentAccepted>({
    queryKey: ["autonomous-research-deployment", activeResearchRequestId, activeDeploymentId],
    queryFn: () => strategyDeploymentStatus(activeResearchRequestId as string, activeDeploymentId as string),
    enabled: Boolean(activeResearchRequestId && activeDeploymentId && ["ACTIVE", "PAUSED", "FAILED"].includes(submitted?.deploymentStatus ?? "")),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "REMOVED" || status === "FAILED" ? false : CHAT_POLL_MS;
    },
  });

  /**
   * 결과가 불확실한 재전송이 두 번째 주문이 되지 않게 하는 안전장치.
   *
   * `askCeo`는 `request_id`를 안 주면 호출마다 `crypto.randomUUID()`를 새로
   * 뽑고, 서버 중복 방지는 그 값 하나에만 걸려 있다
   * (`ceo.py` -> `client_request_id`, `UNIQUE (user_id, client_request_id)`).
   * 그래서 타임아웃처럼 결말을 모르는 상태에서 사용자가 같은 지시를 다시
   * 보내면 새 키가 발급돼 **주문이 두 번 들어갈 수 있었다**(2026-08-31).
   *
   * 전송 *전에* (fund, book, 지시문) 지문으로 키를 고정해 저장한다. 같은
   * 지문의 재전송은 같은 키를 쓰고, 서버 `admit()`이 같은 키+같은 주문을
   * 기존 요청으로 그대로 돌려주므로 두 번째 주문이 생기지 않는다. 결말을
   * 확인한 뒤(onSuccess)에만 키를 버려, 나중에 같은 주문을 **의도적으로** 한
   * 번 더 내는 것은 막지 않는다.
   *
   * 주문이 아닌 대화는 Book이 없어 지문을 만들 수 없다. 그때는 예전처럼
   * 서버가 키를 발급하게 두고 안전장치는 적용하지 않는다.
   */
  function orderScope(
    fundId?: string,
    bookId?: string,
  ): PaperOrderStorageScope | null {
    if (!fundId || !bookId) return null;
    return { accountId: DEFAULT_ACCOUNT.userId, fundId, bookId };
  }

  const sendMutation = useMutation({
    mutationFn: ({ text, bookId, fundId }: { text: string; bookId?: string; fundId?: string }) => {
      const scope = orderScope(fundId, bookId);
      const storage = scope ? browserSessionStorage() : null;
      if (!scope || !storage) return askCeo(text, undefined, bookId, fundId);

      const input = { fundId: scope.fundId, bookId: scope.bookId, query: text };
      let requestId: string;
      try {
        const action = preparePaperOrderAction(
          input,
          loadRetryablePaperOrderAction(storage, scope),
        );
        // 전송 전에 저장한다. 새로고침이 같은 주문에 두 번째 키를 뽑지 못한다.
        if (!persistRetryablePaperOrderAction(storage, scope, action)) {
          return askCeo(text, undefined, bookId, fundId);
        }
        requestId = action.submission.idempotencyKey;
      } catch {
        return askCeo(text, undefined, bookId, fundId);
      }
      return askCeo(text, requestId, bookId, fundId);
    },
    onSuccess: (_response, variables) => {
      const scope = orderScope(variables.fundId, variables.bookId);
      const storage = scope ? browserSessionStorage() : null;
      if (scope && storage) clearRetryablePaperOrderAction(storage, scope);
    },
    // onError에서는 지우지 않는다. 결말을 모르는 상태가 정확히 이 장치가
    // 필요한 순간이고, 다음 전송이 같은 키를 재사용해야 한다.
  });

  const approvalMutation = useMutation({
    mutationFn: ({
      requestId,
      deploymentId,
      overrideReviewRequired,
    }: {
      requestId: string;
      deploymentId: string;
      overrideReviewRequired: boolean;
    }) =>
      approveStrategyDeployment(requestId, deploymentId, {
        confirm: true,
        override_review_required: overrideReviewRequired,
        reason: overrideReviewRequired
          ? "웹 CEO Control Room의 최상위 사람 예외 승인"
          : "웹 CEO Control Room의 명시적 사람 승인",
      }),
  });

  const lifecycleMutation = useMutation({
    mutationFn: ({
      requestId,
      deploymentId,
      action,
    }: {
      requestId: string;
      deploymentId: string;
      action: "start" | "stop" | "remove";
    }) =>
      action === "remove"
        ? removeStrategyDeployment(requestId, deploymentId, {
            confirm: true,
            reason: "웹 CEO Control Room의 명시적 전략 제거",
          })
        : powerStrategyDeployment(requestId, deploymentId, {
            action,
            reason: `웹 CEO Control Room의 명시적 컨테이너 ${action === "start" ? "시작" : "중지"}`,
          }),
  });

  async function send(text: string) {
    const value = text.trim();
    if (!value || sendMutation.isPending) return;
    if (PAPER_ORDER_LANGUAGE.test(value) && !selectedBookId) {
      setLocalError(
        "매매 지시를 보내려면 거래 가능한 PAPER 계좌를 선택하세요. 요청은 아직 전송하지 않았습니다.",
      );
      return;
    }

    setDraft("");
    setLocalError("");
    approvalMutation.reset();
    lifecycleMutation.reset();
    // 서버의 최초 응답을 기다리지 않고 사용자 메시지를 먼저 보여준다.
    setSubmitted({
      taskId: null,
      query: value,
      answer: null,
      planning: null,
      orderRequestId: null,
      orderState: null,
      researchRequestId: null,
      deploymentId: null,
      deploymentStatus: null,
      deploymentBacktestSummary: null,
      deploymentRuntimeStatus: null,
      deploymentExecutionStatus: null,
    });

    try {
      const response = await sendMutation.mutateAsync({
        text: value,
        fundId: effectiveFundId ?? undefined,
        ...(selectedBookId ? { bookId: selectedBookId } : {}),
      });
      if ("deployment_id" in response) {
        const deployment = response as StrategyDeploymentAccepted;
        setSubmitted({
          taskId: null,
          query: value,
          answer: deployment.message,
          planning: null,
          orderRequestId: null,
          orderState: null,
          // Deployment responses carry the same research request ID. Keep it
          // so approval can target the exact request and the research status
          // card remains visible while the release gate is open.
          researchRequestId: deployment.request_id,
          deploymentId: deployment.deployment_id,
          deploymentStatus: deployment.status,
          deploymentBacktestSummary: deployment.backtest_summary,
          deploymentRuntimeStatus: deployment.runtime_status,
          deploymentExecutionStatus: deployment.execution_status,
        });
      } else if ("lab_id" in response) {
        const research = response as StrategyResearchAccepted;
        setSubmitted({
          taskId: null,
          query: value,
          answer: research.message,
          planning: null,
          orderRequestId: null,
          orderState: null,
          researchRequestId: research.request_id,
          deploymentId: null,
          deploymentStatus: null,
          deploymentBacktestSummary: null,
          deploymentRuntimeStatus: null,
          deploymentExecutionStatus: null,
        });
      } else {
        setSubmitted({
          taskId: response.task_id,
          query: value,
          answer: response.answer,
          planning: response.planning ?? null,
          orderRequestId: response.order_request_id ?? null,
          orderState: response.order_state ?? null,
          researchRequestId: null,
          deploymentId: null,
          deploymentStatus: null,
          deploymentBacktestSummary: null,
          deploymentRuntimeStatus: null,
          deploymentExecutionStatus: null,
        });
      }
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

  async function approveSubmittedDeployment() {
    const overrideReviewRequired = submitted?.deploymentStatus === "REVIEW_REQUIRED";
    if (
      !submitted?.deploymentId ||
      !submitted.researchRequestId ||
      !["AWAITING_APPROVAL", "REVIEW_REQUIRED"].includes(submitted.deploymentStatus ?? "") ||
      approvalMutation.isPending
    ) {
      return;
    }
    setLocalError("");
    try {
      const deployment = await approvalMutation.mutateAsync({
        requestId: submitted.researchRequestId,
        deploymentId: submitted.deploymentId,
        overrideReviewRequired,
      });
      setSubmitted((current) =>
        current
          ? {
              ...current,
              answer: deployment.message,
              deploymentStatus: deployment.status,
              deploymentBacktestSummary: deployment.backtest_summary,
              deploymentRuntimeStatus: deployment.runtime_status,
              deploymentExecutionStatus: deployment.execution_status,
            }
          : current,
      );
    } catch {
      // approvalMutation.error is rendered below without losing the request.
    }
  }

  async function changeDeploymentPower(action: "start" | "stop" | "remove") {
    if (
      !submitted?.deploymentId ||
      !submitted.researchRequestId ||
      lifecycleMutation.isPending
    ) {
      return;
    }
    if (action === "remove" && !window.confirm("이 PAPER 전략 컨테이너를 제거할까요? 연구 원본과 백테스트 증거는 보존됩니다.")) {
      return;
    }
    setLocalError("");
    try {
      const deployment = await lifecycleMutation.mutateAsync({
        requestId: submitted.researchRequestId,
        deploymentId: submitted.deploymentId,
        action,
      });
      setSubmitted((current) =>
        current
          ? {
              ...current,
              answer: deployment.message,
              deploymentStatus: deployment.status,
              deploymentBacktestSummary: deployment.backtest_summary,
              deploymentRuntimeStatus: deployment.runtime_status,
              deploymentExecutionStatus: deployment.execution_status,
            }
          : current,
      );
    } catch {
      // lifecycleMutation.error is rendered below without losing the request.
    }
  }

  const approvalError = approvalMutation.isError
    ? approvalMutation.error instanceof Error
      ? approvalMutation.error.message
      : String(approvalMutation.error)
    : "";
  const lifecycleError = lifecycleMutation.isError
    ? lifecycleMutation.error instanceof Error
      ? lifecycleMutation.error.message
      : String(lifecycleMutation.error)
    : "";
  const deploymentView = deploymentStatusQuery.data;
  const deploymentStatus = deploymentView?.status ?? submitted?.deploymentStatus;
  const deploymentSummary = deploymentView?.backtest_summary ?? submitted?.deploymentBacktestSummary;
  const deploymentRuntimeStatus = deploymentView?.runtime_status ?? submitted?.deploymentRuntimeStatus;
  const deploymentExecutionStatus = deploymentView?.execution_status ?? submitted?.deploymentExecutionStatus;

  /**
   * 새 대화·상태 갱신이 오면 결과 영역을 바닥으로 내린다.
   *
   * 답변과 본부별 진행·주문·연구·배포 카드는 폴링으로 뒤늦게 붙어서, 그대로
   * 두면 새로 온 내용이 320px 스크롤 영역 밖에 쌓인다. 다만 사용자가 위로
   * 올려 읽는 중이면 따라 내리지 않는다(바닥 근처일 때만 따라간다).
   * 폴링 카드는 텍스트만 바뀌기도 해서 의존성 배열 대신 MutationObserver로
   * 실제 DOM 변화를 본다.
   */
  const resultsRef = useRef<HTMLDivElement>(null);
  const pinnedToBottomRef = useRef(true);

  function handleResultsScroll(event: UIEvent<HTMLDivElement>) {
    const el = event.currentTarget;
    pinnedToBottomRef.current =
      el.scrollHeight - el.scrollTop - el.clientHeight <= AUTOSCROLL_BOTTOM_SLACK_PX;
  }

  useEffect(() => {
    const el = resultsRef.current;
    if (!el) return;
    const scrollToBottom = () => {
      if (!pinnedToBottomRef.current) return;
      el.scrollTop = el.scrollHeight;
    };
    scrollToBottom();
    if (typeof MutationObserver === "undefined") return;
    const observer = new MutationObserver(scrollToBottom);
    observer.observe(el, { childList: true, subtree: true, characterData: true });
    return () => observer.disconnect();
  }, []);

  // 새 질의를 보내면 이전에 위로 올려둔 상태와 관계없이 처음부터 따라간다.
  const submittedQuery = submitted?.query ?? "";
  useEffect(() => {
    if (!submittedQuery) return;
    pinnedToBottomRef.current = true;
    const el = resultsRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [activeTaskId, submittedQuery]);

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
        ref={resultsRef}
        onScroll={handleResultsScroll}
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
              <div className="font-bold text-body-sm font-body-sm text-primary mb-1">
                {submitted.deploymentId
                  ? "전략 배포 게이트"
                  : submitted.researchRequestId
                    ? "Hermes 자율 연구실"
                    : "CEO Hermes"}
              </div>
              <p className="text-body-sm font-body-sm text-on-surface m-0 whitespace-pre-line">
                {submitted.answer ?? "CEO Hermes가 답변을 준비하는 중입니다…"}
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
              {submitted.taskId ? (
                <code className="block text-right text-[10px] text-outline mt-1">{submitted.taskId}</code>
              ) : null}
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
                  {/* 요청 상태는 활성화 워크플로의 결말일 뿐이다. 조건주문은
                      규칙이 따로 실패할 수 있어 요청만 보면 성공으로 읽힌다. */}
                  {orderStatusQuery.data?.conditional_rules?.length
                    ? " (조건주문 접수 결과)"
                    : null}
                </p>
                {orderStatusQuery.data?.conditional_rules?.map((rule) => (
                  <p
                    key={rule.rule_id}
                    className={`mt-1 mb-0 text-[11px] ${
                      rule.state === "FAILED" || rule.state === "EXPIRED"
                        ? "text-error"
                        : "text-on-surface"
                    }`}
                  >
                    조건규칙 {rule.state}
                    {rule.status_message
                      ? ` — ${rule.status_message}`
                      : rule.last_error_code
                        ? ` — ${rule.last_error_code}`
                        : ""}
                  </p>
                ))}
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

            {activeResearchRequestId ? (
              <div
                className="self-start max-w-[92%] border border-primary/30 rounded p-3 bg-secondary-container/20"
                aria-live="polite"
              >
                <div className="text-xs font-bold text-primary">자율 전략 연구실</div>
                <p className="mt-1 mb-0 text-xs text-on-surface">
                  {researchStatusQuery.data?.status === "CANDIDATE"
                    ? "검증 게이트를 통과한 후보가 기록되었습니다. 별도 QA·Risk·사람 심사가 필요합니다."
                    : researchStatusQuery.data?.status === "COMPLETED"
                      ? "실험과 검증이 완료되었습니다. 최종 보고서를 확인하세요."
                    : researchStatusQuery.data?.status === "BLOCKED"
                      ? `연구가 일시 중단되었습니다: ${researchStatusQuery.data.error ?? "오류 원인 기록을 확인하세요."}`
                    : `Hermes가 연구 중입니다 · ${researchStatusQuery.data?.cycle ?? 0}회차 · 계획 ${researchStatusQuery.data?.plan_count ?? 0}개 · 결과 ${researchStatusQuery.data?.result_count ?? 0}개`}
                </p>
                {researchStatusQuery.data?.last_action ? (
                  <p className="mt-1 mb-0 text-[11px] text-on-surface-variant">
                    현재 단계: {researchStatusQuery.data.last_action}
                  </p>
                ) : null}
                {researchStatusQuery.isError ? (
                  <p className="mt-1 mb-0 text-[11px] text-error">연구실 상태를 다시 확인하고 있습니다.</p>
                ) : null}
                <code className="mt-1 block text-[10px] text-outline">{activeResearchRequestId}</code>
              </div>
            ) : null}

            {submitted.deploymentId ? (
              <div
                className="self-start max-w-[92%] border border-primary/30 rounded p-3 bg-secondary-container/20"
                aria-live="polite"
              >
                <div className="text-xs font-bold text-primary">전략 배포 요청</div>
                <p className="mt-1 mb-0 text-xs text-on-surface">
                  상태: {deploymentStatus ?? "REVIEW_REQUIRED"} · 실행: {deploymentRuntimeStatus ?? "NOT_STARTED"} · {deploymentExecutionStatus ?? "NOT_STARTED"}
                </p>
                {deploymentSummary ? (
                  <div className="mt-2 grid grid-cols-2 gap-x-3 gap-y-1 text-[11px] text-on-surface-variant">
                    <span>유니버스: {String(deploymentSummary.symbols ?? deploymentSummary.symbol ?? "미확인")}</span>
                    <span>기간: {String(deploymentSummary.period ?? "미확인")}</span>
                    <span>타임프레임: {String(deploymentSummary.timeframe ?? "미확인")}</span>
                    <span>거래: {String(deploymentSummary.trade_count ?? "미확인")}</span>
                    <span>수익률: {String(deploymentSummary.return_pct ?? "미확인")}</span>
                    <span>승률: {String(deploymentSummary.win_rate_pct ?? "미확인")}</span>
                    <span>MDD: {String(deploymentSummary.mdd_pct ?? "미확인")}</span>
                    <span>판정: {String(deploymentSummary.decision ?? "미확인")}</span>
                  </div>
                ) : null}
                {deploymentStatus === "AWAITING_APPROVAL" || deploymentStatus === "REVIEW_REQUIRED" ? (
                  <button
                    type="button"
                    className="mt-3 rounded border border-primary bg-primary px-3 py-1.5 text-xs font-bold text-on-primary disabled:opacity-50"
                    disabled={approvalMutation.isPending}
                    onClick={approveSubmittedDeployment}
                  >
                    {approvalMutation.isPending
                      ? "승인·PAPER 컨테이너 시작 중…"
                      : deploymentStatus === "REVIEW_REQUIRED"
                        ? "최상위 승인으로 예외 PAPER 배포"
                        : "백테스트 확인 후 PAPER 배포 승인"}
                  </button>
                ) : null}
                {deploymentStatus === "ACTIVE" || deploymentStatus === "PAUSED" || deploymentStatus === "FAILED" ? (
                  <div className="mt-3 flex flex-wrap gap-2">
                    {deploymentStatus === "PAUSED" ? (
                      <button
                        type="button"
                        className="rounded border border-primary bg-primary px-3 py-1.5 text-xs font-bold text-on-primary disabled:opacity-50"
                        disabled={lifecycleMutation.isPending}
                        onClick={() => changeDeploymentPower("start")}
                      >
                        PAPER 컨테이너 시작
                      </button>
                    ) : deploymentStatus === "ACTIVE" ? (
                      <button
                        type="button"
                        className="rounded border border-outline-variant bg-surface px-3 py-1.5 text-xs font-bold text-on-surface disabled:opacity-50"
                        disabled={lifecycleMutation.isPending}
                        onClick={() => changeDeploymentPower("stop")}
                      >
                        PAPER 컨테이너 중지
                      </button>
                    ) : null}
                    <button
                      type="button"
                      className="rounded border border-error/50 bg-error-container px-3 py-1.5 text-xs font-bold text-error disabled:opacity-50"
                      disabled={lifecycleMutation.isPending}
                      onClick={() => changeDeploymentPower("remove")}
                    >
                      전략 배포 제거
                    </button>
                  </div>
                ) : null}
                {deploymentStatusQuery.isError ? (
                  <p className="mt-1 mb-0 text-[11px] text-error">컨테이너 실시간 상태를 다시 확인하고 있습니다.</p>
                ) : null}
                <code className="mt-1 block text-[10px] text-outline">{submitted.deploymentId}</code>
              </div>
            ) : null}
          </>
        ) : null}

        {error || approvalError || lifecycleError ? (
          <p role="alert" className="self-start max-w-[92%] text-xs text-error border border-error-container bg-error-container rounded p-2">
            ⚠️ {error || approvalError || lifecycleError}
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
