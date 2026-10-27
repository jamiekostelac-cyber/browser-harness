import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 10_000,
  retries: 0,
  workers: 1,
  reporter: "list",
  use: {
    browserName: "chromium",
    headless: true,
    // The harness tests explicitly launch and instrument their own contexts.
    trace: "off",
    video: "off",
  },
});
