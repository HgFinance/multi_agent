import assert from "node:assert/strict";
import test from "node:test";

import { explainLangsmithError } from "../app/lib/langsmithClient.ts";

test("LangSmith errors keep structured upstream messages", () => {
  assert.equal(
    explainLangsmithError({ detail: { error_code: "feedback_unavailable", message: "잠시 후 다시 시도해 주세요" } }, 503),
    "잠시 후 다시 시도해 주세요",
  );
});
