import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import { parseCurrentUser } from "../app/lib/currentUserContract.ts";
import {
  authorizedBooksForFund,
  clearPendingPaperDirective,
  clearRetryablePaperOrderAction,
  createPaperOrderSubmission,
  initialPaperBookId,
  loadPendingPaperDirective,
  loadRetryablePaperOrderAction,
  paperDirectiveIsComplete,
  paperDirectivePollInterval,
  paperDirectiveStatusPath,
  persistPendingPaperDirective,
  persistRetryablePaperOrderAction,
  preparePaperOrderAction,
  selectedAuthorizedBook,
  shouldPollPaperDirective,
} from "../app/lib/paperOrderClient.ts";

class MemoryStorage {
  values = new Map();

  getItem(key) {
    return this.values.get(key) ?? null;
  }

  setItem(key, value) {
    this.values.set(key, String(value));
  }

  removeItem(key) {
    this.values.delete(key);
  }
}

function directive(overrides = {}) {
  return {
    directive_id: "directive-1",
    state: "UNKNOWN",
    action: "PLACE_ORDER",
    priority: 2000,
    priority_class: "USER_DIRECTIVE_HIGHEST",
    mode: "PAPER",
    fund_id: "fund-1",
    book_id: "book-2",
    idempotency_key: "paper-order:00000000-0000-4000-8000-000000000001",
    instruction_ref: "instruction-1",
    payload_sha256: "a".repeat(64),
    created_at: "2026-08-18T08:00:00.000Z",
    updated_at: "2026-08-18T08:00:01.000Z",
    completed_at: null,
    error_code: null,
    error_message: null,
    legs: [],
    ...overrides,
  };
}

const profile = parseCurrentUser({
  schema_version: "portfolio.current-user.v1",
  user_id: "user-1",
  display_name: "Operator",
  status: "ACTIVE",
  funds: [
    {
      fund_id: "fund-1",
      roles: ["OWNER"],
      books: [
        { book_id: "book-1", name: "Primary PAPER" },
        { book_id: "book-2", name: "Intraday PAPER" },
      ],
    },
  ],
  onboarding_required: false,
});

test("PAPER order mode uses only server-projected books and never guesses among many", () => {
  const books = authorizedBooksForFund(profile, "fund-1");
  assert.deepEqual(books.map((book) => book.bookId), ["book-1", "book-2"]);
  assert.equal(initialPaperBookId(books), "");
  assert.equal(initialPaperBookId([books[0]]), "book-1");
  assert.equal(selectedAuthorizedBook(books, "book-2")?.name, "Intraday PAPER");
  assert.equal(selectedAuthorizedBook(books, "attacker-book"), null);
  assert.deepEqual(authorizedBooksForFund(profile, "other-fund"), []);
});

test("current-user contract rejects duplicate or malformed trading books", () => {
  const base = {
    schema_version: "portfolio.current-user.v1",
    user_id: "user-1",
    status: "ACTIVE",
    onboarding_required: false,
  };
  assert.throws(
    () => parseCurrentUser({
      ...base,
      funds: [{
        fund_id: "fund-1",
        roles: ["OWNER"],
        books: [
          { book_id: "same", name: "One" },
          { book_id: "same", name: "Two" },
        ],
      }],
    }),
    /invalid_current_user_book/,
  );
  assert.throws(
    () => parseCurrentUser({
      ...base,
      funds: [{ fund_id: "fund-1", roles: ["OWNER"], books: [{ book_id: "book-1" }] }],
    }),
    /invalid_current_user_book/,
  );
});

