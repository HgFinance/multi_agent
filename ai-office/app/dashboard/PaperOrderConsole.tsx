"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import {
  browserSessionStorage,
  clearPendingPaperDirective,
  clearRetryablePaperOrderAction,
  getPaperDirective,
  initialPaperBookId,
  loadPendingPaperDirective,
  loadRetryablePaperOrderAction,
  paperOrderActionFingerprint,
  paperDirectiveIsComplete,
  paperDirectivePollInterval,
  persistPendingPaperDirective,
  persistRetryablePaperOrderAction,
  preparePaperOrderAction,
  selectedAuthorizedBook,
  shouldPollPaperDirective,
  submitPaperOrderSubmission,
  type PaperDirective,
  type PaperOrderStorageScope,
  type RetryablePaperOrderAction,
} from "../lib/paperOrderClient";
import type { AuthorizedBook } from "../lib/currentUserContract";

const STATE_LABEL: Record<PaperDirective["state"], string> = {
  RECEIVED: "접수됨",
  RUNNING: "처리 중",
  IN_PROGRESS: "진행 중",
  PARTIAL: "부분 처리",
  COMPLETED: "완료",
  FAILED: "실패",
  UNKNOWN: "상태 확인 불가",
};

const ACTION_LABEL: Record<PaperDirective["action"], string> = {
  PLACE_ORDER: "개별 주문",
  PLACE_BASKET: "복수 종목 바스켓 주문",
  SELL_ALL: "보유 종목 전량 매도",
  CANCEL_ALL: "미체결 주문 전체 취소",
};

