/**
 * Fixed-demo browser client for explicit user-directed PAPER orders.
 *
 * This is deliberately separate from `ceoClient`: advisory Hermes chat never
 * infers an order, and only the PAPER-order UI calls this module. The BFF owns
 * deterministic parsing, governed book access and the private Trading service proof.
 */

import { bffFetch, type BffRequestInit } from "./bffClient";
import type { AuthorizedBook, CurrentUserProfile } from "./currentUserContract";

export const PAPER_ORDER_PATH = "/trading/agent/order";

export const PAPER_DIRECTIVE_STATES = [
  "RECEIVED",
  "RUNNING",
  "IN_PROGRESS",
  "PARTIAL",
  "COMPLETED",
  "FAILED",
  "UNKNOWN",
] as const;

export type PaperDirectiveState = (typeof PAPER_DIRECTIVE_STATES)[number];
export type PaperDirectiveAction =
  | "PLACE_ORDER"
  | "PLACE_BASKET"
  | "SELL_ALL"
  | "CANCEL_ALL";

export interface PaperDirectiveLeg {
  leg_id: string;
  leg_index: number;
  instrument_id: string | null;
  symbol: string | null;
  side: "BUY" | "SELL" | null;
  order_type: "MARKET" | "LIMIT" | null;
  requested_quantity: string | null;
  limit_price: string | null;
  filled_quantity: string;
  target_filled_quantity: string;
  state: string;
  reduce_only: boolean;
  linked_order_id: string | null;
  client_order_id: string | null;
  broker_order_id: string | null;
  broker_event_id: string | null;
  expires_at: string | null;
  error_code: string | null;
  error_message: string | null;
}

export interface PaperDirective {
  directive_id: string;
  state: PaperDirectiveState;
  action: PaperDirectiveAction;
  priority: 1000 | 2000;
  priority_class: "USER_DIRECTIVE_HIGHEST";
  mode: "PAPER";
  fund_id: string;
  book_id: string;
  idempotency_key: string;
  instruction_ref: string;
  payload_sha256: string;
  created_at: string;
  updated_at: string;
  completed_at: string | null;
  error_code: string | null;
  error_message: string | null;
  legs: PaperDirectiveLeg[];
}

export interface PaperOrderSubmission {
  path: typeof PAPER_ORDER_PATH;
  idempotencyKey: string;
  init: BffRequestInit;
}

export interface PaperOrderActionInput {
  fundId: string;
  bookId: string;
  query: string;
}

export interface RetryablePaperOrderAction {
  fingerprint: string;
  input: PaperOrderActionInput;
  submission: PaperOrderSubmission;
}

export interface PaperOrderStorageScope {
  accountId: string;
  fundId: string;
  bookId: string;
}

export interface PaperOrderStorage {
  getItem(key: string): string | null;
  setItem(key: string, value: string): void;
  removeItem(key: string): void;
}

const PAPER_ORDER_RETRY_SCHEMA = "paper-order-retry.v1";
const PAPER_ORDER_PENDING_SCHEMA = "paper-order-pending.v1";
// Keep persisted retry identities inside the BFF's 128-character contract.
const PAPER_ORDER_IDEMPOTENCY_KEY = /^paper-order:[0-9A-Za-z][0-9A-Za-z._:-]{7,115}$/;
const UNKNOWN_FAST_POLL_AGE_MS = 30_000;
const UNKNOWN_MEDIUM_POLL_AGE_MS = 5 * 60_000;

function record(value: unknown): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("paper_order_invalid_response");
  }
  return value as Record<string, unknown>;
}

function nullableString(value: unknown): string | null {
  if (value === null) return null;
  if (typeof value !== "string") throw new Error("paper_order_invalid_response");
  return value;
}

function parseLeg(value: unknown): PaperDirectiveLeg {
  const leg = record(value);
  if (
    typeof leg.leg_id !== "string" ||
    !Number.isInteger(leg.leg_index) ||
    typeof leg.filled_quantity !== "string" ||
    typeof leg.target_filled_quantity !== "string" ||
    typeof leg.state !== "string" ||
    typeof leg.reduce_only !== "boolean"
  ) {
    throw new Error("paper_order_invalid_response");
  }
  const side = nullableString(leg.side);
  const orderType = nullableString(leg.order_type);
  if (side !== null && side !== "BUY" && side !== "SELL") {
    throw new Error("paper_order_invalid_response");
  }
  if (orderType !== null && orderType !== "MARKET" && orderType !== "LIMIT") {
    throw new Error("paper_order_invalid_response");
  }
  return {
    leg_id: leg.leg_id,
    leg_index: leg.leg_index as number,
    instrument_id: nullableString(leg.instrument_id),
    symbol: nullableString(leg.symbol),
    side,
    order_type: orderType,
    requested_quantity: nullableString(leg.requested_quantity),
    limit_price: nullableString(leg.limit_price),
    filled_quantity: leg.filled_quantity,
    target_filled_quantity: leg.target_filled_quantity,
    state: leg.state,
    reduce_only: leg.reduce_only,
    linked_order_id: nullableString(leg.linked_order_id),
    client_order_id: nullableString(leg.client_order_id),
    broker_order_id: nullableString(leg.broker_order_id),
    broker_event_id: nullableString(leg.broker_event_id),
    expires_at: nullableString(leg.expires_at),
    error_code: nullableString(leg.error_code),
    error_message: nullableString(leg.error_message),
  };
}