test("each explicit PAPER action creates one key and enables only idempotent auth transport replay", () => {
  let sequence = 0;
  const uuid = () => `00000000-0000-4000-8000-${String(++sequence).padStart(12, "0")}`;
  const input = { fundId: "fund-1", bookId: "book-2", query: "삼성전자 2주 시장가 매수" };
  const first = createPaperOrderSubmission(input, uuid);
  const second = createPaperOrderSubmission(input, uuid);

  assert.equal(first.path, "/trading/agent/order");
  assert.notEqual(first.idempotencyKey, second.idempotencyKey);
  assert.equal(first.init.retryMutationAfterRefresh, true);
  assert.equal(new Headers(first.init.headers).get("Idempotency-Key"), first.idempotencyKey);
  assert.deepEqual(JSON.parse(String(first.init.body)), {
    fund_id: "fund-1",
    book_id: "book-2",
    query: "삼성전자 2주 시장가 매수",
  });

  const retry = preparePaperOrderAction(input, {
    fingerprint: JSON.stringify([input.fundId, input.bookId, input.query]),
    input,
    submission: first,
  }, uuid);
  assert.equal(retry.reused, true);
  assert.equal(retry.submission.idempotencyKey, first.idempotencyKey);

  const changedAction = preparePaperOrderAction({ ...input, query: "삼성전자 3주 시장가 매수" }, retry, uuid);
  assert.equal(changedAction.reused, false);
  assert.notEqual(changedAction.submission.idempotencyKey, first.idempotencyKey);
});

test("ambiguous PAPER retry identity survives reload only in the authenticated user/fund/book scope", () => {
  const storage = new MemoryStorage();
  const scope = { accountId: "user-1", fundId: "fund-1", bookId: "book-2" };
  const input = { fundId: "fund-1", bookId: "book-2", query: " 삼성전자 2주 시장가 매수 " };
  const prepared = preparePaperOrderAction(
    input,
    null,
    () => "00000000-0000-4000-8000-000000000001",
  );

  assert.equal(persistRetryablePaperOrderAction(storage, scope, prepared), true);
  const recovered = loadRetryablePaperOrderAction(storage, scope);
  assert.equal(recovered?.input.query, "삼성전자 2주 시장가 매수");
  assert.equal(recovered?.submission.idempotencyKey, prepared.submission.idempotencyKey);
  assert.equal(
    new Headers(recovered?.submission.init.headers).get("Idempotency-Key"),
    prepared.submission.idempotencyKey,
  );
  assert.equal(
    preparePaperOrderAction(input, recovered, () => "must-not-be-used").submission.idempotencyKey,
    prepared.submission.idempotencyKey,
  );

  assert.equal(
    loadRetryablePaperOrderAction(storage, { ...scope, accountId: "user-2" }),
    null,
  );
  assert.equal(
    loadRetryablePaperOrderAction(storage, { ...scope, bookId: "book-1" }),
    null,
  );
  clearRetryablePaperOrderAction(storage, scope);
  assert.equal(loadRetryablePaperOrderAction(storage, scope), null);
});

test("corrupt or cross-scope retry records are discarded instead of issuing a request", () => {
  const storage = new MemoryStorage();
  const scope = { accountId: "user-1", fundId: "fund-1", bookId: "book-2" };
  const action = preparePaperOrderAction(
    { fundId: "fund-1", bookId: "book-2", query: "삼성전자 2주 시장가 매수" },
    null,
    () => "00000000-0000-4000-8000-000000000001",
  );
  persistRetryablePaperOrderAction(storage, scope, action);
  const [key] = storage.values.keys();
  const stored = JSON.parse(storage.getItem(key));
  storage.setItem(key, JSON.stringify({ ...stored, book_id: "attacker-book" }));

  assert.equal(loadRetryablePaperOrderAction(storage, scope), null);
  assert.equal(storage.getItem(key), null);
});

test("non-terminal directive polling survives reload and terminal status clears it", () => {
  const storage = new MemoryStorage();
  const scope = { accountId: "user-1", fundId: "fund-1", bookId: "book-2" };
  const pending = directive();

  assert.equal(persistPendingPaperDirective(storage, scope, pending), true);
  assert.equal(loadPendingPaperDirective(storage, scope)?.directive_id, "directive-1");

  assert.equal(
    persistPendingPaperDirective(storage, scope, directive({ state: "COMPLETED" })),
    false,
  );
  assert.equal(loadPendingPaperDirective(storage, scope), null);

  persistPendingPaperDirective(storage, scope, pending);
  clearPendingPaperDirective(storage, scope);
  assert.equal(loadPendingPaperDirective(storage, scope), null);
});

