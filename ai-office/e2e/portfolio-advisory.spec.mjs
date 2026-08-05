import { test, expect } from "@playwright/test";

test("renders the advisory control plane in a fail-closed offline state", async ({ page }) => {
  await page.route("http://127.0.0.1:8001/**", (route) => route.abort("failed"));
  await page.goto("/");

  await expect(page).toHaveTitle("HgFinance - AI 헤지펀드 오피스");
  await page.getByRole("button", { name: "Mandate 설정" }).click();
  await expect(page.locator("#portfolio-interview")).toContainText("Mandate Configuration");
  await expect(page.locator("#portfolio-interview")).toContainText("USER INPUT → CEO ROUTER → RISK / QA GATE");
  await expect(page.locator("#portfolio-interview")).toContainText("Mandate 제출하고 검토 시작");
});

test("keeps the browser surface advisory-only", async ({ page }) => {
  await page.route("http://127.0.0.1:8001/**", (route) => route.abort("failed"));
  await page.goto("/");

  await page.getByRole("button", { name: "Mandate 설정" }).click();
  await expect(page.locator("#portfolio-interview")).toContainText("Mandate Configuration");
  await expect(page.locator("#portfolio-interview")).not.toContainText("주문 전송");
  await expect(page.locator("#portfolio-interview")).not.toContainText("Ledger Posting");
});
test("fails closed when the BFF returns an invalid snapshot contract", async ({ page }) => {
  await page.route("http://127.0.0.1:8001/ui/snapshot", (route) => route.fulfill({
    status: 200,
    contentType: "application/json",
    body: JSON.stringify({ schema_version: "invalid", mode: "DEMO" }),
  }));
  await page.goto("/");
  await page.getByRole("button", { name: "📊 대시보드" }).click();
  await page.getByRole("button", { name: "Operations Console" }).click();
  await expect(page.getByRole("heading", { name: "실행의 모든 흔적을" })).toBeVisible();
  await expect(page.getByText("미연결", { exact: true }).first()).toBeVisible();
  await expect(page.getByText("RUNTIME CONNECTED", { exact: true })).toHaveCount(0);
});
