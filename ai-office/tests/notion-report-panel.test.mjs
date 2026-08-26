import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  fetchNotionReport,
  fetchNotionReports,
} from "../app/lib/notionReportClient.ts";

const dashboard = await readFile(
  new URL("../app/dashboard/DashboardView.tsx", import.meta.url),
  "utf8",
);
const threadDialog = await readFile(
  new URL("../app/agent-logs/AgentLogsView.tsx", import.meta.url),
  "utf8",
);

/** 원본 `fetch`를 되돌려 주는 스텁. 브라우저가 아니므로 절대 주소로 나간다. */
function stubFetch(handler) {
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => handler(String(url), init);
  return () => {
    globalThis.fetch = original;
  };
}

function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  };
}

const CARD = {
  schema_version: "ui.notion-report-card.v1",
  page_id: "2adc190ac33d4d639a90f1ab86087f42",
  url: "https://www.notion.so/CEO-Synthesis-t-cc8081c5",
  title: "CEO Synthesis · t_cc8081c5",
  category: "저녁 브리핑",
  state: "보고 완료",
  published_at: "2026-08-26T17:12:00+09:00",
};

test("결과물 창고는 Kanban done 열이 아니라 발행된 Notion 리포트를 읽는다", () => {
  // done 카드와 발행된 리포트는 같은 집합이 아니다. 이 패널이 다시 Kanban을
  // 읽기 시작하면 열어도 리포트가 없는 행이 목록에 섞인다.
  assert.match(dashboard, /queryKey: \["notion-reports"\]/);
  assert.match(dashboard, /fetchNotionReports/);
  assert.doesNotMatch(dashboard, /fetchHermesKanban/);
  assert.doesNotMatch(dashboard, /columns\.done/);
});

test("목록에 세로 스크롤이 있고 칸반 바로가기 열은 없다", () => {
  assert.match(dashboard, /max-h-80 overflow-y-auto/);
  // 열 이름은 스크롤해도 남아야 한다.
  assert.match(dashboard, /sticky top-0 z-10/);
  // 바로가기는 열이 아니라 모달 안 버튼으로 옮겼다.
  assert.doesNotMatch(dashboard, /보드 보기/);
  assert.doesNotMatch(dashboard, />바로가기</);
});

test("행 전체가 하나의 버튼이라 키보드로도 리포트를 연다", () => {
  assert.match(dashboard, /<button\s+type="button"\s+onClick=\{\(\) => setOpenReport\(row\)\}/);
  // `<tr onClick>`은 포커스를 못 받는다 - 표를 쓰지 않는 이유가 이것이다.
  assert.doesNotMatch(dashboard, /<tr[^>]*onClick/);
  assert.match(dashboard, /\$\{OUTPUT_ROW_GRID\} group w-full/);
});

test("헤더와 행이 같은 비율 grid를 써서 열이 어긋나지 않는다", () => {
  // 헤더와 각 행은 서로 다른 grid 컨테이너라, `auto` 열은 각자 자기 글자
  // 수만큼만 넓어진다 - 그래서 "구분"/"발행 시각" 헤더가 값과 다른 자리에
  // 섰다. 글자 수가 아니라 가로 비율로 못 박아야 어느 행이든 같은 자리에서
  // 열이 시작한다.
  const grid = dashboard.match(/const OUTPUT_ROW_GRID =\s*"([^"]+)"/);
  assert.ok(grid, "OUTPUT_ROW_GRID가 없다");
  assert.doesNotMatch(grid[1], /_auto[_\]]/);
  assert.match(grid[1], /%_/);
  // 헤더와 행이 같은 상수를 안 쓰면 언젠가 다시 갈라진다.
  assert.equal((dashboard.match(/\$\{OUTPUT_ROW_GRID\}/g) ?? []).length, 2);
});

test("각 행이 눌러볼 수 있는 줄이라는 걸 아이콘으로 알린다", () => {
  // 글자만 있는 줄은 표처럼 보여서 아무도 누르지 않는다.
  assert.match(dashboard, /chevron_right/);
  assert.match(dashboard, /cursor-pointer/);
  assert.match(dashboard, /group-hover:/);
});

test("모달은 Discord 스레드 모달과 같은 dialog 방식·같은 겉모습을 쓴다", () => {
  assert.match(dashboard, /ref\.current\?\.showModal\(\)/);
  assert.match(dashboard, /<form method="dialog"/);

  // 겉모습이 갈라지면 같은 modal이라고 부를 수 없다. 스레드 모달의 dialog
  // className을 그대로 공유하는지 문자열로 대조한다.
  const dialogClass =
    'className="m-auto w-[min(46rem,92vw)] max-h-[85vh] p-0 rounded-xl ' +
    'bg-surface-container-lowest text-on-surface border border-outline-variant ' +
    'shadow-sm backdrop:bg-black/60"';
  assert.ok(threadDialog.includes(dialogClass), "스레드 모달 스타일이 바뀌었다");
  assert.ok(dashboard.includes(dialogClass), "리포트 모달이 같은 스타일을 안 쓴다");
});

test("모달 안에 칸반 바로가기와 Notion 링크 버튼이 함께 있다", () => {
  assert.match(dashboard, /칸반 바로가기/);
  assert.match(dashboard, /Notion에서 열기/);
  assert.match(dashboard, /renderDiscordMarkup\(report\.markdown\)/);
});

test("목록 조회는 계약을 검증하고 어긋나면 조용히 비우지 않는다", async () => {
  let requested = "";
  const restore = stubFetch((url) => {
    requested = url;
    return jsonResponse({
      schema_version: "ui.notion-reports.v1",
      source: "notion",
      authoritative: false,
      database_id: "db-1",
      reports: [CARD],
    });
  });
  try {
    const body = await fetchNotionReports(20);
    assert.equal(body.reports.length, 1);
    assert.equal(body.reports[0].title, "CEO Synthesis · t_cc8081c5");
    assert.match(requested, /\/ui\/notion\/reports\?limit=20$/);
  } finally {
    restore();
  }

  const restoreBad = stubFetch(() =>
    jsonResponse({ schema_version: "something-else", reports: [] }),
  );
  try {
    await assert.rejects(fetchNotionReports(20), /계약이 올바르지 않습니다/);
  } finally {
    restoreBad();
  }
});

test("BFF 오류는 detail을 그대로 화면 문구로 올린다", async () => {
  const restore = stubFetch(() =>
    jsonResponse({ detail: "NOTION_TOKEN / NOTION_CEO_DB가 설정되지 않았습니다." }, 503),
  );
  try {
    await assert.rejects(fetchNotionReports(), /NOTION_TOKEN \/ NOTION_CEO_DB/);
  } finally {
    restore();
  }
});

test("본문 조회는 page_id를 인코딩하고 마크다운을 그대로 돌려준다", async () => {
  let requested = "";
  const restore = stubFetch((url) => {
    requested = url;
    return jsonResponse({
      ...CARD,
      schema_version: "ui.notion-report.v1",
      source: "notion",
      authoritative: false,
      markdown: "# CEO Final Synthesis\n\n- Root task: `t_cc8081c5`",
      truncated: false,
    });
  });
  try {
    const detail = await fetchNotionReport("a/b?c");
    assert.match(detail.markdown, /^# CEO Final Synthesis/);
    assert.equal(detail.truncated, false);
    assert.ok(requested.endsWith("/ui/notion/reports/a%2Fb%3Fc"), requested);
  } finally {
    restore();
  }
});
