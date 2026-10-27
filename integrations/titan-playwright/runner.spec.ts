import { test, expect } from "@playwright/test";

test("the local test fixture renders", async ({ page }) => {
  await page.setContent("<main><h1>Fixture ready</h1></main>");
  await expect(page.getByRole("heading", { name: "Fixture ready" })).toBeVisible();
});
