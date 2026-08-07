import { defineConfig, devices } from "@playwright/test";

const liveE2e = process.env.PORTFOLIO_LIVE_E2E === "1";

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: true,
  workers: 1,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 2 : 0,
  reporter: "list",
  use: {
    baseURL: process.env.AI_OFFICE_BASE_URL || "http://localhost:3006",
    trace: "retain-on-failure",
    ...devices["Desktop Chrome"],
  },
  webServer: liveE2e ? undefined : {
    command: "npm run dev -- --host 127.0.0.1 --port 3006",
    url: "http://localhost:3006",
    reuseExistingServer: !process.env.CI,
    timeout: 120_000,
  },
});