export function PaperOrderConsole({
  accountId,
  fundId,
  books,
}: {
  accountId: string;
  fundId: string | null;
  books: readonly AuthorizedBook[];
}) {
  const [draft, setDraft] = useState("");
  const [requestedBookId, setRequestedBookId] = useState("");
  const [submitted, setSubmitted] = useState<PaperDirective | null>(null);
  const [retryable, setRetryable] = useState<RetryablePaperOrderAction | null>(null);
  const selectedBookId =
    selectedAuthorizedBook(books, requestedBookId)?.bookId ?? initialPaperBookId(books);

  useEffect(() => {
    const storage = browserSessionStorage();
    const recovery = window.setTimeout(() => {
      if (!storage || !fundId || !selectedBookId) {
        setSubmitted(null);
        setRetryable(null);
        setDraft("");
        return;
      }
      const scope = { accountId, fundId, bookId: selectedBookId };
      const recoveredRetry = loadRetryablePaperOrderAction(storage, scope);
      setSubmitted(loadPendingPaperDirective(storage, scope));
      setRetryable(recoveredRetry);
      setDraft(recoveredRetry?.input.query ?? "");
    }, 0);
    return () => window.clearTimeout(recovery);
  }, [accountId, fundId, selectedBookId]);

  const submitMutation = useMutation({
    mutationFn: (action: RetryablePaperOrderAction) =>
      submitPaperOrderSubmission(action.submission),
    retry: false,
    onMutate: (action) => {
      setSubmitted(null);
      const storage = browserSessionStorage();
      if (storage) {
        persistRetryablePaperOrderAction(storage, storageScope(accountId, action.input), action);
      }
    },
    onSuccess: (directive, action) => {
      const storage = browserSessionStorage();
      if (storage) {
        const scope = storageScope(accountId, action.input);
        clearRetryablePaperOrderAction(storage, scope);
        persistPendingPaperDirective(storage, scope, directive);
      }
      setSubmitted(directive);
      setRetryable(null);
      setDraft("");
    },
  });

  const statusQuery = useQuery({
    queryKey: [
      "paper-user-directive",
      submitted?.directive_id,
      submitted?.fund_id,
      submitted?.book_id,
    ],
    queryFn: () =>
      getPaperDirective({
        directiveId: submitted!.directive_id,
        fundId: submitted!.fund_id,
        bookId: submitted!.book_id,
      }),
    enabled: Boolean(submitted),
    refetchInterval: (query) =>
      paperDirectivePollInterval(query.state.data ?? submitted ?? undefined),
  });

  useEffect(() => {
    const latest = statusQuery.data;
    const storage = browserSessionStorage();
    if (!latest || !storage) return;
    const scope = storageScope(accountId, {
      fundId: latest.fund_id,
      bookId: latest.book_id,
    });
    if (shouldPollPaperDirective(latest)) {
      persistPendingPaperDirective(storage, scope, latest);
    } else {
      clearPendingPaperDirective(storage, scope);
    }
  }, [accountId, statusQuery.data]);

  const directive = statusQuery.data ?? submitted;
  const selectedBook = selectedAuthorizedBook(books, selectedBookId);
  const currentFingerprint =
    fundId && selectedBook && draft.trim()
      ? paperOrderActionFingerprint({ fundId, bookId: selectedBook.bookId, query: draft })
      : null;
  const retriesSameAction = Boolean(
    currentFingerprint && retryable?.fingerprint === currentFingerprint,
  );
  const canSubmit = Boolean(
    fundId && selectedBook && draft.trim() && !submitMutation.isPending,
  );
  const error = submitMutation.error ?? statusQuery.error;

  function submit() {
    if (!canSubmit || !selectedBook || !fundId) return;
    const query = draft.trim();
    // A distinct user action gets a new key. If the same action had an
    // ambiguous transport failure, an explicit retry reuses its prepared
    // request and key; React Query itself never auto-retries this mutation.
    const prepared = preparePaperOrderAction(
      { fundId, bookId: selectedBook.bookId, query },
      retryable,
    );
    setRetryable(prepared);
    submitMutation.mutate(prepared);
  }

  return (
    <div className="flex min-h-[420px] flex-col">
      <div className="border-b border-outline-variant bg-error-container/30 px-4 py-3">
        <p className="m-0 text-sm font-bold text-error">PAPER 주문 전용 · LIVE 주문 아님</p>
        <p className="m-0 mt-1 text-xs text-on-surface-variant">
          이 모드는 사용자가 직접 선택했을 때만 열립니다. 입력은 자문 채팅이나 LLM 의도 분류를 거치지 않고,
          인증된 BFF의 결정론적 주문 파서로 전달됩니다.
        </p>
      </div>

      <div className="flex flex-col gap-3 p-4">
        <label className="text-xs font-bold text-on-surface-variant">
          주문 대상 PAPER Book
          <select
            value={selectedBookId}
            onChange={(event) => setRequestedBookId(event.target.value)}
            className="mt-1 w-full rounded border border-outline-variant bg-surface p-2 text-sm font-normal text-on-surface"
          >
            {books.length !== 1 ? <option value="">Book을 직접 선택하세요</option> : null}
            {books.map((book) => (
              <option key={book.bookId} value={book.bookId}>
                {book.name} ({book.bookId})
              </option>
            ))}
          </select>
        </label>

        {books.length === 0 ? (
          <p role="alert" className="m-0 rounded border border-error/40 bg-error-container p-2 text-xs text-on-error-container">
            서버가 이 펀드에 대해 거래 가능한 ACTIVE Book을 부여하지 않았습니다. PAPER 주문을 제출할 수 없습니다.
          </p>
        ) : null}
        {books.length > 1 && !selectedBook ? (
          <p className="m-0 text-xs text-error">여러 Book 중 주문 대상을 직접 선택해야 합니다.</p>
        ) : null}

        <label className="text-xs font-bold text-on-surface-variant">
          사용자 PAPER 주문 명령
          <textarea
            value={draft}
            maxLength={500}
            rows={3}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                event.preventDefault();
                submit();
              }
            }}
            placeholder="예: 삼성전자 2주 시장가 매수 / 삼성전자, SK하이닉스 100만원씩 매수해 / 보유계좌 종목 전량 매도 / 미체결 주문 전부 취소"
            className="mt-1 w-full resize-none rounded border border-outline-variant bg-surface p-3 text-sm font-normal text-on-surface focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary"
          />
        </label>

        <div className="flex items-center justify-between gap-3">
          <span className="text-xs text-outline">
            {draft.length}/500 · 전송이 불명확했던 동일 요청은 같은 키로만 재시도
          </span>
          <button
            type="button"
            onClick={submit}
            disabled={!canSubmit}
            className="shrink-0 rounded border border-error/50 bg-error-container px-4 py-2 text-sm font-bold text-on-error-container transition-colors hover:bg-error-container/80 disabled:opacity-40"
          >
            {submitMutation.isPending
              ? "PAPER 접수 중…"
              : retriesSameAction
                ? "같은 PAPER 요청 재시도"
                : "PAPER 주문 제출"}
          </button>
        </div>

        {error ? (
          <p role="alert" className="m-0 rounded border border-error/40 bg-error-container p-2 text-xs text-on-error-container">
            요청 또는 상태 조회 오류: {error instanceof Error ? error.message : String(error)}
          </p>
        ) : null}

        {directive ? <DirectiveResult directive={directive} polling={statusQuery.isFetching} /> : null}
      </div>
    </div>
  );
}