export function parsePaperDirective(value: unknown): PaperDirective {
  const directive = record(value);
  const state = directive.state;
  const action = directive.action;
  if (
    typeof directive.directive_id !== "string" ||
    !PAPER_DIRECTIVE_STATES.includes(state as PaperDirectiveState) ||
    !["PLACE_ORDER", "PLACE_BASKET", "SELL_ALL", "CANCEL_ALL"].includes(
      String(action),
    ) ||
    (directive.priority !== 1000 && directive.priority !== 2000) ||
    directive.priority_class !== "USER_DIRECTIVE_HIGHEST" ||
    directive.mode !== "PAPER" ||
    typeof directive.fund_id !== "string" ||
    typeof directive.book_id !== "string" ||
    typeof directive.idempotency_key !== "string" ||
    typeof directive.instruction_ref !== "string" ||
    typeof directive.payload_sha256 !== "string" ||
    typeof directive.created_at !== "string" ||
    typeof directive.updated_at !== "string" ||
    !Array.isArray(directive.legs)
  ) {
    throw new Error("paper_order_invalid_response");
  }
  return {
    directive_id: directive.directive_id,
    state: state as PaperDirectiveState,
    action: action as PaperDirectiveAction,
    priority: directive.priority,
    priority_class: "USER_DIRECTIVE_HIGHEST",
    mode: "PAPER",
    fund_id: directive.fund_id,
    book_id: directive.book_id,
    idempotency_key: directive.idempotency_key,
    instruction_ref: directive.instruction_ref,
    payload_sha256: directive.payload_sha256,
    created_at: directive.created_at,
    updated_at: directive.updated_at,
    completed_at: nullableString(directive.completed_at),
    error_code: nullableString(directive.error_code),
    error_message: nullableString(directive.error_message),
    legs: directive.legs.map(parseLeg),
  };
}

export function authorizedBooksForFund(
  profile: CurrentUserProfile | null,
  fundId: string | null,
): AuthorizedBook[] {
  if (!profile || !fundId) return [];
  return profile.funds.find((fund) => fund.fundId === fundId)?.books ?? [];
}

/** A sole authorized book is safe to preselect; two or more require a click. */
export function initialPaperBookId(books: readonly AuthorizedBook[]): string {
  return books.length === 1 ? books[0].bookId : "";
}

export function selectedAuthorizedBook(
  books: readonly AuthorizedBook[],
  selectedBookId: string,
): AuthorizedBook | null {
  return books.find((book) => book.bookId === selectedBookId) ?? null;
}