test("status polling route remains fund/book scoped and completion is literal", () => {
  assert.equal(
    paperDirectiveStatusPath({
      directiveId: "directive/with slash",
      fundId: "fund 1",
      bookId: "book&2",
    }),
    "/ui/paper-orders/directive%2Fwith%20slash?fund_id=fund+1&book_id=book%262",
  );
  assert.equal(paperDirectiveIsComplete({ state: "COMPLETED" }), true);
  for (const state of ["RECEIVED", "RUNNING", "IN_PROGRESS", "PARTIAL", "FAILED", "UNKNOWN"]) {
    assert.equal(paperDirectiveIsComplete({ state }), false, state);
  }
  assert.equal(shouldPollPaperDirective({ state: "IN_PROGRESS" }), true);
  assert.equal(shouldPollPaperDirective({ state: "UNKNOWN" }), true);
  assert.equal(shouldPollPaperDirective({ state: "PARTIAL" }), false);
  assert.equal(shouldPollPaperDirective({ state: "FAILED" }), false);
});

test("UNKNOWN polling backs off with directive age without treating UNKNOWN as terminal", () => {
  const createdAt = Date.parse("2026-08-18T08:00:00.000Z");
  const unknown = { state: "UNKNOWN", created_at: "2026-08-18T08:00:00.000Z" };
  assert.equal(paperDirectivePollInterval(unknown, createdAt + 29_999), 2_000);
  assert.equal(paperDirectivePollInterval(unknown, createdAt + 30_000), 10_000);
  assert.equal(paperDirectivePollInterval(unknown, createdAt + 299_999), 10_000);
  assert.equal(paperDirectivePollInterval(unknown, createdAt + 300_000), 30_000);
  assert.equal(paperDirectivePollInterval({ ...unknown, created_at: "invalid" }, createdAt), 30_000);
  assert.equal(
    paperDirectivePollInterval({ state: "IN_PROGRESS", created_at: unknown.created_at }, createdAt + 999_999),
    2_000,
  );
  assert.equal(
    paperDirectivePollInterval({ state: "COMPLETED", created_at: unknown.created_at }, createdAt),
    false,
  );
});

test("CEO chat unifies advice and PAPER commands while keeping book scope explicit", async () => {
  const controlRoom = await readFile(
    new URL("../app/dashboard/CeoControlRoomChat.tsx", import.meta.url),
    "utf8",
  );
  const ceoClient = await readFile(
    new URL("../app/lib/ceoClient.ts", import.meta.url),
    "utf8",
  );
  assert.match(controlRoom, /궁금한 점을 묻거나 매매를 지시하면 대표가 확인해 처리합니다/);
  assert.match(controlRoom, /authorizedBooksForFund\(portfolio\.profile, portfolio\.activeFundId\)/);
  assert.match(controlRoom, /authorizedBooks\.length === 1/);
  assert.match(controlRoom, /계좌를 선택하세요/);
  assert.match(controlRoom, /질문과 안내는 계속 사용할 수 있습니다/);
  assert.doesNotMatch(controlRoom, /PAPER ONLY · LIVE 아님/);
  assert.doesNotMatch(controlRoom, /LIVE 아님/);
  assert.match(controlRoom, /askCeo\(text, undefined, bookId\)/);
  assert.doesNotMatch(controlRoom, /PaperOrderConsole|setMode\("paper"\)|role="tablist"/);
  assert.match(ceoClient, /bookId\?: string/);
  assert.match(ceoClient, /bookId \? \{ book_id: bookId \} : \{\}/);
});
