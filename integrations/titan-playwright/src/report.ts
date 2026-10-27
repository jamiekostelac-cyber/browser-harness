export type JobStatus = "ok" | "failed";

export type StepResult = {
  index: number;
  type: string;
  result?: string;
  selector?: string;
  mode?: "redacted";
  truncated?: boolean;
};

const SECRET_TEXT_PATTERNS = [
  /\bBearer\s+[A-Za-z0-9._~+\/-]+=*/gi,
  /\b(?:api[_ -]?key|authorization|cookie|password|passcode|secret|session|token)\s*[:=]\s*(?:Bearer\s+)?[^\s,;]+/gi,
  /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b/g,
  /\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b/gi,
];

export function redactText(input: string): string {
  return SECRET_TEXT_PATTERNS.reduce((text, pattern) => text.replace(pattern, "[REDACTED]"), input);
}

export function safeUrl(input: string): string {
  try {
    const url = new URL(input);
    url.username = "";
    url.password = "";
    url.search = "";
    url.hash = "";
    url.pathname = "/";
    return url.toString();
  } catch {
    return "[invalid-url]";
  }
}

export function successReport(jobId: string, steps: StepResult[]) {
  return { status: "ok" as JobStatus, jobId, steps, artifacts: null };
}

export function failureReport(
  jobId: string,
  error: "timeout" | "step_failed" | "browser_unavailable" | "context_failed" | "setup_failed" | "invalid_job",
  stepIndex: number | null,
  artifacts: { trace: string | null; video: string | null; directory?: string } | null,
) {
  return { status: "failed" as JobStatus, jobId, error, stepIndex, artifacts };
}
