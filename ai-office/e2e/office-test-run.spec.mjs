import { test, expect } from "@playwright/test";

test("DEMO runs the whole office from 09:00 to completion", async ({ page }) => {
  test.setTimeout(90_000);
  // The DEMO projection is intentionally self-contained. No BFF or production
  // endpoint is needed for this browser acceptance scenario.
  await page.route("http://127.0.0.1:8001/**", (route) => route.abort("failed"));
  await page.goto("/");

  await expect(page.locator(".live-clock b")).toHaveText("09:00");
  await expect(page.locator(".live-bar")).toContainText("테스트 오피스 시작");

  await page.getByRole("button", { name: "4x" }).click();
  await page.getByRole("button", { name: "테스트 오피스 시작" }).click();

  await expect(page.locator(".feed-list")).toContainText("09:00 자동 출근");
  await expect(page.locator(".live-progress")).toContainText("업무 종료", {
    timeout: 90_000,
  });
  await expect(page.locator(".live-progress")).toContainText("100%");
  await expect(page.locator(".live-clock b")).toHaveText("18:00");
  await expect(page.locator(".live-counts")).toContainText("완료 8");
});