/** SSR과 브라우저 프라이버시 설정 양쪽에서 안전한 sessionStorage 접근. */
export function browserSessionStorage(): PaperOrderStorage | null {
  if (typeof window === "undefined") return null;
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export function createPaperOrderSubmission(
  input: PaperOrderActionInput,
  randomUuid: () => string = () => crypto.randomUUID(),
): PaperOrderSubmission {
  const query = input.query.trim();
  if (!input.fundId || !input.bookId || !query) throw new Error("paper_order_input_required");
  return createPaperOrderSubmissionWithKey(
    { fundId: input.fundId, bookId: input.bookId, query },
    `paper-order:${randomUuid()}`,
  );
}

function createPaperOrderSubmissionWithKey(
  input: PaperOrderActionInput,
  idempotencyKey: string,
): PaperOrderSubmission {
  return {
    path: PAPER_ORDER_PATH,
    idempotencyKey,
    init: {
      method: "POST",
      cache: "no-store",
      retryIdempotentMutation: true,
      headers: {
        Accept: "application/json",
        "Content-Type": "application/json",
        "Idempotency-Key": idempotencyKey,
      },
      body: JSON.stringify({
        fund_id: input.fundId,
        book_id: input.bookId,
        query: input.query,
      }),
    },
  };
}

export function paperOrderActionFingerprint(input: PaperOrderActionInput): string {
  return JSON.stringify([input.fundId, input.bookId, input.query.trim()]);
}

export function preparePaperOrderAction(
  input: PaperOrderActionInput,
  prior: RetryablePaperOrderAction | null,
  randomUuid: () => string = () => crypto.randomUUID(),
): RetryablePaperOrderAction & { reused: boolean } {
  const fingerprint = paperOrderActionFingerprint(input);
  if (prior?.fingerprint === fingerprint) {
    return { ...prior, reused: true };
  }
  const normalizedInput = { ...input, query: input.query.trim() };
  return {
    fingerprint,
    input: normalizedInput,
    submission: createPaperOrderSubmission(normalizedInput, randomUuid),
    reused: false,
  };
}

function paperOrderStorageKey(
  kind: "retry" | "pending",
  scope: PaperOrderStorageScope,
): string {
  return [
    "hgfinance.paper-order",
    kind,
    "v1",
    encodeURIComponent(scope.accountId),
    encodeURIComponent(scope.fundId),
    encodeURIComponent(scope.bookId),
  ].join(":");
}

function validStorageScope(scope: PaperOrderStorageScope): boolean {
  return Boolean(scope.accountId.trim() && scope.fundId.trim() && scope.bookId.trim());
}

function removeStoredValue(
  storage: PaperOrderStorage,
  kind: "retry" | "pending",
  scope: PaperOrderStorageScope,
): void {
  try {
    storage.removeItem(paperOrderStorageKey(kind, scope));
  } catch {
    // Storage can be disabled by browser privacy policy. In-memory safety still applies.
  }
}

/** Persist before transport so a reload cannot mint a second key for the same uncertain action. */
export function persistRetryablePaperOrderAction(
  storage: PaperOrderStorage,
  scope: PaperOrderStorageScope,
  action: RetryablePaperOrderAction,
): boolean {
  const input = { ...action.input, query: action.input.query.trim() };
  if (
    !validStorageScope(scope) ||
    input.fundId !== scope.fundId ||
    input.bookId !== scope.bookId ||
    !input.query ||
    action.fingerprint !== paperOrderActionFingerprint(input) ||
    !PAPER_ORDER_IDEMPOTENCY_KEY.test(action.submission.idempotencyKey)
  ) {
    return false;
  }
  try {
    storage.setItem(
      paperOrderStorageKey("retry", scope),
      JSON.stringify({
        schema_version: PAPER_ORDER_RETRY_SCHEMA,
        account_id: scope.accountId,
        fund_id: scope.fundId,
        book_id: scope.bookId,
        query: input.query,
        fingerprint: action.fingerprint,
        idempotency_key: action.submission.idempotencyKey,
      }),
    );
    return true;
  } catch {
    return false;
  }
}

export function loadRetryablePaperOrderAction(
  storage: PaperOrderStorage,
  scope: PaperOrderStorageScope,
): RetryablePaperOrderAction | null {
  if (!validStorageScope(scope)) return null;
  try {
    const raw = storage.getItem(paperOrderStorageKey("retry", scope));
    if (!raw) return null;
    const value = record(JSON.parse(raw));
    const input = {
      fundId: typeof value.fund_id === "string" ? value.fund_id : "",
      bookId: typeof value.book_id === "string" ? value.book_id : "",
      query: typeof value.query === "string" ? value.query.trim() : "",
    };
    if (
      value.schema_version !== PAPER_ORDER_RETRY_SCHEMA ||
      value.account_id !== scope.accountId ||
      input.fundId !== scope.fundId ||
      input.bookId !== scope.bookId ||
      !input.query ||
      typeof value.fingerprint !== "string" ||
      value.fingerprint !== paperOrderActionFingerprint(input) ||
      typeof value.idempotency_key !== "string" ||
      !PAPER_ORDER_IDEMPOTENCY_KEY.test(value.idempotency_key)
    ) {
      removeStoredValue(storage, "retry", scope);
      return null;
    }
    return {
      fingerprint: value.fingerprint,
      input,
      submission: createPaperOrderSubmissionWithKey(input, value.idempotency_key),
    };
  } catch {
    removeStoredValue(storage, "retry", scope);
    return null;
  }
}

export function clearRetryablePaperOrderAction(
  storage: PaperOrderStorage,
  scope: PaperOrderStorageScope,
): void {
  removeStoredValue(storage, "retry", scope);
}

/** Keep only a non-terminal directive; terminal results need no reload-time polling. */
export function persistPendingPaperDirective(
  storage: PaperOrderStorage,
  scope: PaperOrderStorageScope,
  directive: PaperDirective,
): boolean {
  if (
    !validStorageScope(scope) ||
    directive.fund_id !== scope.fundId ||
    directive.book_id !== scope.bookId ||
    !shouldPollPaperDirective(directive)
  ) {
    removeStoredValue(storage, "pending", scope);
    return false;
  }
  try {
    storage.setItem(
      paperOrderStorageKey("pending", scope),
      JSON.stringify({
        schema_version: PAPER_ORDER_PENDING_SCHEMA,
        account_id: scope.accountId,
        fund_id: scope.fundId,
        book_id: scope.bookId,
        directive,
      }),
    );
    return true;
  } catch {
    return false;
  }
}

export function loadPendingPaperDirective(
  storage: PaperOrderStorage,
  scope: PaperOrderStorageScope,
): PaperDirective | null {
  if (!validStorageScope(scope)) return null;
  try {
    const raw = storage.getItem(paperOrderStorageKey("pending", scope));
    if (!raw) return null;
    const value = record(JSON.parse(raw));
    const directive = parsePaperDirective(value.directive);
    if (
      value.schema_version !== PAPER_ORDER_PENDING_SCHEMA ||
      value.account_id !== scope.accountId ||
      value.fund_id !== scope.fundId ||
      value.book_id !== scope.bookId ||
      directive.fund_id !== scope.fundId ||
      directive.book_id !== scope.bookId ||
      !shouldPollPaperDirective(directive)
    ) {
      removeStoredValue(storage, "pending", scope);
      return null;
    }
    return directive;
  } catch {
    removeStoredValue(storage, "pending", scope);
    return null;
  }
}

export function clearPendingPaperDirective(
  storage: PaperOrderStorage,
  scope: PaperOrderStorageScope,
): void {
  removeStoredValue(storage, "pending", scope);
}

export function paperDirectiveStatusPath(input: {
  directiveId: string;
  fundId: string;
  bookId: string;
}): string {
  const params = new URLSearchParams({ fund_id: input.fundId, book_id: input.bookId });
  return `/ui/paper-orders/${encodeURIComponent(input.directiveId)}?${params.toString()}`;
}

function explainError(body: unknown, status: number): string {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    if (detail && typeof detail === "object") {
      const value = detail as { code?: unknown; field?: unknown };
      if (typeof value.code === "string") {
        return typeof value.field === "string" ? `${value.code}: ${value.field}` : value.code;
      }
    }
  }
  return `PAPER 주문 요청 실패 (HTTP ${status})`;
}

