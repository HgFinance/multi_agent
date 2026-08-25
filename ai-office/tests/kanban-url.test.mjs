import assert from "node:assert/strict";
import test from "node:test";

import { resolveKanbanUrl } from "../app/lib/kanbanUrl.ts";

const HERMES = "http://127.0.0.1:9119";

test("보드 경로는 항상 /kanban이다", () => {
  // 임베드 주소가 /kanban이면 보드가 바로 열린다.
  assert.equal(resolveKanbanUrl(HERMES), "http://127.0.0.1:9119/kanban");
  assert.equal(resolveKanbanUrl(`${HERMES}/`), "http://127.0.0.1:9119/kanban");
  assert.equal(resolveKanbanUrl(`${HERMES}/kanban`), "http://127.0.0.1:9119/kanban");
});

test("host를 페이지에 맞춘다 - SameSite=Lax 세션 쿠키 회귀", () => {
  // host가 어긋나면 iframe 안에서 SameSite 쿠키가 유지되지 않아 보드 상태가
  // 매번 초기화된다.
  assert.equal(resolveKanbanUrl(HERMES, "localhost"), "http://localhost:9119/kanban");
  assert.equal(resolveKanbanUrl("http://localhost:9119", "127.0.0.1"), "http://127.0.0.1:9119/kanban");
  // 포트는 SameSite 판정에 안 들어가므로 건드리지 않는다.
  assert.equal(new URL(resolveKanbanUrl(HERMES, "localhost")).port, "9119");
});

test("loopback이 아니면 설정값을 그대로 둔다", () => {
  // 페이지가 LAN IP/도메인이면 거기에 Hermes가 떠 있다는 보장이 없다.
  assert.equal(resolveKanbanUrl(HERMES, "192.168.0.10"), "http://127.0.0.1:9119/kanban");
  assert.equal(resolveKanbanUrl(HERMES, "office.example.com"), "http://127.0.0.1:9119/kanban");
  assert.equal(
    resolveKanbanUrl("https://hermes.example.com", "localhost"),
    "https://hermes.example.com/kanban",
  );
});

test("임의 주소를 이 화면에 임베드하지 못한다", () => {
  assert.equal(resolveKanbanUrl("javascript:alert(1)"), null);
  assert.equal(resolveKanbanUrl("file:///etc/passwd"), null);
  assert.equal(resolveKanbanUrl("http://user:pw@127.0.0.1:9119"), null, "자격증명 포함 URL");
  assert.equal(resolveKanbanUrl(`${HERMES}/kanban?token=abc`), null, "query 포함");
  assert.equal(resolveKanbanUrl(`${HERMES}/kanban#x`), null, "hash 포함");
  assert.equal(resolveKanbanUrl(`${HERMES}/admin`), null, "/kanban 외 경로");
  assert.equal(resolveKanbanUrl("not a url"), null);
  assert.equal(resolveKanbanUrl(""), null);
});
