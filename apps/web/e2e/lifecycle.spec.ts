import path from "node:path";

import { expect, test } from "@playwright/test";

const DOCS = path.resolve(__dirname, "../../../examples/demo-documents");

async function launch(page: import("@playwright/test").Page, file: string, forceFailure = false) {
  await page.goto("/dashboard");
  await expect(page.getByText("SIMULATION MODE", { exact: false })).toBeVisible();
  await page.setInputFiles('[data-testid="file-input"]', path.join(DOCS, file));
  if (forceFailure) await page.getByTestId("force-failure").check();
  await page.getByRole("button", { name: /analyze document/i }).click();
  await page.waitForURL(/\/jobs\//);
}

test("successful job: GPU goes active, then is destroyed — COMPUTE = 0", async ({ page }) => {
  await launch(page, "sample-contract.pdf");
  await expect(page.getByTestId("instance-banner")).toContainText(/ACTIVE|PROVISIONING/);
  await expect(page.getByTestId("job-status")).toContainText("COMPLETED");
  await expect(page.getByTestId("instance-banner")).toContainText("DESTROYED");
  await expect(page.getByTestId("compute-zero")).toContainText("COMPUTE = 0");
  await expect(page.getByTestId("result")).toContainText("Contract");
  await expect(page.getByTestId("timeline").locator('[data-state="failed"]')).toHaveCount(0);

  await page.goto("/dashboard");
  await expect(page.getByTestId("active-gpus")).toHaveText("0");
  await expect(page.getByTestId("compute-hero")).toContainText("COMPUTE = 0");
});

test("forced inference failure still destroys the GPU", async ({ page }) => {
  await launch(page, "sample-policy.pdf", true);
  await expect(page.getByTestId("job-status")).toContainText("FAILED");
  await expect(page.getByTestId("job-error")).toContainText("INFERENCE FORCED FAILURE");
  await expect(page.getByTestId("job-error")).toContainText("Cleanup successful");
  await expect(page.getByTestId("job-error")).toContainText("GPU destroyed");
  await expect(page.getByTestId("compute-zero")).toContainText("COMPUTE = 0");
});

test("prompt-injection document is flagged, not obeyed", async ({ page }) => {
  await launch(page, "prompt-injection-test.pdf");
  await expect(page.getByTestId("job-status")).toContainText("COMPLETED");
  await expect(page.getByTestId("result")).toContainText("attempts to instruct an AI system");
});