async function responseDirective(response: Response): Promise<PaperDirective> {
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) throw new Error(explainError(body, response.status));
  return parsePaperDirective(body);
}

export async function submitPaperOrder(input: {
  fundId: string;
  bookId: string;
  query: string;
}): Promise<PaperDirective> {
  // One invocation corresponds to one explicit click. `bffFetch` may reuse the
  // same request/key only for its explicit idempotent retry.
  const submission = createPaperOrderSubmission(input);
  return submitPaperOrderSubmission(submission);
}

/** Execute a pre-built action without changing its idempotency identity. */
export async function submitPaperOrderSubmission(
  submission: PaperOrderSubmission,
): Promise<PaperDirective> {
  return responseDirective(await bffFetch(submission.path, submission.init));
}

export async function getPaperDirective(input: {
  directiveId: string;
  fundId: string;
  bookId: string;
}): Promise<PaperDirective> {
  return responseDirective(
    await bffFetch(paperDirectiveStatusPath(input), {
      cache: "no-store",
      headers: { Accept: "application/json" },
    }),
  );
}

/** Only COMPLETED is ever presented as completion. */
export function paperDirectiveIsComplete(directive: PaperDirective): boolean {
  return directive.state === "COMPLETED";
}

export function shouldPollPaperDirective(
  directive: Pick<PaperDirective, "state"> | undefined,
): boolean {
  return !directive || !["COMPLETED", "PARTIAL", "FAILED"].includes(directive.state);
}

/** UNKNOWN is observed aggressively at first, then backed off without declaring completion. */
export function paperDirectivePollInterval(
  directive: Pick<PaperDirective, "state" | "created_at"> | undefined,
  nowMs: number = Date.now(),
): number | false {
  if (!shouldPollPaperDirective(directive)) return false;
  if (!directive || directive.state !== "UNKNOWN") return 2_000;
  const createdAtMs = Date.parse(directive.created_at);
  if (!Number.isFinite(createdAtMs)) return 30_000;
  const ageMs = Math.max(0, nowMs - createdAtMs);
  if (ageMs < UNKNOWN_FAST_POLL_AGE_MS) return 2_000;
  if (ageMs < UNKNOWN_MEDIUM_POLL_AGE_MS) return 10_000;
  return 30_000;
}
