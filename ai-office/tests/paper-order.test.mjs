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
  parsePaperDirective,
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

test("each explicit PAPER action creates one key and enables only idempotent transport replay", () => {
  let sequence = 0;
  const uuid = () => `00000000-0000-4000-8000-${String(++sequence).padStart(12, "0")}`;
  const input = { fundId: "fund-1", bookId: "book-2", query: "삼성전자 2주 시장가 매수" };
  const first = createPaperOrderSubmission(input, uuid);
  const second = createPaperOrderSubmission(input, uuid);

  assert.equal(first.path, "/trading/agent/order");
  assert.notEqual(first.idempotencyKey, second.idempotencyKey);
  assert.equal(first.init.retryIdempotentMutation, true);
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

test("ambiguous PAPER retry identity survives reload only in the current user/fund/book scope", () => {
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
  assert.match(controlRoom, /authorizedBooksForFund\(portfolio\.profile, effectiveFundId\)/);
  assert.match(controlRoom, /authorizedBooks\.length === 1/);
  assert.match(controlRoom, /계좌를 선택하세요/);
  assert.match(controlRoom, /질문과 안내는 계속 사용할 수 있습니다/);
  assert.doesNotMatch(controlRoom, /PAPER ONLY · LIVE 아님/);
  assert.doesNotMatch(controlRoom, /LIVE 아님/);
  assert.match(
    controlRoom,
    /askCeo\(text, undefined, bookId, fundId, confirmOrder\)/,
  );
  assert.doesNotMatch(controlRoom, /PaperOrderConsole|setMode\("paper"\)|role="tablist"/);
  assert.match(ceoClient, /fundId\?: string/);
  assert.match(ceoClient, /bookId\?: string/);
  assert.match(ceoClient, /bookId \? \{ book_id: bookId \} : \{\}/);
  assert.match(ceoClient, /confirmOrder \? \{ confirm_order: true \} : \{\}/);
});

test("모든 백엔드 directive action을 파싱한다", () => {
  // PLACE_BASKET이 허용목록에서 빠져 있으면 백엔드가 성공시킨 바스켓 주문을
  // UI가 paper_order_invalid_response로 던져버린다. 백엔드 계약
  // (orchestration/contracts/user_paper_order.py DirectiveAction)과 어긋나지
  // 않도록 다섯 액션을 모두 고정한다.
  for (const action of [
    "PLACE_ORDER",
    "PLACE_BASKET",
    "SELL_ALL",
    "SELL_POSITION",
    "CANCEL_ALL",
  ]) {
    const parsed = parsePaperDirective(directive({ action }));
    assert.equal(parsed.action, action);
  }
});

test("계약에 없는 action은 계속 거부한다", () => {
  assert.throws(
    () => parsePaperDirective(directive({ action: "LIQUIDATE_ALL" })),
    /paper_order_invalid_response/,
  );
});

test("결말이 불확실한 재전송은 같은 키를, 확인된 뒤 같은 주문은 새 키를 쓴다", () => {
  // CEO 채팅이 의존하는 안전장치의 전 생애주기. 이 순서가 깨지면 타임아웃 뒤
  // 재전송이 두 번째 주문이 되거나(키가 안 살아남음), 사용자가 의도적으로 같은
  // 주문을 한 번 더 낼 수 없게 된다(키를 안 버림).
  const storage = new MemoryStorage();
  const scope = { accountId: "user-1", fundId: "fund-1", bookId: "book-2" };
  const input = { fundId: "fund-1", bookId: "book-2", query: "보유종목 전량 일괄매도" };
  let sequence = 0;
  const uuid = () => `00000000-0000-4000-8000-${String(++sequence).padStart(12, "0")}`;

  // 1) 최초 전송: 키를 만들고 전송 *전에* 저장한다.
  const first = preparePaperOrderAction(input, loadRetryablePaperOrderAction(storage, scope), uuid);
  assert.equal(first.reused, false);
  assert.equal(persistRetryablePaperOrderAction(storage, scope, first), true);

  // 2) 결말 모름(타임아웃) 후 새로고침 + 같은 지시 재전송 -> 같은 키.
  const afterReload = preparePaperOrderAction(input, loadRetryablePaperOrderAction(storage, scope), uuid);
  assert.equal(afterReload.reused, true);
  assert.equal(
    afterReload.submission.idempotencyKey,
    first.submission.idempotencyKey,
    "재전송이 새 키를 뽑으면 서버 중복 방지를 통과해 주문이 두 번 들어간다",
  );

  // 3) 결말 확인 -> 키를 버린다.
  clearRetryablePaperOrderAction(storage, scope);
  assert.equal(loadRetryablePaperOrderAction(storage, scope), null);

  // 4) 이제 같은 주문을 한 번 더 내는 것은 정상적으로 새 주문이다.
  const deliberateSecond = preparePaperOrderAction(input, loadRetryablePaperOrderAction(storage, scope), uuid);
  assert.equal(deliberateSecond.reused, false);
  assert.notEqual(deliberateSecond.submission.idempotencyKey, first.submission.idempotencyKey);
});

test("안전장치 키는 CEO ask의 request_id 제약(8~128자)을 만족한다", () => {
  // 채팅은 이 키를 `askCeo`의 request_id로 보낸다. AgentAsk.request_id는
  // min_length=8, max_length=128의 자유 문자열이다(hermes_boundary.py).
  const prepared = preparePaperOrderAction(
    { fundId: "fund-1", bookId: "book-2", query: "보유종목 전량 일괄매도" },
    null,
    () => "00000000-0000-4000-8000-000000000001",
  );
  const key = prepared.submission.idempotencyKey;
  assert.ok(key.length >= 8 && key.length <= 128, `키 길이 ${key.length}`);
  assert.match(key, /^paper-order:/);
});