function DirectiveResult({ directive, polling }: { directive: PaperDirective; polling: boolean }) {
  const complete = paperDirectiveIsComplete(directive);
  return (
    <section className="rounded border border-outline-variant bg-surface-container-low p-3" aria-live="polite">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 className="m-0 text-sm font-bold text-primary">PAPER directive 상태</h3>
        <span
          className={`rounded-full border px-2 py-0.5 text-xs font-bold ${
            complete
              ? "border-tertiary-fixed-dim bg-tertiary-fixed/30 text-on-tertiary-fixed-variant"
              : "border-outline-variant bg-surface-container text-on-surface-variant"
          }`}
        >
          {STATE_LABEL[directive.state]}{polling ? " · 확인 중" : ""}
        </span>
      </div>

      <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
        <dt className="text-on-surface-variant">directive_id</dt>
        <dd className="m-0 break-all font-data-mono text-on-surface">{directive.directive_id}</dd>
        <dt className="text-on-surface-variant">action</dt>
        <dd className="m-0 text-on-surface">{ACTION_LABEL[directive.action]} ({directive.action})</dd>
        <dt className="text-on-surface-variant">state</dt>
        <dd className="m-0 font-bold text-on-surface">{directive.state}</dd>
        <dt className="text-on-surface-variant">book_id</dt>
        <dd className="m-0 break-all font-data-mono text-on-surface">{directive.book_id}</dd>
      </dl>

      {!complete ? (
        <p className="mb-0 mt-3 text-xs font-bold text-error">
          현재 상태는 COMPLETED가 아닙니다. 주문이 완료됐다고 간주하지 마세요.
        </p>
      ) : (
        <p className="mb-0 mt-3 text-xs font-bold text-on-tertiary-fixed-variant">
          서버가 이 PAPER directive를 COMPLETED로 확인했습니다.
        </p>
      )}
      {directive.error_code || directive.error_message ? (
        <p className="mb-0 mt-2 text-xs text-error">
          {directive.error_code ?? "PAPER_ERROR"}: {directive.error_message ?? "상세 사유 없음"}
        </p>
      ) : null}
      {directive.state === "UNKNOWN" ? (
        <p role="alert" className="mb-0 mt-2 rounded border border-error/40 bg-error-container p-2 text-xs font-bold text-on-error-container">
          서버 상태가 UNKNOWN입니다. 자동 재조회는 계속되지만 완료로 간주하지 마세요. 장시간 지속되면
          운영 화면과 거래 원장을 수동 대사해 실제 체결·취소 상태를 확인하세요.
        </p>
      ) : null}

      <h4 className="mb-1 mt-3 text-xs font-bold text-on-surface-variant">처리 legs</h4>
      {directive.legs.length === 0 ? (
        <p className="m-0 text-xs text-outline">아직 생성되거나 반환된 leg가 없습니다.</p>
      ) : (
        <ul className="m-0 flex list-none flex-col gap-2 p-0">
          {directive.legs.map((leg) => (
            <li key={leg.leg_id} className="rounded border border-outline-variant bg-surface p-2 text-xs text-on-surface">
              <div className="flex flex-wrap justify-between gap-2">
                <span className="font-bold">
                  #{leg.leg_index + 1} {leg.symbol ?? "전체 주문"} {leg.side ?? ""}
                </span>
                <span>{leg.state}</span>
              </div>
              <p className="m-0 mt-1 text-on-surface-variant">
                요청 {leg.requested_quantity ?? "-"} · 체결 {leg.filled_quantity} · {leg.order_type ?? "취소 작업"}
                {leg.reduce_only ? " · reduce-only" : ""}
              </p>
              {leg.error_code || leg.error_message ? (
                <p className="m-0 mt-1 text-error">{leg.error_code ?? "LEG_ERROR"}: {leg.error_message ?? "상세 사유 없음"}</p>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function storageScope(
  accountId: string,
  input: { fundId: string; bookId: string },
): PaperOrderStorageScope {
  return { accountId, fundId: input.fundId, bookId: input.bookId };
}
