import { test, expect } from "@playwright/test";
import { failureReport, redactText, safeUrl, successReport } from "../src/report.ts";

test("safeUrl removes credentials, query values, and fragments", () => {
  expect(safeUrl("https://user:pass@example.test/path?token=secret#section"))
    .toBe("https://example.test/");
});

test("safeUrl redacts sensitive and opaque path segments", () => {
  expect(safeUrl("https://example.test/reset/private-reset-code/abc123456789012345678901234567890123456789"))
    .toBe("https://example.test/");
});

test("redactText removes common secrets and email addresses from snapshots", () => {
  expect(redactText("Contact person@example.com. Authorization: Bearer abc.def.ghi token=secret-value"))
    .toBe("Contact [REDACTED]. [REDACTED] [REDACTED]");
});

test("failure reports expose only bounded codes and artifact paths", () => {
  const report = failureReport("job-1", "step_failed", 2, { trace: "/tmp/trace.zip", video: null });
  expect(report).toEqual({
    status: "failed",
    jobId: "job-1",
    error: "step_failed",
    stepIndex: 2,
    artifacts: { trace: "/tmp/trace.zip", video: null },
  });
  expect(successReport("job-1", [{ index: 0, type: "snapshot", result: "ok" }]).status).toBe("ok");
});
