import { defineConfig } from "@playwright/test";

// Runs against a live stack (API + worker in EPHEMERA_MODE=simulation + web).
// `make e2e` starts everything; set BASE_URL to target another deployment.
export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  expect: { timeout: 60_000 },
  use: {
    baseURL: process.env.BASE_URL ?? "http://localhost:3000",
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_PATH }
      : undefined,
    screenshot: "only-on-failure",
  },
  reporter: [["list"]],
});
